import React, { useCallback, useEffect, useRef, useState } from 'react'
import './bake/BakePanel.css'

export interface ConfirmDialogOptions {
  title: string
  description: string
  confirmLabel?: string
  danger?: boolean
}

interface ConfirmRequest extends ConfirmDialogOptions {
  resolve: (ok: boolean) => void
}

/**
 * 应用内确认对话框。
 *
 * Tauri 的 macOS WKWebView（wry）未实现 window.confirm 面板，
 * 原生 confirm 会静默返回 false，导致删除等破坏性操作“点击无响应”。
 * 统一使用该 hook 提供的 Promise 式 confirm 与应用内弹窗。
 */
export function useConfirmDialog() {
  const [request, setRequest] = useState<ConfirmRequest | null>(null)
  const requestRef = useRef<ConfirmRequest | null>(null)
  requestRef.current = request

  const confirm = useCallback((options: ConfirmDialogOptions) => (
    new Promise<boolean>((resolve) => {
      setRequest({ confirmLabel: '确认删除', danger: true, ...options, resolve })
    })
  ), [])

  const settle = useCallback((ok: boolean) => {
    const current = requestRef.current
    if (!current) return
    setRequest(null)
    current.resolve(ok)
  }, [])

  useEffect(() => {
    if (!request) return undefined
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') settle(false)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [request, settle])

  const dialog = request ? (
    <div className="bake-modal-overlay" onClick={() => settle(false)}>
      <section
        className="bake-modal bake-modal--confirm"
        role="alertdialog"
        aria-modal="true"
        aria-label={request.title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="bake-modal__header">
          <h3>{request.title}</h3>
          <button type="button" className="bake-modal__close" aria-label="关闭" onClick={() => settle(false)}>
            ×
          </button>
        </div>
        <div className="bake-modal__body bake-modal__body--confirm">
          <p>{request.description}</p>
        </div>
        <div className="bake-modal__footer">
          <button type="button" className="bake-btn bake-btn--compact" autoFocus onClick={() => settle(false)}>
            取消
          </button>
          <button
            type="button"
            className={`bake-btn bake-btn--compact ${request.danger === false ? '' : 'bake-btn--danger'}`.trim()}
            onClick={() => settle(true)}
          >
            {request.confirmLabel}
          </button>
        </div>
      </section>
    </div>
  ) : null

  return { confirm, dialog }
}

export default useConfirmDialog
