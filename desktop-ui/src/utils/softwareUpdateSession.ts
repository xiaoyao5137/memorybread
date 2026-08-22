import { create } from 'zustand'
import { listen } from '@tauri-apps/api/event'
import type { AppMetadata } from './appMetadata'
import {
  downloadAndInstallSoftwareUpdate,
  prepareSoftwareUpdate,
  restartApplication,
  type SoftwareUpdateProgress,
} from './softwareUpdate'

/**
 * 软件更新执行会话。
 *
 * 下载、校验、安装都在 Rust 侧执行，弹窗只是进度的展示层；
 * 会话状态提升到全局 store 后，非强制更新允许用户把弹窗切到后台，
 * 左下角入口继续显示进度，完成后引导重启。
 */
export type SoftwareUpdateSessionPhase =
  | 'idle'
  | 'preparing'
  | SoftwareUpdateProgress['phase']
  | 'failed'

interface SoftwareUpdateSessionState {
  version: string | null
  phase: SoftwareUpdateSessionPhase
  progress: SoftwareUpdateProgress | null
  error: string
}

export const useSoftwareUpdateSession = create<SoftwareUpdateSessionState>(() => ({
  version: null,
  phase: 'idle',
  progress: null,
  error: '',
}))

export const softwareUpdateSessionBusy = (phase: SoftwareUpdateSessionPhase): boolean =>
  phase !== 'idle' && phase !== 'ready_to_restart' && phase !== 'failed'

let progressListenerRegistered = false

const ensureProgressListener = (): void => {
  if (progressListenerRegistered) return
  progressListenerRegistered = true
  void listen<SoftwareUpdateProgress>('software-update-progress', event => {
    useSoftwareUpdateSession.setState({
      progress: event.payload,
      phase: event.payload.phase,
    })
  }).catch(() => {
    // 浏览器设计预览没有 Tauri event bridge；正式桌面端会注册原生进度事件。
  })
}

export interface StartSoftwareUpdateSessionOptions {
  adminApiBaseUrl: string
  metadata: AppMetadata
  environment: 'production' | 'staging'
  channel: string
  version: string
}

export async function startSoftwareUpdateSession(
  options: StartSoftwareUpdateSessionOptions,
): Promise<void> {
  const current = useSoftwareUpdateSession.getState()
  if (softwareUpdateSessionBusy(current.phase) || current.phase === 'ready_to_restart') return
  ensureProgressListener()
  useSoftwareUpdateSession.setState({
    version: options.version,
    phase: 'preparing',
    progress: null,
    error: '',
  })
  try {
    const prepared = await prepareSoftwareUpdate(
      options.adminApiBaseUrl,
      options.metadata,
      options.environment,
      options.channel,
    )
    if (!prepared) throw new Error('此更新已暂停或不再适用于当前设备，请重新检查')
    if (prepared.version !== options.version) throw new Error('版本清单已变化，请重新检查后再更新')
    useSoftwareUpdateSession.setState({ phase: 'downloading' })
    await downloadAndInstallSoftwareUpdate()
    useSoftwareUpdateSession.setState({ phase: 'ready_to_restart' })
  } catch (cause) {
    useSoftwareUpdateSession.setState({
      phase: 'failed',
      error: cause instanceof Error ? cause.message : String(cause || '软件更新失败'),
    })
  }
}

export async function restartForSoftwareUpdate(): Promise<void> {
  await restartApplication()
}

export function resetSoftwareUpdateSession(): void {
  useSoftwareUpdateSession.setState({ version: null, phase: 'idle', progress: null, error: '' })
}
