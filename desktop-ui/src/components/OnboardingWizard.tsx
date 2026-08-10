import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useAppStore } from '../store/useAppStore'
import {
  disableInitializationTestMode,
  fetchInitializationReport,
  fetchInitializationStatus,
  initializationIsReady,
  startInitialization,
  type InitializationStage,
  type InitializationStatus,
} from '../utils/initialization'
import { useConfirmDialog } from './useConfirmDialog'
import './OnboardingWizard.css'

const STATUS_POLL_MS = 1_000
const SIDECAR_RETRY_MS = 1_500
const MAX_SIDECAR_RETRIES = 12
const ESTIMATED_INITIALIZATION_MS = 20 * 60 * 1_000
const MAX_ESTIMATED_REMAINING_MS = 30 * 60 * 1_000

interface OnboardingWizardProps {
  onStatusValidated?: (ready: boolean) => void
}

function stageMark(stage: InitializationStage) {
  if (stage.status === 'succeeded' || stage.status === 'skipped') return '✓'
  if (stage.status === 'failed') return '!'
  if (stage.status === 'running') return '•'
  return ''
}

function stageStatusLabel(stage: InitializationStage) {
  if (stage.status === 'skipped') return '已存在'
  if (stage.status === 'succeeded') return '已完成'
  if (stage.status === 'failed') return '失败'
  if (stage.status === 'running') return `${Math.max(0, Math.min(100, stage.progress || 0))}%`
  return '等待'
}

function timestampMs(value?: string | null) {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function formatElapsedTime(durationMs: number) {
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1_000))
  const hours = Math.floor(totalSeconds / 3_600)
  const minutes = Math.floor((totalSeconds % 3_600) / 60)
  const seconds = totalSeconds % 60
  const clock = [minutes, seconds].map(value => String(value).padStart(2, '0')).join(':')
  return hours > 0 ? `${String(hours).padStart(2, '0')}:${clock}` : clock
}

function estimateRemainingTime(progress: number, elapsedMs: number) {
  if (progress >= 100) return 0
  const remainingRatio = Math.max(0, 100 - progress) / 100
  const baseline = ESTIMATED_INITIALIZATION_MS * remainingRatio
  const observed = progress > 0 && elapsedMs >= 30_000
    ? elapsedMs * (100 - progress) / progress
    : 0
  return Math.min(MAX_ESTIMATED_REMAINING_MS, Math.max(baseline, observed))
}

