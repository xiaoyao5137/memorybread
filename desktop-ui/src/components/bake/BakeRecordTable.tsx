import { Eye, Network, Pencil, X } from 'lucide-react'
import React, { useEffect, useRef, useState } from 'react'
import { BakeButton, BakeCard } from './BakeShared'

export type BakeRecordColumn<T> = {
  key: string
  label: string
  className?: string
  render: (item: T) => React.ReactNode
}

export const BakeTableActionButton: React.FC<{
  kind: 'detail' | 'edit' | 'graph'
  label: string
  onClick: (trigger: HTMLButtonElement) => void
}> = ({ kind, label, onClick }) => (
  <button
    type="button"
    className="bake-record-table__action-button"
    aria-label={label}
    title={kind === 'detail' ? '查看详情' : kind === 'edit' ? '编辑' : '在记忆图谱中查看'}
    onClick={(event) => onClick(event.currentTarget)}
  >
    {kind === 'detail'
      ? <Eye size={15} strokeWidth={1.9} aria-hidden="true" />
      : kind === 'edit'
        ? <Pencil size={14} strokeWidth={1.9} aria-hidden="true" />
        : <Network size={15} strokeWidth={1.9} aria-hidden="true" />}
  </button>
)

export const BakeRecordTable = <T,>({
  items,
  total,
  limit,
  offset,
  columns,
  getRowId,
  ariaLabel,
  emptyTitle,
  emptyDescription,
  loading,
  activeId,
  itemLabel = '条',
  onPageChange,
  onLimitChange,
  renderActions,
}: {
  items: T[]
  total: number
  limit: number
  offset: number
  columns: BakeRecordColumn<T>[]
  getRowId: (item: T) => string | number
  ariaLabel: string
  emptyTitle: string
  emptyDescription: string
  loading?: boolean
  activeId?: string | number | null
  itemLabel?: string
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  renderActions: (item: T) => React.ReactNode
}) => {
  const [pageInput, setPageInput] = useState('')
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))

  return (
    <BakeCard className="bake-record-table-card">
      <div className="bake-record-table-region" role="region" aria-label={ariaLabel} tabIndex={0}>
        {loading ? (
          <div className="bake-record-table-empty">
            <div className="bake-record-table-empty__title">正在加载…</div>
          </div>
        ) : items.length === 0 ? (
          <div className="bake-record-table-empty">
            <div className="bake-record-table-empty__title">{emptyTitle}</div>
            <div className="bake-muted">{emptyDescription}</div>
          </div>
        ) : (
          <table className="bake-record-table">
            <caption className="bake-visually-hidden">{ariaLabel}</caption>
            <thead>
              <tr>
                {columns.map(column => (
                  <th key={column.key} className={column.className} scope="col">{column.label}</th>
                ))}
                <th className="bake-record-table__actions" scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => {
                const rowId = getRowId(item)
                return (
                  <tr key={rowId} className={String(rowId) === String(activeId ?? '') ? 'bake-record-table__row--active' : undefined}>
                    {columns.map(column => (
                      <td key={column.key} className={column.className}>{column.render(item)}</td>
                    ))}
                    <td className="bake-record-table__actions">
                      <div className="bake-record-table__action-group">{renderActions(item)}</div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="bake-pagination bake-pagination--extended">
        <div className="bake-pagination__controls">
          <BakeButton compact disabled={offset <= 0} onClick={() => onPageChange(Math.max(0, offset - limit))}>上一页</BakeButton>
          <BakeButton compact disabled={offset + limit >= total} onClick={() => onPageChange(offset + limit)}>下一页</BakeButton>
        </div>
        <div className="bake-pagination__summary-group bake-muted">
          <span className="bake-pagination__summary">共 {total} {itemLabel}</span>
          <span className="bake-pagination__summary">第 {page}/{totalPages} 页</span>
        </div>
        <div className="bake-pagination__right">
          <label className="bake-pagination__field">
            <span className="bake-muted">每页</span>
            <select
              className="bake-input bake-pagination__select"
              value={String(limit)}
              aria-label="每页条数"
              onChange={(event) => onLimitChange(Number(event.target.value))}
            >
              {[10, 20, 50, 100].map(option => <option key={option} value={option}>{option} 条</option>)}
            </select>
          </label>
          <div className="bake-pagination__jump">
            <span className="bake-muted">第</span>
            <input
              className="bake-input bake-pagination__input"
              type="number"
              min={1}
              max={totalPages}
              value={pageInput}
              onChange={(event) => setPageInput(event.target.value)}
              placeholder={String(page)}
              aria-label="跳转页码"
            />
            <span className="bake-muted">页</span>
            <BakeButton compact onClick={() => {
              const target = Number(pageInput)
              if (!Number.isFinite(target) || target < 1) return
              onPageChange((Math.min(totalPages, Math.floor(target)) - 1) * limit)
              setPageInput('')
            }}>前往</BakeButton>
          </div>
        </div>
      </div>
    </BakeCard>
  )
}

export const BakeDetailDrawer: React.FC<{
  open: boolean
  eyebrow: string
  title: string
  meta?: React.ReactNode
  ariaLabel?: string
  closeLabel?: string
  wide?: boolean
  onClose: () => void
  children: React.ReactNode
  footer?: React.ReactNode
}> = ({ open, eyebrow, title, meta, ariaLabel, closeLabel, wide, onClose, children, footer }) => {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!open) return
    closeButtonRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [open])

  if (!open) return null

  return (
    <div className="bake-record-drawer-overlay" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <aside
        className={`bake-record-drawer${wide ? ' bake-record-drawer--wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel || title}
      >
        <header className="bake-record-drawer__header">
          <div className="bake-record-drawer__heading">
            <span>{eyebrow}</span>
            <h2>{title}</h2>
            {meta && <div className="bake-record-drawer__meta">{meta}</div>}
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="bake-record-drawer__close"
            aria-label={closeLabel || '关闭详情'}
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <div className="bake-record-drawer__body">{children}</div>
        {footer && <footer className="bake-record-drawer__footer">{footer}</footer>}
      </aside>
    </div>
  )
}
