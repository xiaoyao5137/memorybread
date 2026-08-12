import React, { useState } from 'react'
import type { BakeKnowledgeItem } from '../../types'
import { BakeButton, BakeCard, BakeMarkdown, BakeSectionHeader } from './BakeShared'

const formatCreatedTime = (item: Pick<BakeKnowledgeItem, 'createdAt' | 'createdAtMs'>) => {
  if (item.createdAtMs > 0) {
    return new Date(item.createdAtMs).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }
  return item.createdAt || '创建时间未知'
}

const BakeKnowledgeTab: React.FC<{
  items: BakeKnowledgeItem[]
  total: number
  limit: number
  offset: number
  query: string
  draftQuery: string
  from: string
  to: string
  draftFrom: string
  draftTo: string
  selectedKnowledgeId: string | null
  onSelectKnowledge: (id: string | null) => void
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  onDeleteKnowledge: (id: string) => void
  onOpenCapture: (captureId?: string) => void
  onViewSourceTimeline: (timelineId?: string) => void
  sourceTimelineTitle?: string
  onCreateKnowledge?: (knowledge: Partial<BakeKnowledgeItem>) => void
  focusId?: string | null
}> = ({
  items,
  total,
  limit,
  offset,
  query,
  draftQuery,
  from,
  to,
  draftFrom,
  draftTo,
  selectedKnowledgeId,
  onSelectKnowledge,
  onPageChange,
  onLimitChange,
  onDraftQueryChange,
  onDraftFromChange,
  onDraftToChange,
  onSearch,
  onClearFilters,
  onDeleteKnowledge,
  onOpenCapture,
  onViewSourceTimeline,
  sourceTimelineTitle,
  onCreateKnowledge,
  focusId,
}) => {
  const selected = items.find(item => item.id === selectedKnowledgeId) ?? items[0]
  const selectedSourceCaptureIds = selected?.sourceCaptureIds.length
    ? selected.sourceCaptureIds
    : selected?.captureId
      ? [selected.captureId]
      : []
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))
  const hasActiveFilters = Boolean(query.trim() || from || to || focusId)
  const [pageInput, setPageInput] = useState('')
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [newKnowledge, setNewKnowledge] = useState({
    summary: '',
    overview: '',
    details: '',
    category: '',
    importance: 5,
  })
  const handleCreate = () => {
    if (!newKnowledge.summary.trim()) return
    onCreateKnowledge?.({
      ...newKnowledge,
      id: `knowledge-manual-${Date.now()}`,
      captureId: '',
      sourceCaptureIds: [],
      entities: [],
      occurrenceCount: 1,
      status: 'confirmed',
      reviewStatus: 'confirmed',
      updatedAt: new Date().toLocaleString('zh-CN', { hour12: false }),
      updatedAtMs: Date.now(),
      createdAt: new Date().toLocaleString('zh-CN', { hour12: false }),
      createdAtMs: Date.now(),
    })
    setShowCreateDialog(false)
    setNewKnowledge({
      summary: '',
      overview: '',
      details: '',
      category: '',
      importance: 5,
    })
  }

  return (
    <>
      <BakeCard>
        <BakeSectionHeader
          title="知识"
          right={onCreateKnowledge && <BakeButton primary onClick={() => setShowCreateDialog(true)}>新建知识</BakeButton>}
        />
        <form
          className="bake-list-toolbar bake-list-toolbar--repository"
          onSubmit={(event) => {
            event.preventDefault()
            onSearch()
          }}
        >
          <div className="bake-list-toolbar__repository">
            <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--search">
              <label className="bake-form-field bake-filter-field bake-filter-field--search">
                <span className="bake-filter-label">关键词</span>
                <input
                  className="bake-input"
                  value={draftQuery}
                  onChange={(event) => onDraftQueryChange(event.target.value)}
                  placeholder="搜索知识摘要、概述、详情或分类"
                />
              </label>
              <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--search">
                <BakeButton compact primary type="submit">搜索</BakeButton>
              </div>
            </div>
            <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--dates">
              <label className="bake-form-field bake-filter-field">
                <span className="bake-filter-label">新增开始日期</span>
                <input
                  className="bake-input"
                  type="date"
                  value={draftFrom}
                  onChange={(event) => onDraftFromChange(event.target.value)}
                />
              </label>
              <label className="bake-form-field bake-filter-field">
                <span className="bake-filter-label">新增结束日期</span>
                <input
                  className="bake-input"
                  type="date"
                  value={draftTo}
                  onChange={(event) => onDraftToChange(event.target.value)}
                />
              </label>
              {(draftQuery || query || draftFrom || from || draftTo || to || focusId) && (
                <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
                  <BakeButton compact type="button" onClick={onClearFilters}>清除筛选</BakeButton>
                </div>
              )}
            </div>
          </div>
        </form>
      </BakeCard>
      <div className="bake-split-list-detail bake-split-list-detail--knowledge">
        <BakeCard className="bake-knowledge-list-card">
        <div className="bake-list bake-knowledge-list">
          {items.length === 0 ? (
            <div className="bake-muted">{hasActiveFilters ? '当前筛选条件下没有可展示的知识条目。' : '当前还没有知识条目。'}</div>
          ) : items.map(item => {
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => onSelectKnowledge(item.id)}
                className={`bake-list-item bake-knowledge-list-item ${item.id === selected?.id ? 'bake-list-item--active' : ''}`.trim()}
              >
                <div className="bake-list-item__title bake-line-clamp-2">{item.summary}</div>
                <div className="bake-muted bake-line-clamp-2">{item.overview || '暂无概述'}</div>
                <div className="bake-memory-list-item__meta">
                  <span>创建 {formatCreatedTime(item)}</span>
                  <span>重要度 {item.importance}</span>
                </div>
              </button>
            )
          })}
        </div>
        <div className="bake-pagination bake-pagination--extended">
          <div className="bake-pagination__controls">
            <BakeButton compact onClick={() => onPageChange(Math.max(0, offset - limit))}>上一页</BakeButton>
            <BakeButton compact onClick={() => onPageChange(offset + limit)}>{offset + limit >= total ? '已到底' : '下一页'}</BakeButton>
          </div>
          <div className="bake-pagination__summary-group bake-muted">
            <span className="bake-pagination__summary">知识条目共 {total} 条</span>
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
                {[10, 20, 50, 100].map(option => (
                  <option key={option} value={option}>{option} 条</option>
                ))}
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
                const nextPage = Math.min(totalPages, Math.floor(target))
                onPageChange((nextPage - 1) * limit)
                setPageInput('')
              }}>前往</BakeButton>
            </div>
          </div>
        </div>
      </BakeCard>

      <BakeCard className="bake-knowledge-detail-card">
        {selected ? (
          <div className="bake-kv bake-capture-detail bake-knowledge-detail">
            <div>
              <div className="bake-title" style={{ fontSize: 18 }}>{selected.summary}</div>
              <div className="bake-muted" style={{ marginTop: 4 }}>
                ID: {selected.id} · 创建：{formatCreatedTime(selected)}
              </div>
            </div>
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">概述</div>
              <div className="bake-muted" style={{ lineHeight: 1.7 }}>{selected.overview || '暂无概述'}</div>
            </div>
            {selected.detailedContent && (
              <div className="bake-knowledge-detail__section">
                <div className="bake-kv__title">详细内容</div>
                <BakeMarkdown content={selected.detailedContent} />
              </div>
            )}
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">元数据</div>
              <div className="bake-muted" style={{ lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
                {(() => {
                  if (!selected.details) return '暂无元数据'
                  try {
                    const parsed = JSON.parse(selected.details)
                    return JSON.stringify(parsed, null, 2)
                  } catch {
                    return selected.details
                  }
                })()}
              </div>
            </div>
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">来源采集记录</div>
              <div className="bake-memory-detail__stats">
                {selectedSourceCaptureIds.length > 0 ? selectedSourceCaptureIds.map((captureId) => (
                  <button
                    key={captureId}
                    type="button"
                    className="bake-stat-chip bake-stat-chip--button"
                    onClick={() => onOpenCapture(captureId)}
                  >
                    采集记录 #{captureId}
                  </button>
                )) : <span className="bake-muted">暂无关联采集记录</span>}
              </div>
            </div>
            <div className="bake-actions--primary">
              <BakeButton onClick={() => onViewSourceTimeline(selected.sourceTimelineId || selected.id)}>关联时间线</BakeButton>
              <BakeButton danger onClick={() => onDeleteKnowledge(selected.id)}>删除知识</BakeButton>
            </div>
            <div className="bake-related-summary">
              <div className="bake-related-row">
                <span className="bake-related-row__label">关联时间线</span>
                <span className="bake-related-row__value">
                  {sourceTimelineTitle || (selected.sourceTimelineId ? `时间线 #${selected.sourceTimelineId}` : '暂无')}
                </span>
              </div>
              <div className="bake-related-row">
                <span className="bake-related-row__label">来源采集标题</span>
                <span className="bake-related-row__value">
                  {selectedSourceCaptureIds.length > 0
                    ? selectedSourceCaptureIds.map(captureId => `采集记录 #${captureId}`).join('、')
                    : '暂无'}
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="bake-muted">暂无知识条目</div>
        )}
      </BakeCard>
      </div>
      {showCreateDialog && (
        <div className="bake-modal-overlay" onClick={() => setShowCreateDialog(false)}>
          <div className="bake-modal" onClick={(e) => e.stopPropagation()}>
            <div className="bake-modal__header">
              <h3>新建知识</h3>
              <button className="bake-modal__close" onClick={() => setShowCreateDialog(false)}>×</button>
            </div>
            <div className="bake-modal__body">
              <label className="bake-form-field">
                <span className="bake-form-label">摘要 *</span>
                <input
                  className="bake-input"
                  value={newKnowledge.summary}
                  onChange={(e) => setNewKnowledge({ ...newKnowledge, summary: e.target.value })}
                  placeholder="简短描述这条知识"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">概述</span>
                <textarea
                  className="bake-textarea"
                  rows={3}
                  value={newKnowledge.overview}
                  onChange={(e) => setNewKnowledge({ ...newKnowledge, overview: e.target.value })}
                  placeholder="对知识的概括性说明"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">详细内容</span>
                <textarea
                  className="bake-textarea"
                  rows={6}
                  value={newKnowledge.details}
                  onChange={(e) => setNewKnowledge({ ...newKnowledge, details: e.target.value })}
                  placeholder="详细的知识内容，支持 Markdown 格式"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">分类</span>
                <input
                  className="bake-input"
                  value={newKnowledge.category}
                  onChange={(e) => setNewKnowledge({ ...newKnowledge, category: e.target.value })}
                  placeholder="如：技术、业务、流程等"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">重要度 (1-10)</span>
                <input
                  className="bake-input"
                  type="number"
                  min={1}
                  max={10}
                  value={newKnowledge.importance}
                  onChange={(e) => setNewKnowledge({ ...newKnowledge, importance: Number(e.target.value) })}
                />
              </label>
            </div>
            <div className="bake-modal__footer">
              <BakeButton onClick={() => setShowCreateDialog(false)}>取消</BakeButton>
              <BakeButton primary onClick={handleCreate} disabled={!newKnowledge.summary.trim()}>创建</BakeButton>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default BakeKnowledgeTab
