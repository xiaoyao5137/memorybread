import { strToU8, zipSync } from 'fflate'
import { serviceEnvironmentHeaders } from '../store/useAppStore'
import type { DebugLogContent, DebugLogFile } from '../types'
import type { AppMetadata } from './appMetadata'

const INSTALLATION_ID_KEY = 'memory-bread_customer-log-installation-id'
const MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
const MAX_LOG_FILES = 8

interface PreparedUpload {
  upload_id: string
  oss_object_key: string
  upload_url: string
  required_headers: Record<string, string>
}

interface ApiEnvelope<T> {
  data: T
}

export interface CustomerLogReceipt {
  log_id: string
  received_at: string
  duplicate: boolean
}

const uuid = (): string => {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()
  const bytes = new Uint8Array(16)
  cryptoApi.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export const getCustomerLogInstallationId = (): string => {
  const existing = window.localStorage.getItem(INSTALLATION_ID_KEY)?.trim()
  if (existing && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(existing)) {
    return existing
  }
  const created = uuid()
  window.localStorage.setItem(INSTALLATION_ID_KEY, created)
  return created
}

export const scrubDiagnosticLog = (content: string): string => content
  .replace(/\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b/gi, '[REDACTED_TOKEN]')
  .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [REDACTED]')
  .replace(/\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\s*[:=]\s*([^\s,;]+)/gi, '$1=[REDACTED]')
  .replace(/[A-Z]:\\Users\\[^\\\s]+/gi, '[USER_HOME]')
  .replace(/\/Users\/[^/\s]+/g, '[USER_HOME]')
  .replace(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/g, '[REDACTED_EMAIL]')
  .replace(/(?<!\d)1[3-9]\d{9}(?!\d)/g, '[REDACTED_PHONE]')

const readApiError = async (response: Response, fallback: string): Promise<Error> => {
  const payload = await response.json().catch(() => null)
  return new Error(payload?.error?.message || fallback)
}

const sha256Hex = async (value: Uint8Array): Promise<string> => {
  const bytes = new Uint8Array(value)
  const digest = await crypto.subtle.digest('SHA-256', bytes.buffer)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

const safeFileName = (key: string, index: number): string => {
  const safe = key.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
  return `${String(index + 1).padStart(2, '0')}-${safe || 'log'}.log`
}

const normalizedArchitecture = (architecture: string): string => {
  if (['aarch64', 'arm64', 'x86_64', 'amd64'].includes(architecture)) return architecture
  return navigator.platform.toLowerCase().includes('arm') ? 'aarch64' : 'x86_64'
}

const collectArchive = async (localApiBaseUrl: string, metadata: AppMetadata): Promise<Uint8Array> => {
  const response = await fetch(`${localApiBaseUrl}/api/debug/log-files`)
  if (!response.ok) throw new Error('无法读取本机诊断日志')
  const files = await response.json() as DebugLogFile[]
  const available = files.filter((file) => file.exists).slice(0, MAX_LOG_FILES)
  if (available.length === 0) throw new Error('当前没有可上报的诊断日志')

  const archiveFiles: Record<string, Uint8Array> = {}
  const manifest = {
    schema_version: 'customer-log.v1',
    generated_at: new Date().toISOString(),
    client_version: metadata.version,
    platform: metadata.platform,
    architecture: normalizedArchitecture(metadata.architecture),
    privacy: {
      scrubbed: true,
      excluded: ['screenshots', 'database', 'captures', 'memories', 'prompts', 'answers'],
    },
    logs: [] as Array<{ key: string; label: string; truncated: boolean; original_size_bytes: number }>,
  }
  for (const [index, file] of available.entries()) {
    const logResponse = await fetch(`${localApiBaseUrl}/api/debug/log-files/${encodeURIComponent(file.key)}`)
    if (!logResponse.ok) continue
    const log = await logResponse.json() as DebugLogContent
    archiveFiles[safeFileName(file.key, index)] = strToU8(scrubDiagnosticLog(log.content))
    manifest.logs.push({
      key: file.key,
      label: file.label,
      truncated: log.truncated,
      original_size_bytes: log.total_size_bytes,
    })
  }
  if (manifest.logs.length === 0) throw new Error('本机诊断日志读取失败')
  archiveFiles['manifest.json'] = strToU8(JSON.stringify(manifest, null, 2))
  const archive = zipSync(archiveFiles, { level: 6 })
  if (archive.byteLength > MAX_ARCHIVE_BYTES) throw new Error('诊断日志超过 10MB，请稍后重试')
  return archive
}

export const reportCustomerLogs = async ({
  adminApiBaseUrl,
  localApiBaseUrl,
  authToken,
  metadata,
  description,
}: {
  adminApiBaseUrl: string
  localApiBaseUrl: string
  authToken?: string | null
  metadata: AppMetadata
  description?: string
}): Promise<CustomerLogReceipt> => {
  const archive = await collectArchive(localApiBaseUrl.replace(/\/+$/, ''), metadata)
  const checksum = await sha256Hex(archive)
  const installationId = getCustomerLogInstallationId()
  const fileName = `memorybread-diagnostics-${new Date().toISOString().slice(0, 10)}.zip`
  const common = {
    installation_id: installationId,
    file_name: fileName,
    size_bytes: archive.byteLength,
    checksum_sha256: checksum,
    client_version: metadata.version,
    platform: metadata.platform,
    architecture: normalizedArchitecture(metadata.architecture),
    description: description?.trim() || null,
  }
  const headers: Record<string, string> = {
    ...serviceEnvironmentHeaders(),
    'Content-Type': 'application/json',
  }
  if (authToken) headers.Authorization = `Bearer ${authToken}`

  const prepareResponse = await fetch(`${adminApiBaseUrl}/v1/customer-logs/upload-url`, {
    method: 'POST',
    headers,
    body: JSON.stringify(common),
  })
  if (!prepareResponse.ok) throw await readApiError(prepareResponse, '无法准备日志上报')
  const prepared = (await prepareResponse.json() as ApiEnvelope<PreparedUpload>).data
  const uploadResponse = await fetch(prepared.upload_url, {
    method: 'PUT',
    headers: prepared.required_headers,
    body: new Blob([archive.slice().buffer], { type: 'application/zip' }),
  })
  if (!uploadResponse.ok) throw new Error('诊断日志上传失败，请检查网络后重试')

  const completeResponse = await fetch(`${adminApiBaseUrl}/v1/customer-logs`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      ...common,
      upload_id: prepared.upload_id,
      oss_object_key: prepared.oss_object_key,
    }),
  })
  if (!completeResponse.ok) throw await readApiError(completeResponse, '日志上报确认失败')
  return (await completeResponse.json() as ApiEnvelope<CustomerLogReceipt>).data
}
