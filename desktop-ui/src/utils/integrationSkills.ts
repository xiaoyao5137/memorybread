import { fetchWithLocalhostFallback } from '../hooks/useApi'

export type IntegrationDirection = 'input' | 'output'
export type IntegrationInputKind = 'folder' | 'files' | 'query' | 'memory_pick' | 'none'
export type IntegrationRunMode = 'preview' | 'execute'
export type IntegrationRunStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface IntegrationSkillCatalogItem {
  id: string
  title: string
  eyebrow: string
  description: string
  capability: string
  badge: string
  direction: IntegrationDirection
  executor: string
  version: string
  inputKind: IntegrationInputKind
  accept: string
  supportsPreview: boolean
  fileCount: number
}

export interface IntegrationSkillSourceFile {
  path: string
  mediaType: string
  sizeBytes: number
  content?: string
}

export interface IntegrationSkillDetail extends IntegrationSkillCatalogItem {
  files: IntegrationSkillSourceFile[]
}

export interface IntegrationSkillInputFile {
  path: string
  mediaType: string
  contentBase64: string
  sizeBytes: number
}

export interface IntegrationSkillRunLog {
  ts: number
  level: 'info' | 'success' | 'warning' | 'error' | string
  message: string
}

export interface IntegrationArtifact {
  fileName: string
  mediaType: string
  contentBase64: string
}

export interface IntegrationSkillRunResult {
  kind?: 'import' | 'artifact' | 'install' | 'install_preview' | 'vault_preview' | 'vault_export' | string
  mode?: IntegrationRunMode
  parsed?: number
  created?: number
  updated?: number
  unchanged?: number
  skipped?: number
  tagCount?: number
  linkCount?: number
  embedCount?: number
  matchCount?: number
  noteCount?: number
  overwriteCount?: number
  sample?: Array<{ title: string; path: string }>
  records?: Array<{
    id: string | number
    title: string
    path?: string
    outcome?: 'created' | 'updated' | 'unchanged' | string
  }>
  target?: string
  fileCount?: number
  existingInstallation?: boolean
  willBackup?: boolean
  backup?: string | null
  invocation?: string
  artifact?: IntegrationArtifact
}

export interface IntegrationSkillRun {
  id: string
  skillId: string
  mode: IntegrationRunMode
  status: IntegrationRunStatus
  inputSummary: {
    fileCount?: number
    totalBytes?: number
    queryLength?: number
    limit?: number
  }
  result?: IntegrationSkillRunResult | null
  logs: IntegrationSkillRunLog[]
  errorCode?: string | null
  errorMessage?: string | null
  createdAtMs: number
  startedAtMs?: number | null
  finishedAtMs?: number | null
}

export interface StartIntegrationSkillRunInput {
  mode: IntegrationRunMode
  files?: IntegrationSkillInputFile[]
  config?: Record<string, unknown>
}

export interface IntegrationMemoryOption {
  id: number
  title: string
  category: string
  observedAt?: number | null
}

const parseError = async (response: Response, fallback: string) => {
  const payload = await response.json().catch(() => null)
  return String(payload?.message || payload?.error || fallback)
}

export async function listIntegrationSkills(apiBaseUrl: string): Promise<IntegrationSkillCatalogItem[]> {
  const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/integration-skills`)
  if (!response.ok) throw new Error(await parseError(response, '读取集成 Skill 失败'))
  return response.json()
}

export async function getIntegrationSkill(
  apiBaseUrl: string,
  skillId: string,
): Promise<IntegrationSkillDetail> {
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/integration-skills/${encodeURIComponent(skillId)}`,
  )
  if (!response.ok) throw new Error(await parseError(response, '读取 Skill 文件失败'))
  return response.json()
}

export async function startIntegrationSkillRun(
  apiBaseUrl: string,
  skillId: string,
  input: StartIntegrationSkillRunInput,
): Promise<IntegrationSkillRun> {
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/integration-skills/${encodeURIComponent(skillId)}/runs`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: input.mode,
        files: input.files || [],
        config: input.config || {},
      }),
    },
  )
  if (!response.ok) throw new Error(await parseError(response, '启动 Skill 失败'))
  return response.json()
}

export async function getIntegrationSkillRun(
  apiBaseUrl: string,
  runId: string,
): Promise<IntegrationSkillRun> {
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/integration-skills/runs/${encodeURIComponent(runId)}`,
  )
  if (!response.ok) throw new Error(await parseError(response, '读取 Skill 状态失败'))
  return response.json()
}

