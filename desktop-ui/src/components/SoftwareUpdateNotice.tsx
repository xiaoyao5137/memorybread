import React, { useEffect, useMemo, useState } from 'react'
import { listen } from '@tauri-apps/api/event'
import { Download, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, Store, X } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { openExternalUrl, useAppMetadata } from '../utils/appMetadata'
import {
  downloadAndInstallSoftwareUpdate,
  prepareSoftwareUpdate,
  restartApplication,
  type SoftwareUpdateCheck,
  type SoftwareUpdateProgress,
} from '../utils/softwareUpdate'
import './SoftwareUpdateNotice.css'

interface SoftwareUpdateNoticeProps {
  update: SoftwareUpdateCheck
  onDismiss: () => void
}

type UpdatePhase = 'idle' | 'preparing' | SoftwareUpdateProgress['phase']

const SoftwareUpdateNotice: React.FC<SoftwareUpdateNoticeProps> = ({ update, onDismiss }) => {
  const { adminApiBaseUrl, serviceEnvironment } = useAppStore()
  const metadata = useAppMetadata()
  const [phase, setPhase] = useState<UpdatePhase>('idle')
  const [progress, setProgress] = useState<SoftwareUpdateProgress | null>(null)
  const [error, setError] = useState('')
  const release = update.release
  const isBusy = phase !== 'idle' && phase !== 'ready_to_restart'
  const canDismiss = !update.is_mandatory && !isBusy

  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | undefined
    void listen<SoftwareUpdateProgress>('software-update-progress', event => {
      if (disposed) return
      setProgress(event.payload)
      setPhase(event.payload.phase)
    }).then(cleanup => {
      if (disposed) cleanup()
      else unlisten = cleanup
    }).catch(() => {
      // 浏览器设计预览没有 Tauri event bridge；正式桌面端会注册原生进度事件。
    })
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && canDismiss) onDismiss()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [canDismiss, onDismiss])

  const actionLabel = useMemo(() => {
    if (release?.distribution === 'app_store') return '前往 App Store'
    if (!metadata.update_supported) return '下载正式安装包'
    if (phase === 'preparing') return '正在核对更新'
    if (phase === 'downloading') return `正在下载${progress?.percent != null ? ` ${progress.percent}%` : ''}`
    if (phase === 'verifying') return '正在校验签名'
    if (phase === 'installing') return '正在安装'
    if (phase === 'ready_to_restart') return '重启并完成更新'
    return '立即更新'
  }, [metadata.update_supported, phase, progress?.percent, release?.distribution])

  if (!release) return null

  const startUpdate = async () => {
    setError('')
    if (release.distribution === 'app_store' || !metadata.update_supported) {
      try {
        await openExternalUrl(release.download_url)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '无法打开 App Store 页面')
      }
      return
    }
    if (phase === 'ready_to_restart') {
      await restartApplication().catch(cause => {
        setError(cause instanceof Error ? cause.message : '暂时无法重启应用')
      })
      return
    }
    setPhase('preparing')
    try {
      const prepared = await prepareSoftwareUpdate(
        adminApiBaseUrl,
        metadata,
        serviceEnvironment,
        release.channel,
      )
      if (!prepared) throw new Error('此更新已暂停或不再适用于当前设备，请重新检查')
      if (prepared.version !== release.version) throw new Error('版本清单已变化，请重新检查后再更新')
      setPhase('downloading')
      await downloadAndInstallSoftwareUpdate()
      setPhase('ready_to_restart')
    } catch (cause) {
      setPhase('idle')
      setError(cause instanceof Error ? cause.message : String(cause || '软件更新失败'))
    }
  }

  return (
    <div className="software-update-notice" role="presentation">
      <section
        aria-describedby="software-update-description"
        aria-labelledby="software-update-title"
        aria-modal="true"
        className="software-update-notice__dialog"
        role="dialog"
      >
        <div className="software-update-notice__mark" aria-hidden>
          {update.is_mandatory ? <ShieldAlert size={30} /> : <Sparkles size={30} />}
        </div>
        {canDismiss && (
          <button aria-label="稍后提醒" className="software-update-notice__close" onClick={onDismiss} type="button">
            <X size={18} />
          </button>
        )}
        <div className="software-update-notice__eyebrow">
          {update.is_mandatory ? '重要软件更新' : '记忆面包有新版本'}
        </div>
        <h2 id="software-update-title">{release.title}</h2>
        <p id="software-update-description">
          当前版本 <code>v{update.current_version}</code>
          <span aria-hidden> → </span>
          最新版本 <code>v{update.latest_version}</code>
          <small>build {release.build_number}</small>
        </p>
        <div className="software-update-notice__notes">{release.release_notes}</div>
        <div className="software-update-notice__trust">
          {release.distribution === 'direct' && metadata.update_supported
            ? <><ShieldCheck size={17} aria-hidden /><span>更新包会先通过发布签名与 SHA-256 双重校验，再写入应用。</span></>
            : release.distribution === 'app_store'
              ? <><Store size={17} aria-hidden /><span>此安装来自 Mac App Store，更新将由 App Store 完成。</span></>
              : <><Download size={17} aria-hidden /><span>当前是未配置更新公钥的测试构建，请下载安装正式发布包。</span></>}
        </div>
        {phase !== 'idle' && release.distribution === 'direct' && (
          <div className="software-update-notice__progress" role="status" aria-live="polite">
            <div><span>{actionLabel}</span><strong>{progress?.percent != null ? `${progress.percent}%` : ''}</strong></div>
            <span className="software-update-notice__progress-track"><i style={{ width: `${progress?.percent ?? (phase === 'preparing' ? 6 : 100)}%` }} /></span>
          </div>
        )}
        {error && <div className="software-update-notice__error" role="alert">{error}</div>}
        <div className="software-update-notice__actions">
          {canDismiss && <button onClick={onDismiss} type="button">24 小时后提醒</button>}
          <button autoFocus className="software-update-notice__download" disabled={isBusy} onClick={() => void startUpdate()} type="button">
            {phase === 'ready_to_restart' ? <RefreshCw size={17} aria-hidden /> : release.distribution === 'app_store' ? <Store size={17} aria-hidden /> : <Download size={17} aria-hidden />}
            {actionLabel}
          </button>
        </div>
      </section>
    </div>
  )
}

export default SoftwareUpdateNotice
