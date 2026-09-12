import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useAppStore } from '../store/useAppStore'
import './PermissionPreparation.css'

export interface PermissionItem {
  id: string
  name: string
  description: string
  status: 'granted' | 'required' | 'unverified' | 'verified' | 'unsupported'
  message: string
  optional: boolean
}
const labels: Record<PermissionItem['status'], string> = {
  granted: '已开启', required: '待开启', unverified: '待设置', verified: '已开启', unsupported: '无需设置',
}

export default function PermissionPreparation({ variant = 'inline', active = true }: {
  sandbox?: boolean
  variant?: 'inline' | 'step'
  active?: boolean
}) {
  const [open, setOpen] = useState(false)
  const autoPresented = useRef(false)
  const dialogRef = useRef<HTMLDivElement>(null)
  const entryRef = useRef<HTMLButtonElement>(null)
  const enabled = active || open
  const apiBaseUrl = useAppStore(state => state.apiBaseUrl)
  const [items, setItems] = useState<PermissionItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const alive = useRef(false)
  const inFlight = useRef(false)
  const actionInFlight = useRef(false)
  const epoch = useRef(0)

  const refresh = useCallback(async () => {
    if (!enabled || inFlight.current || actionInFlight.current) return
    inFlight.current = true
    const version = epoch.current
    try {
      const response = await fetch(`${apiBaseUrl}/api/permissions`, { signal: AbortSignal.timeout(10_000) })
      if (!response.ok) throw new Error('权限检测服务尚未就绪，可稍后重试；不影响组件初始化。')
      const data = await response.json()
      if (!Array.isArray(data.items) || data.items.length === 0) throw new Error('权限检测未返回有效结果，请重试。')
      if (!alive.current || version !== epoch.current) return
      if (data.items.some((item: PermissionItem) => !item || !item.id || !(item.status in labels))) {
        throw new Error('权限检测返回无效状态，请重试。')
      }
      setItems(data.items)
      setError('')
    } catch (err) {
      if (alive.current && version === epoch.current) setError(err instanceof Error ? err.message : '权限检测失败，请重试。')
    } finally {
      inFlight.current = false
      if (alive.current) setLoading(false)
    }
  }, [apiBaseUrl, enabled])

  useEffect(() => {
    alive.current = true
    void refresh()
    const onFocus = () => {
      void refresh()
    }
    const onVisible = () => { if (document.visibilityState === 'visible') onFocus() }
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh() }, 5000)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive.current = false
      epoch.current += 1
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [refresh])

  const act = async (id: string) => {
    if (actionInFlight.current) return
    actionInFlight.current = true
    epoch.current += 1
    setBusy(id)
    setError('')
    try {
      const response = await fetch(`${apiBaseUrl}/api/permissions/${encodeURIComponent(id)}/request`, {
        method: 'POST', signal: AbortSignal.timeout(75_000),
      })
      if (!response.ok) throw new Error('未能完成权限操作，请重试。')
      const result: PermissionItem = await response.json()
      if (!result || result.id !== id || !(result.status in labels)) throw new Error('权限操作未返回有效结果，请重试。')
      if (!alive.current) return
      setItems(previous => previous.map(item => item.id === id ? result : item))
    } catch (err) {
      if (alive.current) setError(err instanceof Error ? err.message : '权限操作失败，请重试。')
    } finally {
      actionInFlight.current = false
      if (alive.current) { setBusy(''); void refresh() }
    }
  }
  const ready = items.length > 0 && !loading && !error
    && items.filter(item => !item.optional).every(item => ['granted', 'verified', 'unsupported'].includes(item.status))
  useEffect(() => {
    if (variant !== 'step' || !active || loading || error || items.length === 0) return
    if (ready) {
      autoPresented.current = true
      setOpen(false)
    } else if (!autoPresented.current) {
      autoPresented.current = true
      setOpen(true)
    }
  }, [variant, active, loading, ready, error, items.length])

  useEffect(() => {
    if (!open || variant !== 'step') return
    const previous = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    dialog?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); return }
      if (event.key !== 'Tab' || !dialog) return
      const controls = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), summary, [tabindex="0"]'))
        .filter(node => !node.closest('details:not([open])') || node.tagName === 'SUMMARY')
      const first = controls[0], last = controls[controls.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault(); last?.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (previous?.isConnected) previous.focus()
      else entryRef.current?.focus()
    }
  }, [open, variant])

  const permissionRow = (item: PermissionItem) => {
    const ready = ['granted', 'verified', 'unsupported'].includes(item.status)
    return <li key={item.id}>
      <div><strong>{item.name}</strong><span className={`permission-status permission-status--${item.status}`}>{labels[item.status]}</span></div>
      <p>{item.description}</p>
      {item.optional && item.message && <p className="permission-detail">{item.message}</p>}
      {!ready && <button type="button" disabled={!!busy} onClick={() => void act(item.id)}>
        {busy === item.id ? '请完成系统操作…' : '去开启'}
      </button>}
    </li>
  }
  const panel = <section className="permission-preparation" aria-labelledby="permission-title">
    <div className="permission-preparation__header"><h2 id="permission-title">开启采集权限</h2></div>
    {loading && <p role="status">正在检查系统权限…</p>}
    {error && <p className="permission-error" role="alert">{error} <button type="button" onClick={() => void refresh()}>重试</button></p>}
    {ready && <p className="permission-success" role="status">采集权限已开启</p>}
    <ul>{items.filter(item => !item.optional).map(permissionRow)}</ul>
    <details className="permission-optional">
      <summary>更多可选设置</summary>
      <ul>{items.filter(item => item.optional).map(permissionRow)}</ul>
      <p className="permission-detail">可以稍后在设置中继续开启，不影响咨询与创作。</p>
    </details>
  </section>
  if (variant === 'inline') return panel
  const stepStatus = !active ? 'pending' : ready ? 'succeeded' : 'running'
  return <>
    <li className={`stage-item stage-item--${stepStatus} permission-stage`} data-testid="permission-stage">
      <span className="stage-mark" aria-hidden>{ready && active ? '✓' : active ? '•' : ''}</span>
      <span className="stage-copy"><strong>开启采集权限</strong>
        {active && !ready && <small>{loading ? '正在检查系统权限' : error ? '检查暂不可用' : '等待系统授权'}</small>}
      </span>
      <button ref={entryRef} type="button" className="stage-status permission-stage-button" onClick={() => setOpen(true)}>
        {!active ? '等待' : ready ? '已完成' : loading ? '检查中' : '去开启'}
      </button>
    </li>
    {open && createPortal(<div className="permission-modal-backdrop" onMouseDown={event => {
      if (event.target === event.currentTarget) setOpen(false)
    }}>
      <div className="permission-modal" ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="permission-title">
        <button type="button" className="permission-modal-close" aria-label="关闭权限引导" onClick={() => setOpen(false)}>×</button>
        {panel}
      </div>
    </div>, document.body)}
  </>
}
