import type { CloudDevice, ServiceEnvironment } from '../types'
import { invoke } from '@tauri-apps/api/core'
import { serviceEnvironmentHeaders } from '../store/useAppStore'
import { cloudApiErrorCode, upsertCloudDevice } from './authApi'
import { getAppMetadata, type AppMetadata } from './appMetadata'

export interface SoftwareRelease {
  id: string
  version: string
  build_number: number
  channel: string
  distribution: 'direct' | 'app_store'
  platform: string
  architecture: string
  title: string
  release_notes: string
  download_url: string
  checksum_sha256?: string | null
  updater_signature?: string | null
  download_size_bytes?: number | null
  minimum_supported_version?: string | null
  rollout_percentage: number
  is_mandatory: boolean
  status: string
  published_at?: string | null
  created_at: string
  updated_at: string
}

export interface SoftwareUpdateCheck {
  current_version: string
  latest_version: string
  update_available: boolean
  is_mandatory: boolean
  release?: SoftwareRelease | null
}

export interface PreparedSoftwareUpdate {
  current_version: string
  version: string
  notes?: string | null
  published_at?: string | null
}

export interface SoftwareUpdateProgress {
  phase: 'downloading' | 'verifying' | 'installing' | 'ready_to_restart'
  downloaded_bytes: number
  total_bytes?: number | null
  percent?: number | null
}

export const CLOUD_DEVICE_ID_KEY = 'memory-bread_cloud_device_id'
export const CLOUD_DEVICE_PUBLIC_KEY = 'memory-bread_cloud_device_public_key'
export const CLOUD_DEVICE_REPORT_STATUS_KEY = 'memory-bread_cloud_device_report_status'
export const SOFTWARE_UPDATE_SNOOZE_KEY = 'memory-bread_software_update_snooze'
export const SOFTWARE_UPDATE_COHORT_KEY = 'memory-bread_software_update_cohort'
export const SOFTWARE_UPDATE_REQUEST_EVENT = 'memory-bread:software-update-requested'
const SOFTWARE_UPDATE_SNOOZE_MS = 24 * 60 * 60 * 1000

export interface CloudDeviceRegistrationScope {
  environment: ServiceEnvironment
  userId: string
}

interface CloudDeviceReportStatus {
  status: 'success' | 'error'
  attempted_at: string
  client_version: string
  error_code?: string
}

const scopedStorageKey = (
  baseKey: string,
  scope: CloudDeviceRegistrationScope,
): string => `${baseKey}:${scope.environment}:${encodeURIComponent(scope.userId)}`

export const cloudDeviceStorageKey = (scope: CloudDeviceRegistrationScope): string =>
  scopedStorageKey(CLOUD_DEVICE_ID_KEY, scope)

export const cloudDevicePublicKeyStorageKey = (scope: CloudDeviceRegistrationScope): string =>
  scopedStorageKey(CLOUD_DEVICE_PUBLIC_KEY, scope)

export const cloudDeviceReportStatusStorageKey = (scope: CloudDeviceRegistrationScope): string =>
  scopedStorageKey(CLOUD_DEVICE_REPORT_STATUS_KEY, scope)

const writeDeviceReportStatus = (
  scope: CloudDeviceRegistrationScope,
  status: CloudDeviceReportStatus,
): void => {
  window.localStorage.setItem(cloudDeviceReportStatusStorageKey(scope), JSON.stringify(status))
}

export function getSoftwareUpdateCohort(): string {
  const stored = window.localStorage.getItem(SOFTWARE_UPDATE_COHORT_KEY)?.trim().toLowerCase()
  if (stored && /^[0-9a-f]{64}$/.test(stored)) return stored
  const bytes = new Uint8Array(32)
  if (typeof crypto !== 'undefined') {
    crypto.getRandomValues(bytes)
  } else {
    bytes.forEach((_, index) => {
      bytes[index] = Math.floor(Math.random() * 256)
    })
  }
  const cohort = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  window.localStorage.setItem(SOFTWARE_UPDATE_COHORT_KEY, cohort)
  return cohort
}

const randomUuid = () => {
  const cryptoApi = typeof crypto !== 'undefined' ? crypto : null
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()
  const bytes = new Uint8Array(16)
  if (cryptoApi) {
    cryptoApi.getRandomValues(bytes)
  } else {
    bytes.forEach((_, index) => {
      bytes[index] = Math.floor(Math.random() * 256)
    })
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const randomBase64 = () => {
  const bytes = new Uint8Array(32)
  if (typeof crypto !== 'undefined') {
    crypto.getRandomValues(bytes)
  } else {
    bytes.forEach((_, index) => {
      bytes[index] = Math.floor(Math.random() * 256)
    })
  }
  return btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))
}