function formatRemainingTime(durationMs: number) {
  if (durationMs <= 0) return '即将完成'
  const minutes = Math.ceil(durationMs / 60_000)
  if (minutes <= 1) return '不足 1 分钟'
  if (minutes < 60) return `约 ${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  return remainder > 0 ? `约 ${hours} 小时 ${remainder} 分钟` : `约 ${hours} 小时`
}

function notifyInitializationComplete() {
  if (typeof Notification === 'undefined') return
  const show = () => new Notification('记忆面包初始化完成', {
    body: '本地 AI、记忆库与核心功能均已通过质检，可以开始使用。',
  })
  try {
    if (Notification.permission === 'granted') {
      show()
    } else if (Notification.permission === 'default') {
      void Notification.requestPermission().then(permission => {
        if (permission === 'granted') show()
      })
    }
  } catch {
    // 系统通知是完成后的附加能力，不影响已经通过的初始化结果。
  }
}

const OnboardingWizard: React.FC<OnboardingWizardProps> = ({ onStatusValidated }) => {
  const {
    adminApiBaseUrl,
    authToken,
    serviceEnvironment,
    setHasCompletedSetup,
    setWindowMode,
  } = useAppStore()
  const { confirm: confirmDestructive, dialog: confirmDialog } = useConfirmDialog()
  const [status, setStatus] = useState<InitializationStatus | null>(null)
  const [connecting, setConnecting] = useState(true)
  const [connectionError, setConnectionError] = useState('')
  const [actionError, setActionError] = useState('')
  const [starting, setStarting] = useState(false)
  const [reporting, setReporting] = useState(false)
  const [reportId, setReportId] = useState('')
  const [leavingSandbox, setLeavingSandbox] = useState(false)
  const [sandboxExitConfirming, setSandboxExitConfirming] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())
  const completedRunRef = useRef<string | null>(null)
  const previousStateRef = useRef<string | null>(null)

  const applyStatus = useCallback((next: InitializationStatus) => {
    setStatus(next)
    setConnecting(false)
    setConnectionError('')
    const ready = initializationIsReady(next)
    onStatusValidated?.(ready)
    if (ready) {
      setHasCompletedSetup(true)
      if (previousStateRef.current === 'running') setWindowMode('rag')
    } else if (next.test_mode_enabled || next.state !== 'completed') {
      setHasCompletedSetup(false)
    }
    if (
      next.state === 'completed'
      && previousStateRef.current === 'running'
      && completedRunRef.current !== next.run_id
    ) {
      completedRunRef.current = next.run_id || 'completed'
      notifyInitializationComplete()
    }
    previousStateRef.current = next.state
  }, [onStatusValidated, setHasCompletedSetup, setWindowMode])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let attempts = 0

    const refresh = async () => {
      try {
        const next = await fetchInitializationStatus()
        if (cancelled) return
        applyStatus(next)
        attempts = 0
      } catch (error) {
        if (cancelled) return
        attempts += 1
        const errorCode = error && typeof error === 'object' && 'code' in error
          ? String(error.code || '')
          : ''
        if (attempts < MAX_SIDECAR_RETRIES) {
          if (errorCode === 'INITIALIZATION_API_UNAVAILABLE') {
            setConnecting(false)
            setConnectionError(error instanceof Error ? error.message : '本地初始化服务版本较旧')
          } else {
            setConnecting(true)
          }
          timer = setTimeout(refresh, SIDECAR_RETRY_MS)
        } else {
          setConnecting(false)
          setConnectionError(error instanceof Error ? error.message : '本地初始化服务暂时不可用')
        }
      }
    }

    void refresh()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [applyStatus])

  useEffect(() => {
    if (!status || leavingSandbox) return
    const timer = window.setInterval(() => {
      void fetchInitializationStatus().then(applyStatus).catch(() => {})
    }, STATUS_POLL_MS)
    return () => window.clearInterval(timer)
  }, [applyStatus, leavingSandbox, status?.run_id, status?.state])

  useEffect(() => {
    if (status?.state !== 'running') return
    setNowMs(Date.now())
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [status?.run_id, status?.state])

  useEffect(() => {
    if (!sandboxExitConfirming) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !leavingSandbox) setSandboxExitConfirming(false)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [leavingSandbox, sandboxExitConfirming])

  const begin = async () => {
    setStarting(true)
    setActionError('')
    setReportId('')
    try {
      const next = await startInitialization(status?.test_mode_enabled ? 'sandbox' : 'normal')
      applyStatus(next)
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '初始化任务启动失败')
    } finally {
      setStarting(false)
    }
  }

  const report = async () => {
    const confirmed = await confirmDestructive({
      title: '确认上报诊断信息？',
      description: '将上报应用版本、系统版本、硬件档位、失败阶段和稳定错误码。不会上报截图、知识内容、提示词、回答、文件路径或密钥。',
      confirmLabel: '确认上报',
      danger: false,
    })
    if (!confirmed) return
    setReporting(true)
    setActionError('')
    try {
      const bundle = await fetchInitializationReport()
      const runId = String(bundle.run_id || status?.run_id || Date.now())
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        'Idempotency-Key': runId,
        'X-MemoryBread-Environment': serviceEnvironment,
      }
      if (authToken) headers.Authorization = `Bearer ${authToken}`
      const response = await fetch(`${adminApiBaseUrl}/v1/initialization-reports`, {
        method: 'POST',
        headers,
        body: JSON.stringify(bundle),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data?.error?.message || '诊断上报失败')
      setReportId(data?.data?.report_id || '已接收')
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '诊断上报失败')
    } finally {
      setReporting(false)
    }
  }

  const leaveSandbox = async () => {
    setLeavingSandbox(true)
    setActionError('')
    try {
      const normal = await disableInitializationTestMode()
      setSandboxExitConfirming(false)
      setHasCompletedSetup(initializationIsReady(normal))
      onStatusValidated?.(initializationIsReady(normal))
      setWindowMode('debug')
      setStatus(normal)
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '关闭初始化测试模式失败')
    } finally {
      setLeavingSandbox(false)
    }
  }

  const retryConnection = () => {
    setConnecting(true)
    setConnectionError('')
    void fetchInitializationStatus().then(applyStatus).catch(error => {
      setConnecting(false)
      setConnectionError(error instanceof Error ? error.message : '本地初始化服务暂时不可用')
    })
  }

  const running = status?.state === 'running'
  const failed = status?.state === 'failed' || status?.state === 'interrupted'
  const sandboxFinished = status?.test_mode_enabled && ['completed', 'failed'].includes(status.state)
  const showPreparationGuide = !failed && !connectionError && status?.state !== 'completed'
  const progress = Math.max(0, Math.min(100, status?.progress || 0))
  const currentStage = status?.stages.find(stage => stage.id === status.current_stage)
  const startedAtMs = timestampMs(status?.started_at)
  const finishedAtMs = timestampMs(status?.finished_at)
  const recordedDurationMs = (status?.stages || []).reduce(
    (total, stage) => total + Math.max(0, stage.duration_ms || 0),
    0,
  )
  const elapsedMs = startedAtMs === null
    ? recordedDurationMs
    : Math.max(0, (running ? nowMs : finishedAtMs || nowMs) - startedAtMs)
  const remainingMs = running ? estimateRemainingTime(progress, elapsedMs) : 0

  return (
    <main className="initialization-gate" data-testid="initialization-gate">
      <div className="initialization-grain" aria-hidden />
      <section
        className="initialization-card"
        aria-labelledby="initialization-title"
        data-testid="initialization-card"
      >
        <header className="initialization-header">
          <div className="brand-lockup">
            <span className="brand-loaf" aria-hidden><i /><i /><i /></span>
            <span>
              <strong>记忆面包</strong>
              <small>首次使用准备</small>
            </span>
          </div>
          {running && <span className="background-note">可以最小化，初始化会在后台继续</span>}
        </header>

        <div className="initialization-body">
          <section className="initialization-story">
            <p className="initialization-eyebrow">ONE-CLICK LOCAL SETUP</p>
            <h1 id="initialization-title">
              {running
                ? '面包烘焙中'
                : failed
                  ? '有一项准备没有完成'
                  : status?.state === 'completed'
                    ? '面包烘焙完成'
                    : '烤面包'}
            </h1>
            <p className="initialization-copy">
              {running
                ? currentStage?.detail || status?.message
                : failed
                  ? status?.suggestion || '可以重试，已经安装好的内容会自动跳过。'
                  : '将自动准备本地 AI、采集提炼模型、语义检索、记忆库、技能与工具，并完成采集、提炼、咨询和创作测试。'}
            </p>

            {showPreparationGuide && (
              <section className="initialization-preparation" aria-label="初始化预计用时与资源占用">
                <p>
                  {running
                    ? '烘焙时间较长，请耐心等待。可以最小化软件，请保持网络畅通。'
                    : '可以最小化软件等待初始化完成，请保持网络畅通。'}
                </p>
                <dl>
                  <div>
                    <dt>预计用时</dt>
                    <dd>10–30 分钟</dd>
                  </div>
                  <div>
                    <dt>新增硬盘占用</dt>
                    <dd>约 4 GB</dd>
                  </div>
                  <div>
                    <dt>运行内存</dt>
                    <dd>约 6 GB</dd>
                  </div>
                </dl>
                <small>请至少预留 6 GB 磁盘空间；实际用时与占用受网络、设备配置和本机已有组件影响。</small>
              </section>
            )}

            {connecting && (
              <div className="initialization-connection" role="status">
                <span className="connection-pulse" aria-hidden />
                正在连接本地初始化服务…
              </div>
            )}
            {connectionError && (
              <div className="initialization-error" role="alert">
                <strong>本地服务尚未就绪</strong>
                <span>{connectionError}</span>
                <button type="button" onClick={retryConnection}>重新连接</button>
              </div>
            )}

            {!connecting && !connectionError && !running && !failed && status?.state !== 'completed' && (
              <button className="initialization-primary" type="button" disabled={starting} onClick={() => void begin()}>
                <span>{starting ? '正在启动…' : failed ? '重新初始化' : '初始化'}</span>
                <small>已存在的组件不会重复安装</small>
              </button>
            )}

            {failed && status?.can_retry && (
              <div className="initialization-actions">
                <button className="initialization-primary" type="button" disabled={starting} onClick={() => void begin()}>
                  {starting ? '正在启动…' : status.state === 'interrupted' ? '恢复初始化' : '重试失败阶段'}
                </button>
                {status.can_report && (
                  <button className="initialization-secondary" type="button" disabled={reporting} onClick={() => void report()}>
                    {reporting ? '正在上报…' : '上报诊断'}
                  </button>
                )}
              </div>
            )}
            {failed && status?.error_code && (
              <p className="initialization-error-code">
                错误码 <code>{status.error_code}</code>
              </p>
            )}
            {reportId && <p className="report-success" role="status">上报成功，编号 {reportId}</p>}
            {actionError && <p className="action-error" role="alert">{actionError}</p>}
          </section>

          <section className="initialization-progress-panel" aria-label="初始化进度">
            <div
              className={`loaf-progress ${running ? 'loaf-progress--active' : ''}`}
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress}
              aria-label={`初始化进度 ${progress}%`}
              style={{ '--progress': `${progress * 3.6}deg` } as React.CSSProperties}
            >
              <div className="loaf-progress__center">
                <strong>{progress}</strong>
                <span>%</span>
              </div>
              <i className="loaf-score loaf-score--one" aria-hidden />
              <i className="loaf-score loaf-score--two" aria-hidden />
              <i className="loaf-score loaf-score--three" aria-hidden />
            </div>
            <div className="current-stage">
              <span>{running ? '当前阶段' : status?.state === 'completed' ? '质检结果' : '准备状态'}</span>
              <strong>{currentStage?.label || status?.message || '等待初始化'}</strong>
            </div>
            {(running || status?.state === 'completed') && (
              <dl className="initialization-runtime" aria-label="初始化时间" aria-live="polite">
                <div>
                  <dt>执行时长</dt>
                  <dd>{formatElapsedTime(elapsedMs)}</dd>
                </div>
                {running && (
                  <div>
                    <dt>预计剩余时间</dt>
                    <dd>{formatRemainingTime(remainingMs)}</dd>
                  </div>
                )}
              </dl>
            )}
            <ol className="stage-list">
              {(status?.stages || []).map(stage => (
                <li className={`stage-item stage-item--${stage.status}`} key={stage.id}>
                  <span className="stage-mark" aria-hidden>{stageMark(stage)}</span>
                  <span className="stage-copy">
                    <strong>{stage.label}</strong>
                    {stage.status === 'running' && <small>{stage.detail}</small>}
                  </span>
                  <span className="stage-status">{stageStatusLabel(stage)}</span>
                </li>
              ))}
            </ol>
          </section>
        </div>
      </section>
      {sandboxFinished && (
        <aside className="initialization-test-controls" aria-label="初始化测试模式控制">
          <span>模拟初始化已结束</span>
          <button type="button" disabled={leavingSandbox} onClick={() => setSandboxExitConfirming(true)}>
            关闭初始化测试模式
          </button>
        </aside>
      )}
      {sandboxExitConfirming && (
        <div
          className="initialization-exit-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="initialization-exit-title"
          aria-describedby="initialization-exit-description"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !leavingSandbox) {
              setSandboxExitConfirming(false)
            }
          }}
        >
          <section className="initialization-exit-dialog">
            <h2 id="initialization-exit-title">关闭初始化测试模式</h2>
            <p id="initialization-exit-description">
              将停止隔离测试进程并清理临时运行时、模型和数据库，真实工作环境不会被修改。
            </p>
            {actionError && <p className="initialization-exit-error" role="alert">{actionError}</p>}
            <div className="initialization-exit-actions">
              <button
                type="button"
                disabled={leavingSandbox}
                onClick={() => setSandboxExitConfirming(false)}
                autoFocus
              >
                取消
              </button>
              <button
                className="initialization-exit-confirm"
                type="button"
                disabled={leavingSandbox}
                aria-busy={leavingSandbox}
                onClick={() => void leaveSandbox()}
              >
                {leavingSandbox ? '正在清理沙箱…' : '确认关闭'}
              </button>
            </div>
          </section>
        </div>
      )}
      {confirmDialog}
    </main>
  )
}

export default OnboardingWizard
