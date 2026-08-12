import React, { useState } from 'react'
import type { SopCandidate } from '../../types'
import { BakeButton, BakeCard, BakeMarkdown, BakeSectionHeader } from './BakeShared'

const formatCreatedTime = (item: Pick<SopCandidate, 'createdAt' | 'createdAtMs'>) => {
  if ((item.createdAtMs ?? 0) > 0) {
    return new Date(item.createdAtMs ?? 0).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }
  return item.createdAt || '创建时间未知'
}

const BakeSopTab: React.FC<{
  candidates: SopCandidate[]
  total: number
  limit: number
  offset: number
  query: string
  from: string
  to: string
  draftQuery: string
  draftFrom: string
  draftTo: string
  selectedSopId: string | null
  onSelectSop: (id: string | null) => void
  onDeleteSop: (id: string) => void
  onViewSourceTimeline: (timelineId?: string) => void
  sourceTimelineTitle?: string
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  onCreateSop?: (sop: Partial<SopCandidate>) => void
  focusId?: string | null
}> = ({
  candidates,
  total,
  limit,
  offset,
  query,
  from,
  to,
  draftQuery,
  draftFrom,
  draftTo,
  selectedSopId,
  onSelectSop,
  onDeleteSop,
  onViewSourceTimeline,
  sourceTimelineTitle,
  onPageChange,
  onLimitChange,
  onDraftQueryChange,
  onDraftFromChange,
  onDraftToChange,
  onSearch,
  onClearFilters,
  onCreateSop,
  focusId,
}) => {
  const selected = candidates.find(item => item.id === selectedSopId) ?? candidates[0]
  const [pageInput, setPageInput] = useState('')
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const hasActiveFilters = Boolean(query.trim() || from || to || focusId)
  const [newSop, setNewSop] = useState<{
    extractedProblem: string
    detailedContent: string
    steps: string[]
    triggerKeywords: string[]
    confidence: 'low' | 'medium' | 'high'
  }>({
    extractedProblem: '',
    detailedContent: '',
    steps: [''],
    triggerKeywords: [''],
    confidence: 'medium',
  })
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))

  const handleCreate = () => {
    if (!newSop.extractedProblem.trim() || newSop.steps.filter(s => s.trim()).length === 0) return
    onCreateSop?.({
      ...newSop,
      id: `sop-manual-${Date.now()}`,
      sourceCaptureId: '',
      steps: newSop.steps.filter(s => s.trim()),
      triggerKeywords: newSop.triggerKeywords.filter(k => k.trim()),
      linkedKnowledgeIds: [],
      linkedKnowledgeSummaries: [],
      status: 'confirmed',
      createdAt: new Date().toLocaleString('zh-CN', { hour12: false }),
      createdAtMs: Date.now(),
    })
    setShowCreateDialog(false)
    setNewSop({
      extractedProblem: '',
      detailedContent: '',
      steps: [''],
      triggerKeywords: [''],
      confidence: 'medium',
    })
  }

  const addStep = () => setNewSop({ ...newSop, steps: [...newSop.steps, ''] })
  const updateStep = (index: number, value: string) => {
    const updated = [...newSop.steps]
    updated[index] = value
    setNewSop({ ...newSop, steps: updated })
  }
  const removeStep = (index: number) => {
    if (newSop.steps.length <= 1) return
    setNewSop({ ...newSop, steps: newSop.steps.filter((_, i) => i !== index) })
  }

  const addKeyword = () => setNewSop({ ...newSop, triggerKeywords: [...newSop.triggerKeywords, ''] })
  const updateKeyword = (index: number, value: string) => {
    const updated = [...newSop.triggerKeywords]
    updated[index] = value
    setNewSop({ ...newSop, triggerKeywords: updated })
  }
  const removeKeyword = (index: number) => {
    if (newSop.triggerKeywords.length <= 1) return
    setNewSop({ ...newSop, triggerKeywords: newSop.triggerKeywords.filter((_, i) => i !== index) })
  }

  return (
    <>
      <BakeCard>
        <BakeSectionHeader
          title="操作"
          right={onCreateSop && <BakeButton primary onClick={() => setShowCreateDialog(true)}>新建</BakeButton>}
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
                  placeholder="搜索问题或关键词"
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
      <div className="bake-split-list-detail bake-split-list-detail--sop">
        <BakeCard className="bake-knowledge-list-card">
        <div className="bake-list bake-knowledge-list">
          {candidates.length === 0 ? (
            <div className="bake-muted">{hasActiveFilters ? '当前筛选条件下没有操作手册。' : '当前还没有操作手册。'}</div>
          ) : candidates.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelectSop(item.id)}
              className={`bake-list-item bake-knowledge-list-item ${item.id === selected?.id ? 'bake-list-item--active' : ''}`.trim()}
            >
              <div className="bake-inline-meta">
                <div style={{ minWidth: 0 }}>
                  <div className="bake-list-item__title bake-line-clamp-2">{item.extractedProblem || '未命名问题'}</div>
                  <div className="bake-muted bake-line-clamp-1">关键词：{item.triggerKeywords.join(' / ') || '暂无'}</div>
                  <div className="bake-muted bake-line-clamp-1">创建：{formatCreatedTime(item)}</div>
                </div>
              </div>
            </button>
          ))}
        </div>
        <div className="bake-pagination bake-pagination--extended">
          <div className="bake-pagination__controls">
            <BakeButton compact onClick={() => onPageChange(Math.max(0, offset - limit))}>上一页</BakeButton>
            <BakeButton compact onClick={() => onPageChange(offset + limit)}>{offset + limit >= total ? '已到底' : '下一页'}</BakeButton>
          </div>
          <div className="bake-pagination__summary bake-muted">操作手册共 {total} 条</div>
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
          <div className="bake-kv bake-knowledge-detail">
            <div>
              <div className="bake-title" style={{ fontSize: 18 }}>{selected.extractedProblem || '未命名问题'}</div>
              <div className="bake-muted" style={{ marginTop: 4 }}>ID: {selected.id} · 创建：{formatCreatedTime(selected)}</div>
            </div>
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">触发关键词</div>
              <div className="bake-memory-detail__stats">
                {selected.triggerKeywords.length > 0 ? selected.triggerKeywords.map(keyword => (
                  <span key={keyword} className="bake-stat-chip">{keyword}</span>
                )) : <span className="bake-muted">暂无触发关键词</span>}
              </div>
            </div>
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">处理步骤</div>
              <div className="bake-list">
                {selected.steps.length > 0 ? selected.steps.map((step, idx) => (
                  <div key={`${selected.id}-${idx}`} className="bake-list-item">
                    <div className="bake-muted">{idx + 1}. {step}</div>
                  </div>
                )) : <div className="bake-muted">暂无处理步骤</div>}
              </div>
            </div>
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">详细描述</div>
              <BakeMarkdown content={selected.detailedContent} />
            </div>
            <div className="bake-actions--primary">
              <BakeButton onClick={() => onViewSourceTimeline(selected.sourceTimelineId || selected.id)}>关联时间线</BakeButton>
              <BakeButton danger onClick={() => onDeleteSop(selected.id)}>删除操作手册</BakeButton>
            </div>
            <div className="bake-related-summary">
              <div className="bake-related-row">
                <span className="bake-related-row__label">关联时间线</span>
                <span className="bake-related-row__value">
                  {sourceTimelineTitle || (selected.sourceTimelineId ? `时间线 #${selected.sourceTimelineId}` : '暂无')}
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="bake-muted">暂无操作手册</div>
        )}
      </BakeCard>
      </div>
      {showCreateDialog && (
        <div className="bake-modal-overlay" onClick={() => setShowCreateDialog(false)}>
          <div className="bake-modal" onClick={(e) => e.stopPropagation()}>
            <div className="bake-modal__header">
              <h3>新建操作手册</h3>
              <button className="bake-modal__close" onClick={() => setShowCreateDialog(false)}>×</button>
            </div>
            <div className="bake-modal__body">
              <label className="bake-form-field">
                <span className="bake-form-label">问题描述 *</span>
                <input
                  className="bake-input"
                  value={newSop.extractedProblem}
                  onChange={(e) => setNewSop({ ...newSop, extractedProblem: e.target.value })}
                  placeholder="描述这个操作手册要解决的问题"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">详细说明</span>
                <textarea
                  className="bake-textarea"
                  rows={4}
                  value={newSop.detailedContent}
                  onChange={(e) => setNewSop({ ...newSop, detailedContent: e.target.value })}
                  placeholder="对操作手册的详细说明，支持 Markdown 格式"
                />
              </label>
              <div className="bake-form-field">
                <span className="bake-form-label">操作步骤 *</span>
                {newSop.steps.map((step, index) => (
                  <div key={index} style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                    <input
                      className="bake-input"
                      value={step}
                      onChange={(e) => updateStep(index, e.target.value)}
                      placeholder={`步骤 ${index + 1}`}
                      style={{ flex: 1 }}
                    />
                    {newSop.steps.length > 1 && (
                      <BakeButton compact onClick={() => removeStep(index)}>删除</BakeButton>
                    )}
                  </div>
                ))}
                <BakeButton compact onClick={addStep}>+ 添加步骤</BakeButton>
              </div>
              <div className="bake-form-field">
                <span className="bake-form-label">触发关键词</span>
                {newSop.triggerKeywords.map((keyword, index) => (
                  <div key={index} style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                    <input
                      className="bake-input"
                      value={keyword}
                      onChange={(e) => updateKeyword(index, e.target.value)}
                      placeholder={`关键词 ${index + 1}`}
                      style={{ flex: 1 }}
                    />
                    {newSop.triggerKeywords.length > 1 && (
                      <BakeButton compact onClick={() => removeKeyword(index)}>删除</BakeButton>
                    )}
                  </div>
                ))}
                <BakeButton compact onClick={addKeyword}>+ 添加关键词</BakeButton>
              </div>
            </div>
            <div className="bake-modal__footer">
              <BakeButton onClick={() => setShowCreateDialog(false)}>取消</BakeButton>
              <BakeButton
                primary
                onClick={handleCreate}
                disabled={!newSop.extractedProblem.trim() || newSop.steps.filter(s => s.trim()).length === 0}
              >
                创建
              </BakeButton>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default BakeSopTab