export async function listIntegrationSkillRuns(
  apiBaseUrl: string,
  skillId?: string,
  limit = 30,
): Promise<IntegrationSkillRun[]> {
  const search = new URLSearchParams({ limit: String(limit) })
  if (skillId) search.set('skillId', skillId)
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/integration-skills/runs?${search}`,
  )
  if (!response.ok) throw new Error(await parseError(response, '读取 Skill 执行历史失败'))
  return response.json()
}

export async function listIntegrationMemoryOptions(
  apiBaseUrl: string,
  query = '',
  limit = 60,
  offset = 0,
): Promise<IntegrationMemoryOption[]> {
  const search = new URLSearchParams({ limit: String(limit) })
  if (offset > 0) search.set('offset', String(offset))
  if (query.trim()) search.set('q', query.trim())
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/integration-skills/memory-options?${search}`,
  )
  if (!response.ok) throw new Error(await parseError(response, '读取记忆候选失败'))
  return response.json()
}

export async function pickLocalDirectory(): Promise<string | null> {
  try {
    const { invoke } = await import('@tauri-apps/api/core')
    const picked = await invoke<string | null>('pick_local_directory')
    return typeof picked === 'string' && picked.trim() ? picked : null
  } catch {
    return null
  }
}

export async function selectedFilesToIntegrationInput(
  files: File[] | FileList,
): Promise<IntegrationSkillInputFile[]> {
  const selected = Array.from(files).filter(file => file.name !== '.DS_Store')
  const rawPaths = selected.map(file => String(
    (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
  ).replace(/\\/g, '/').replace(/^\/+/, ''))
  const roots = new Set(rawPaths.filter(path => path.includes('/')).map(path => path.split('/')[0]))
  const root = roots.size === 1 ? [...roots][0] : ''
  const normalizedPaths = rawPaths.map(path => root && path.startsWith(`${root}/`)
    ? path.slice(root.length + 1)
    : path)

  return Promise.all(selected.map(async (file, index) => ({
    path: normalizedPaths[index],
    mediaType: file.type || inferMediaType(normalizedPaths[index]),
    contentBase64: bytesToBase64(new Uint8Array(await file.arrayBuffer())),
    sizeBytes: file.size,
  })))
}

export async function downloadIntegrationSkillBundle(apiBaseUrl: string, skillId: string) {
  await downloadFromUrl(
    `${apiBaseUrl}/api/integration-skills/${encodeURIComponent(skillId)}/bundle`,
    `memorybread-${skillId}.skill.json`,
  )
}

export async function downloadIntegrationSkillFile(
  apiBaseUrl: string,
  skillId: string,
  path: string,
) {
  const search = new URLSearchParams({ path })
  await downloadFromUrl(
    `${apiBaseUrl}/api/integration-skills/${encodeURIComponent(skillId)}/file?${search}`,
    path.split('/').pop() || 'skill-file.txt',
  )
}

export function downloadIntegrationArtifact(artifact: IntegrationArtifact) {
  const bytes = base64ToBytes(artifact.contentBase64)
  downloadBlob(new Blob([bytes], { type: artifact.mediaType }), artifact.fileName)
}

export async function copyIntegrationArtifact(artifact: IntegrationArtifact) {
  const text = new TextDecoder().decode(base64ToBytes(artifact.contentBase64))
  await navigator.clipboard.writeText(text)
}

async function downloadFromUrl(url: string, fallbackName: string) {
  const response = await fetchWithLocalhostFallback(url)
  if (!response.ok) throw new Error(await parseError(response, '下载 Skill 文件失败'))
  const disposition = response.headers.get('content-disposition') || ''
  const match = disposition.match(/filename="?([^";]+)"?/i)
  downloadBlob(await response.blob(), match?.[1] || fallbackName)
}

function downloadBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

function bytesToBase64(bytes: Uint8Array) {
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
  }
  return btoa(binary)
}

function base64ToBytes(content: string) {
  const binary = atob(content)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}

function inferMediaType(path: string) {
  if (/\.md$/i.test(path)) return 'text/markdown'
  if (/\.json$/i.test(path)) return 'application/json'
  if (/\.jsonl$/i.test(path)) return 'application/x-ndjson'
  if (/\.csv$/i.test(path)) return 'text/csv'
  return 'application/octet-stream'
}
