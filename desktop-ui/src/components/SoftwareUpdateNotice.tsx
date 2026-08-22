import React, { useEffect, useMemo, useState } from 'react'
import { Download, RefreshCw, Sparkles, Store, X } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { openExternalUrl, useAppMetadata } from '../utils/appMetadata'
import {
  type SoftwareUpdateCheck,
  type SoftwareUpdateProgress,
} from '../utils/softwareUpdate'
import {
  restartForSoftwareUpdate,
  startSoftwareUpdateSession,
  useSoftwareUpdateSession,
} from '../utils/softwareUpdateSession'
import './SoftwareUpdateNotice.css'

interface SoftwareUpdateNoticeProps {
  update: SoftwareUpdateCheck
  onDismiss: () => void
}

type UpdatePhase = 'idle' | 'preparing' | SoftwareUpdateProgress['phase'] | 'failed'

const SoftwareUpdateNotice: React.FC<SoftwareUpdateNoticeProps> = ({ update, onDismiss }) => {
  const { adminApiBaseUrl, serviceEnvironment } = useAppStore()
  const metadata = useAppMetadata()
  const session = useSoftwareUpdateSession()
  // 外部跳转（App Store / 无更新公钥的测试构建）没有后台会话，错误留在弹窗本地。
  const [externalError, setExternalError] = useState('')
  const release = update.release
  const isDirectFlow = release?.distribution === 'direct' && metadata.update_supported
  const sessionActive = isDirectFlow && session.version === release?.version
  const phase: UpdatePhase = sessionActive ? session.phase : 'idle'
  const progress = sessionActive ? session.progress : null
  const isBusy = phase !== 'idle' && phase !== 'ready_to_restart' && phase !== 'failed'
  // 非强制更新允许随时关闭；下载在后台继续，进度由左下角入口承接。
  const canDismiss = !update.is_mandatory

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
    if (release.distribution === 'app_store' || !metadata.update_supported) {
      setExternalError('')
      try {
        await openExternalUrl(release.download_url)
      } catch (cause) {
        setExternalError(cause instanceof Error ? cause.message : '无法打开 App Store 页面')
      }
      return
    }
    if (phase === 'ready_to_restart') {
      await restartForSoftwareUpdate().catch(() => {
        useSoftwareUpdateSession.setState({ error: '暂时无法重启应用' })
      })
      return
    }
    void startSoftwareUpdateSession({
      adminApiBaseUrl,
      metadata,
      environment: serviceEnvironment,
      channel: release.channel,
      version: release.version,
    })
  }

  const error = sessionActive ? session.error : externalError

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
          <Sparkles size={30} />
        </div>
        {canDismiss && (
          <button aria-label={isBusy ? '后台继续更新' : '稍后提醒'} className="software-update-notice__close" onClick={onDismiss} type="button">
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
        {(release.distribution === 'app_store' || !metadata.update_supported) && (
          <div className="software-update-notice__trust">
            {release.distribution === 'app_store'
              ? <><Store size={17} aria-hidden /><span>此安装来自 Mac App Store，更新将由 App Store 完成。</span></>
              : <><Download size={17} aria-hidden /><span>当前是未配置更新公钥的测试构建，请下载安装正式发布包。</span></>}
          </div>
        )}
        {phase !== 'idle' && phase !== 'failed' && release.distribution === 'direct' && (
          <div className="software-update-notice__progress" role="status" aria-live="polite">
            <div><span>{actionLabel}</span><strong>{progress?.percent != null ? `${progress.percent}%` : ''}</strong></div>
            <span className="software-update-notice__progress-track"><i style={{ width: `${progress?.percent ?? (phase === 'preparing' ? 6 : 100)}%` }} /></span>
          </div>
        )}
        {error && <div className="software-update-notice__error" role="alert">{error}</div>}
        <div className="software-update-notice__actions">
          {canDismiss && <button onClick={onDismiss} type="button">{isBusy ? '后台继续更新' : '24 小时后提醒'}</button>}
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
