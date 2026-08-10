import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'

export interface AppMetadata {
  product_name: string
  version: string
  build_number: string
  platform: 'macos' | 'windows' | 'linux' | string
  architecture: 'universal' | 'aarch64' | 'x86_64' | string
  distribution: 'direct' | 'app_store'
  update_supported: boolean
}

const browserPlatform = (): AppMetadata['platform'] => {
  const platform = typeof navigator === 'undefined' ? '' : navigator.platform.toLowerCase()
  if (platform.includes('mac')) return 'macos'
  if (platform.includes('win')) return 'windows'
  if (platform.includes('linux')) return 'linux'
  return 'macos'
}

export const FALLBACK_APP_METADATA: AppMetadata = {
  product_name: '记忆面包',
  version: typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.1.0',
  build_number: '1',
  platform: browserPlatform(),
  architecture: 'universal',
  distribution: 'direct',
  update_supported: false,
}

let metadataPromise: Promise<AppMetadata> | null = null

export const getAppMetadata = (): Promise<AppMetadata> => {
  if (!metadataPromise) {
    metadataPromise = invoke<AppMetadata>('get_app_metadata')
      .then((metadata) => ({
        ...FALLBACK_APP_METADATA,
        ...metadata,
      }))
      .catch(() => FALLBACK_APP_METADATA)
  }
  return metadataPromise
}

export const useAppMetadata = (): AppMetadata => {
  const [metadata, setMetadata] = useState(FALLBACK_APP_METADATA)

  useEffect(() => {
    let cancelled = false
    void getAppMetadata().then((value) => {
      if (!cancelled) setMetadata(value)
    })
    return () => {
      cancelled = true
    }
  }, [])

  return metadata
}

export const openExternalUrl = async (value: string): Promise<void> => {
  const url = new URL(value)
  if (url.protocol !== 'https:') throw new Error('下载地址必须使用 HTTPS')
  try {
    await invoke('open_external_url', { url: url.toString() })
  } catch {
    const opened = window.open(url.toString(), '_blank', 'noopener,noreferrer')
    if (!opened) throw new Error('无法打开下载页面，请检查系统浏览器设置')
  }
}
