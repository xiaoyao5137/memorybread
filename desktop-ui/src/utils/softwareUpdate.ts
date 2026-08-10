import type { CloudDevice } from '../types'
import { invoke } from '@tauri-apps/api/core'
import { serviceEnvironmentHeaders } from '../store/useAppStore'
import { upsertCloudDevice } from './authApi'
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
export const SOFTWARE_UPDATE_SNOOZE_KEY = 'memory-bread_software_update_snooze'
export const SOFTWARE_UPDATE_COHORT_KEY = 'memory-bread_software_update_cohort'
export const SOFTWARE_UPDATE_REQUEST_EVENT = 'memory-bread:software-update-requested'
const SOFTWARE_UPDATE_SNOOZE_MS = 24 * 60 * 60 * 1000

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
  signal?: AbortSignal,
): Promise<CloudDevice> {
  const metadata = await getAppMetadata()
  let deviceId = window.localStorage.getItem(CLOUD_DEVICE_ID_KEY)
  if (!deviceId) {
    deviceId = randomUuid()
    window.localStorage.setItem(CLOUD_DEVICE_ID_KEY, deviceId)
  }
  let publicKey = window.localStorage.getItem(CLOUD_DEVICE_PUBLIC_KEY)
  if (!publicKey) {
    publicKey = randomBase64()
    window.localStorage.setItem(CLOUD_DEVICE_PUBLIC_KEY, publicKey)
  }
  const device = await upsertCloudDevice(adminApiBaseUrl, authToken, {
    device_id: deviceId,
    name: `${metadata.product_name} ${metadata.platform}`,
    platform: metadata.platform,
    client_version: metadata.version,
    public_key_base64: publicKey,
  }, signal)
  window.localStorage.setItem(CLOUD_DEVICE_ID_KEY, device.id)
  return device
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