export async function fetchSoftwareUpdate(
  adminApiBaseUrl: string,
  metadata: AppMetadata,
  channel = 'stable',
  signal?: AbortSignal,
): Promise<SoftwareUpdateCheck> {
  const query = new URLSearchParams({
    current_version: metadata.version,
    platform: metadata.platform,
    architecture: metadata.architecture,
    channel,
    distribution: metadata.distribution,
  })
  const response = await fetch(`${adminApiBaseUrl}/v1/software-updates/check?${query}`, {
    headers: {
      ...serviceEnvironmentHeaders(),
      'X-MemoryBread-Update-Cohort': getSoftwareUpdateCohort(),
    },
    ...(signal ? { signal } : {}),
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(payload?.error?.message || '暂时无法检查更新，请稍后重试')
  }
  return payload.data as SoftwareUpdateCheck
}

export async function prepareSoftwareUpdate(
  adminApiBaseUrl: string,
  metadata: AppMetadata,
  environment: 'production' | 'staging',
  channel = 'stable',
): Promise<PreparedSoftwareUpdate | null> {
  if (!metadata.update_supported || metadata.distribution !== 'direct') {
    return null
  }
  const manifestUrl = new URL(
    `/v1/software-updates/manifest/${encodeURIComponent(metadata.platform)}/${encodeURIComponent(metadata.architecture)}/${encodeURIComponent(metadata.version)}`,
    `${adminApiBaseUrl.replace(/\/+$/, '')}/`,
  )
  manifestUrl.searchParams.set('channel', channel)
  return invoke<PreparedSoftwareUpdate | null>('prepare_software_update', {
    manifestUrl: manifestUrl.toString(),
    environment,
    cohort: getSoftwareUpdateCohort(),
  })
}

export const downloadAndInstallSoftwareUpdate = (): Promise<void> =>
  invoke('download_and_install_software_update')

export const restartApplication = (): Promise<void> => invoke('restart_application')

export function requestSoftwareUpdate(update: SoftwareUpdateCheck): void {
  window.dispatchEvent(new CustomEvent<SoftwareUpdateCheck>(SOFTWARE_UPDATE_REQUEST_EVENT, {
    detail: update,
  }))
}

export async function registerCurrentDevice(
  adminApiBaseUrl: string,
  authToken: string,
  scope: CloudDeviceRegistrationScope,
  signal?: AbortSignal,
): Promise<CloudDevice> {
  const metadata = await getAppMetadata()
  const deviceIdKey = cloudDeviceStorageKey(scope)
  const publicKeyKey = cloudDevicePublicKeyStorageKey(scope)
  let deviceId = window.localStorage.getItem(deviceIdKey)
    || window.localStorage.getItem(CLOUD_DEVICE_ID_KEY)
  if (!deviceId) {
    deviceId = randomUuid()
  }
  let publicKey = window.localStorage.getItem(publicKeyKey)
    || window.localStorage.getItem(CLOUD_DEVICE_PUBLIC_KEY)
  if (!publicKey) {
    publicKey = randomBase64()
  }
  // 先持久化本次候选身份，避免服务端已写入但客户端丢失响应时，
  // 下次重试生成新 ID 而留下重复设备。归属冲突时会在重试前覆盖为新身份。
  window.localStorage.setItem(deviceIdKey, deviceId)
  window.localStorage.setItem(publicKeyKey, publicKey)

  const upsert = (nextDeviceId: string, nextPublicKey: string) => upsertCloudDevice(
    adminApiBaseUrl,
    authToken,
    {
      device_id: nextDeviceId,
      name: `${metadata.product_name} ${metadata.platform}`,
      platform: metadata.platform,
      client_version: metadata.version,
      public_key_base64: nextPublicKey,
    },
    signal,
    scope.environment,
  )

  try {
    let device: CloudDevice
    try {
      device = await upsert(deviceId, publicKey)
    } catch (error) {
      if (cloudApiErrorCode(error) !== 'DEVICE_OWNERSHIP_CONFLICT') throw error
      deviceId = randomUuid()
      publicKey = randomBase64()
      window.localStorage.setItem(deviceIdKey, deviceId)
      window.localStorage.setItem(publicKeyKey, publicKey)
      device = await upsert(deviceId, publicKey)
    }
    window.localStorage.setItem(deviceIdKey, device.id)
    window.localStorage.setItem(publicKeyKey, publicKey)
    writeDeviceReportStatus(scope, {
      status: 'success',
      attempted_at: new Date().toISOString(),
      client_version: metadata.version,
    })
    return device
  } catch (error) {
    writeDeviceReportStatus(scope, {
      status: 'error',
      attempted_at: new Date().toISOString(),
      client_version: metadata.version,
      error_code: cloudApiErrorCode(error) || 'DEVICE_REPORT_FAILED',
    })
    throw error
  }
}

export function shouldShowSoftwareUpdate(update: SoftwareUpdateCheck): boolean {
  if (!update.update_available || !update.release) return false
  if (update.is_mandatory) return true
  try {
    const raw = window.localStorage.getItem(SOFTWARE_UPDATE_SNOOZE_KEY)
    if (!raw) return true
    const value = JSON.parse(raw) as { version?: string; until?: number }
    return value.version !== update.latest_version || Number(value.until || 0) <= Date.now()
  } catch {
    return true
  }
}

export function snoozeSoftwareUpdate(version: string): void {
  window.localStorage.setItem(SOFTWARE_UPDATE_SNOOZE_KEY, JSON.stringify({
    version,
    until: Date.now() + SOFTWARE_UPDATE_SNOOZE_MS,
  }))
}
