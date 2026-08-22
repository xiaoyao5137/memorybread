import { invoke } from '@tauri-apps/api/core'

export interface BrowserExtensionRuntimeStatus {
  schema_version: string
  connected: boolean
  extension_version?: string | null
  last_seen_at?: number | null
  active_job_count: number
  queued_job_count: number
  jobs?: BrowserLiveJob[]
}

export interface BrowserLiveJob {
  browser_job_id: string
  url: string
  title: string
  status: 'queued' | 'running' | 'completed' | 'failed' | string
  stage: string
  updated_at: number
  has_preview: boolean
  preview_revision: number
}

export interface BrowserExtensionInstallStatus {
  supported: boolean
  extensionId: string
  extensionDirectory?: string | null
  storeUrl?: string | null
  nativeHostRegistered: boolean
  bridgeAvailable: boolean
}

export interface BrowserIntegrationStatus {
  runtime: BrowserExtensionRuntimeStatus
  install: BrowserExtensionInstallStatus
}

const unavailableInstallStatus: BrowserExtensionInstallStatus = {
  supported: false,
  extensionId: '',
  extensionDirectory: null,
  storeUrl: null,
  nativeHostRegistered: false,
  bridgeAvailable: false,
}

export async function getBrowserIntegrationStatus(apiBaseUrl: string): Promise<BrowserIntegrationStatus> {
  const runtimePromise = fetch(`${apiBaseUrl}/api/browser-integration/status`)
    .then(async response => {
      if (!response.ok) throw new Error(`浏览器集成状态读取失败: ${response.status}`)
      return response.json() as Promise<BrowserExtensionRuntimeStatus>
    })
  const installPromise = invoke<BrowserExtensionInstallStatus>(
    'get_chrome_browser_integration_install_status',
  ).catch(() => unavailableInstallStatus)
  const [runtime, install] = await Promise.all([runtimePromise, installPromise])
  return { runtime, install }
}

export const prepareBrowserIntegration = () => (
  invoke<BrowserExtensionInstallStatus>('prepare_chrome_browser_integration')
)
