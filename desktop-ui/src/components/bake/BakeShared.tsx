import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { prepareBakeMarkdown } from '../../utils/bakeMarkdown'
import './BakePanel.css'

export const BakeCard: React.FC<React.PropsWithChildren<{ className?: string }>> = ({ className = '', children }) => (
  <section className={`bake-card ${className}`.trim()}>{children}</section>
)

export const BakePill: React.FC<{ text: string }> = ({ text }) => (
  <span className="bake-pill">{text}</span>
)

export const BakeButton: React.FC<React.PropsWithChildren<{
  active?: boolean
  primary?: boolean
  danger?: boolean
  compact?: boolean
  disabled?: boolean
  onClick?: () => void
  type?: 'button' | 'submit' | 'reset'
}>> = ({ active, primary, danger, compact, disabled, onClick, type = 'button', children }) => {
  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    onClick?.()
  }

  return (
    <button
      type={type}
      onClick={handleClick}
      disabled={disabled}
      className={`bake-btn ${active ? 'bake-btn--active' : ''} ${primary ? 'bake-btn--primary' : ''} ${danger ? 'bake-btn--danger' : ''} ${compact ? 'bake-btn--compact' : ''}`.trim()}
    >
      {children}
    </button>
  )
}

export const BakeSectionHeader: React.FC<{
  title: string
  subtitle?: string
  right?: React.ReactNode
}> = ({ title, subtitle, right }) => (
  <div className="bake-section-header">
    <div className="bake-section-header__main">
      <div className="bake-section-title">{title}</div>
      {subtitle && <div className="bake-section-subtitle">{subtitle}</div>}
    </div>
    {right ? <div className="bake-section-header__right">{right}</div> : null}
  </div>
)

export const BakeMarkdown: React.FC<{ content?: string | null }> = ({ content }) => {
  const trimmed = content?.trim()
  if (!trimmed) return <div className="bake-muted">暂无详细内容</div>
  return (
    <div className="bake-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ node: _node, ...props }) => (
            <div className="bake-markdown__table-scroll" role="region" aria-label="表格，可横向滚动" tabIndex={0}>
              <table {...props} />
            </div>
          ),
        }}
      >{prepareBakeMarkdown(trimmed)}</ReactMarkdown>
    </div>
  )
}
