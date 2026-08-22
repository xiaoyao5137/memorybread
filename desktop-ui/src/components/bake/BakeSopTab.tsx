import React, { useEffect, useRef, useState } from 'react'
import type { MemoryFavoriteFilter, SopCandidate } from '../../types'
import { BakeFavoriteButton, BakeFavoriteFilterControl } from './BakeFavoriteControls'
import BakeRichTextEditor from './BakeRichTextEditor'
import { BakeDetailDrawer, BakeRecordTable, BakeTableActionButton, type BakeRecordColumn } from './BakeRecordTable'
import { BakeButton, BakeMarkdown } from './BakeShared'

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

const operationOverview = (item: SopCandidate) => {
  const detailed = (item.detailedContent || '').replace(/[#>*_`\[\]()]/g, ' ').replace(/\s+/g, ' ').trim()
  return detailed || item.steps.slice(0, 3).join(' → ') || '暂无操作概述'
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
  onDeleteSop: (id: string) => void | boolean | Promise<boolean>
  onViewSourceTimeline: (timelineId?: string) => void
  sourceTimelineTitle?: string
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  favoriteFilter?: MemoryFavoriteFilter
  onFavoriteFilterChange?: (value: MemoryFavoriteFilter) => void
  onToggleFavorite?: (item: SopCandidate, isFavorite: boolean) => boolean | Promise<boolean>
  onOpenGraph?: (sop: SopCandidate) => void
  onCreateSop?: (sop: Pick<SopCandidate, 'extractedProblem' | 'detailedContent' | 'steps' | 'triggerKeywords'>) => boolean | Promise<boolean>
  onUpdateSop?: (sop: SopCandidate) => boolean | Promise<boolean>
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
  favoriteFilter = 'all',
  onFavoriteFilterChange,
  onToggleFavorite,
  onOpenGraph,
  onCreateSop,
  onUpdateSop,
  focusId,
}) => {
  const selected = candidates.find(item => item.id === selectedSopId) ?? candidates[0]
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [drawerMode, setDrawerMode] = useState<'detail' | 'edit' | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [favoriteBusy, setFavoriteBusy] = useState(false)
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null)
  const hasActiveFilters = Boolean(query.trim() || from || to || focusId || favoriteFilter !== 'all')
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
  const [draftSop, setDraftSop] = useState({
    extractedProblem: '',
    detailedContent: '',
    steps: [''],
    triggerKeywords: [''],
  })
  useEffect(() => {
    setDraftSop({
      extractedProblem: selected?.extractedProblem || '',
      detailedContent: selected?.detailedContent || '',
      steps: selected?.steps.length ? selected.steps : [''],
      triggerKeywords: selected?.triggerKeywords.length ? selected.triggerKeywords : [''],
    })
  }, [selected])

  useEffect(() => {
    if (focusId && selected?.id === focusId) setDrawerMode('detail')
  }, [focusId, selected?.id])

  const openDrawer = (item: SopCandidate, mode: 'detail' | 'edit', trigger: HTMLButtonElement) => {
    detailTriggerRef.current = trigger
    onSelectSop(item.id)
    setDrawerMode(mode)
  }

  const closeDrawer = () => {
    const trigger = detailTriggerRef.current
    setDrawerMode(null)
    onSelectSop(null)
    window.setTimeout(() => trigger?.focus(), 0)
  }

  const handleCreate = async () => {
    if (!newSop.extractedProblem.trim() || newSop.steps.filter(s => s.trim()).length === 0) return
    setIsSaving(true)
    const created = await onCreateSop?.({
      extractedProblem: newSop.extractedProblem,
      detailedContent: newSop.detailedContent,
      steps: newSop.steps.filter(s => s.trim()),
      triggerKeywords: newSop.triggerKeywords.filter(k => k.trim()),
    })
    setIsSaving(false)
    if (created === false) return
    setShowCreateDialog(false)
    setNewSop({
      extractedProblem: '',
      detailedContent: '',
      steps: [''],
      triggerKeywords: [''],
      confidence: 'medium',
    })
  }

  const handleUpdate = async () => {
    if (!selected || !onUpdateSop || !draftSop.extractedProblem.trim()) return
    const steps = draftSop.steps.map(step => step.trim()).filter(Boolean)
    if (!steps.length) return
    setIsSaving(true)
    const saved = await onUpdateSop({
      ...selected,
      extractedProblem: draftSop.extractedProblem.trim(),
      detailedContent: draftSop.detailedContent.trim(),
      steps,
      triggerKeywords: draftSop.triggerKeywords.map(keyword => keyword.trim()).filter(Boolean),
    })
    setIsSaving(false)
    if (saved !== false) setDrawerMode('detail')
  }

  const cancelEditing = () => {
    setDraftSop({
      extractedProblem: selected?.extractedProblem || '',
      detailedContent: selected?.detailedContent || '',
      steps: selected?.steps.length ? selected.steps : [''],
      triggerKeywords: selected?.triggerKeywords.length ? selected.triggerKeywords : [''],
    })
    setDrawerMode('detail')
  }

  const handleToggleFavorite = async () => {
    if (!selected || !onToggleFavorite || favoriteBusy) return
    const nextFavorite = !Boolean(selected.isFavorite)
    setFavoriteBusy(true)
    const updated = await onToggleFavorite(selected, nextFavorite)
    setFavoriteBusy(false)
    if (updated !== false && favoriteFilter !== 'all') closeDrawer()
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

  const columns: BakeRecordColumn<SopCandidate>[] = [
    {
      key: 'created',
      label: '创建时间',
      className: 'bake-record-table__time',
      render: item => <><div>{formatCreatedTime(item)}</div><div className="bake-record-table__secondary">ID #{item.id}</div></>,
    },
    {
      key: 'problem',
      label: '操作名称',
      className: 'bake-record-table__title',
      render: item => <div className="bake-record-table__primary bake-line-clamp-2">{item.extractedProblem || '未命名操作'}</div>,
    },
    {
      key: 'keywords',
      label: '适用场景',
      className: 'bake-record-table__scenario',
      render: item => <div className="bake-record-table__tag-list">{item.triggerKeywords.length ? item.triggerKeywords.map(keyword => <span key={keyword} className="bake-record-table__badge">{keyword}</span>) : <span className="bake-record-table__secondary">暂无适用场景</span>}</div>,
    },
    {
      key: 'overview',
      label: '操作环节概述',
      render: item => <div className="bake-record-table__preview bake-line-clamp-2">{operationOverview(item)}</div>,
    },
  ]

  return (
    <>
      <form className="bake-list-toolbar bake-list-toolbar--repository" onSubmit={(event) => { event.preventDefault(); onSearch() }}>
          <div className="bake-list-toolbar__repository">
            <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--search">
              <label className="bake-form-field bake-filter-field bake-filter-field--search">
                <span className="bake-filter-label">关键词</span>
                <input className="bake-input" value={draftQuery} onChange={(event) => onDraftQueryChange(event.target.value)} placeholder="搜索操作 ID、名称、适用场景或操作内容" />
              </label>
            </div>
            <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--asset-filters bake-list-toolbar__repository-row--asset-filters-simple">
              {onFavoriteFilterChange && <BakeFavoriteFilterControl value={favoriteFilter} onChange={onFavoriteFilterChange} />}
              <label className="bake-form-field bake-filter-field bake-filter-field--date">
                <span className="bake-filter-label">起始时间</span>
                <input
                  className="bake-input"
                  type="date"
                  value={draftFrom}
                  onChange={(event) => onDraftFromChange(event.target.value)}
                />
              </label>
              <label className="bake-form-field bake-filter-field bake-filter-field--date">
                <span className="bake-filter-label">结束时间</span>
                <input
                  className="bake-input"
                  type="date"
                  value={draftTo}
                  onChange={(event) => onDraftToChange(event.target.value)}
                />
              </label>
              <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
                <div className="bake-list-toolbar__repository-primary-actions">
                  <BakeButton compact type="button" onClick={onClearFilters}>清空</BakeButton>
                  <BakeButton compact primary type="submit">搜索</BakeButton>
                  {onCreateSop && <BakeButton compact primary type="button" onClick={() => setShowCreateDialog(true)}>新建</BakeButton>}
                </div>
              </div>
            </div>
          </div>
      </form>
      <BakeRecordTable
        items={candidates}
        total={total}
        limit={limit}
        offset={offset}
        columns={columns}
        getRowId={item => item.id}
        ariaLabel="操作表格"
        emptyTitle={hasActiveFilters ? '没有匹配的操作' : '还没有操作'}
        emptyDescription={hasActiveFilters ? '调整关键词或时间范围后再试。' : '新建操作后，它会展示在这里。'}
        activeId={drawerMode ? selected?.id : null}
        itemLabel="条操作"
        onPageChange={onPageChange}
        onLimitChange={onLimitChange}
        renderActions={item => (
          <>
            <BakeTableActionButton kind="detail" label={`查看操作：${item.extractedProblem || '未命名操作'}`} onClick={trigger => openDrawer(item, 'detail', trigger)} />
            {onUpdateSop && <BakeTableActionButton kind="edit" label={`编辑操作：${item.extractedProblem || '未命名操作'}`} onClick={trigger => openDrawer(item, 'edit', trigger)} />}
            {onOpenGraph && <BakeTableActionButton kind="graph" label={`在记忆图谱中查看操作：${item.extractedProblem || '未命名操作'}`} onClick={() => onOpenGraph(item)} />}
          </>
        )}
      />

      <BakeDetailDrawer
        open={Boolean(drawerMode && selected)}
        wide
        eyebrow={drawerMode === 'edit' ? '编辑操作' : '操作详情'}
        title={selected?.extractedProblem || '未命名操作'}
        meta={selected ? <>ID #{selected.id} · 创建于 {formatCreatedTime(selected)} · {selected.steps.length} 步</> : undefined}
        closeLabel="关闭操作详情"
        onClose={closeDrawer}
        footer={selected && (drawerMode === 'edit' ? (
          <>
            <BakeButton disabled={isSaving} onClick={cancelEditing}>取消</BakeButton>
            <BakeButton primary disabled={isSaving} onClick={handleUpdate}>{isSaving ? '保存中…' : '保存'}</BakeButton>
          </>
        ) : (
          <>
            {onToggleFavorite && <BakeFavoriteButton isFavorite={Boolean(selected.isFavorite)} busy={favoriteBusy} onToggle={handleToggleFavorite} />}
            {onOpenGraph && <BakeButton onClick={() => { closeDrawer(); onOpenGraph(selected) }}>记忆图谱</BakeButton>}
            {selected.sourceTimelineId && <BakeButton onClick={() => onViewSourceTimeline(selected.sourceTimelineId)}>关联时间线</BakeButton>}
            <BakeButton danger onClick={() => {
              void Promise.resolve(onDeleteSop(selected.id)).then(deleted => {
                if (deleted !== false) closeDrawer()
              })
            }}>删除</BakeButton>
            {onUpdateSop && <BakeButton primary onClick={() => setDrawerMode('edit')}>编辑</BakeButton>}
          </>
        ))}
      >
        {selected && (drawerMode === 'edit' ? (
          <div className="bake-document-editor">
            <label className="bake-form-field">
              <span className="bake-kv__title">操作名称</span>
              <input
                className="bake-title-input"
                aria-label="操作名称"
                value={draftSop.extractedProblem}
                onChange={(event) => setDraftSop(previous => ({ ...previous, extractedProblem: event.target.value }))}
                placeholder="操作名称"
              />
            </label>
            <div className="bake-form-field">
              <span className="bake-kv__title">适用场景</span>
              {draftSop.triggerKeywords.map((keyword, index) => (
                <div key={index} className="bake-repeatable-field">
                  <input
                    className="bake-input"
                    value={keyword}
                    onChange={(event) => setDraftSop(previous => ({
                      ...previous,
                      triggerKeywords: previous.triggerKeywords.map((item, itemIndex) => itemIndex === index ? event.target.value : item),
                    }))}
                    placeholder={`场景 ${index + 1}`}
                  />
                  {draftSop.triggerKeywords.length > 1 && <BakeButton compact onClick={() => setDraftSop(previous => ({
                    ...previous,
                    triggerKeywords: previous.triggerKeywords.filter((_, itemIndex) => itemIndex !== index),
                  }))}>删除</BakeButton>}
                </div>
              ))}
              <BakeButton compact onClick={() => setDraftSop(previous => ({ ...previous, triggerKeywords: [...previous.triggerKeywords, ''] }))}>+ 添加适用场景</BakeButton>
            </div>
            <div className="bake-form-field">
              <span className="bake-kv__title">操作步骤</span>
              {draftSop.steps.map((step, index) => (
                <div key={index} className="bake-repeatable-field">
                  <input
                    className="bake-input"
                    value={step}
                    onChange={(event) => setDraftSop(previous => ({
                      ...previous,
                      steps: previous.steps.map((item, itemIndex) => itemIndex === index ? event.target.value : item),
                    }))}
                    placeholder={`步骤 ${index + 1}`}
                  />
                  {draftSop.steps.length > 1 && <BakeButton compact onClick={() => setDraftSop(previous => ({
                    ...previous,
                    steps: previous.steps.filter((_, itemIndex) => itemIndex !== index),
                  }))}>删除</BakeButton>}
                </div>
              ))}
              <BakeButton compact onClick={() => setDraftSop(previous => ({ ...previous, steps: [...previous.steps, ''] }))}>+ 添加步骤</BakeButton>
            </div>
            <div className="bake-form-field">
              <span className="bake-kv__title">详细描述</span>
              <BakeRichTextEditor
                value={draftSop.detailedContent}
                onChange={(value) => setDraftSop(previous => ({ ...previous, detailedContent: value }))}
                ariaLabel="操作详细描述"
                placeholder="输入适用场景、注意事项和验证方式…"
              />
            </div>
          </div>
        ) : (
          <div className="bake-kv bake-knowledge-detail">
            <section className="bake-knowledge-detail__section">
              <div className="bake-kv__title">适用场景</div>
              <div className="bake-memory-detail__stats">
                {selected.triggerKeywords.length > 0 ? selected.triggerKeywords.map(keyword => (
                  <span key={keyword} className="bake-stat-chip">{keyword}</span>
                )) : <span className="bake-muted">暂无适用场景</span>}
              </div>
            </section>
            <section className="bake-knowledge-detail__section">
              <div className="bake-kv__title">处理步骤</div>
              <div className="bake-list">
                {selected.steps.length > 0 ? selected.steps.map((step, index) => (
                  <div key={`${selected.id}-${index}`} className="bake-list-item">
                    <div className="bake-muted">{index + 1}. {step}</div>
                  </div>
                )) : <div className="bake-muted">暂无处理步骤</div>}
              </div>
            </section>
            <section className="bake-knowledge-detail__section">
              <div className="bake-kv__title">详细描述</div>
              <BakeMarkdown content={selected.detailedContent} />
            </section>
            <div className="bake-related-summary">
              <div className="bake-related-row">
                <span className="bake-related-row__label">关联时间线</span>
                <span className="bake-related-row__value">{sourceTimelineTitle || (selected.sourceTimelineId ? `时间线 #${selected.sourceTimelineId}` : '暂无')}</span>
              </div>
            </div>
          </div>
        ))}
      </BakeDetailDrawer>
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
              <div className="bake-form-field">
                <span className="bake-form-label">详细说明</span>
                <BakeRichTextEditor
                  value={newSop.detailedContent}
                  onChange={(value) => setNewSop({ ...newSop, detailedContent: value })}
                  ariaLabel="新操作详细说明"
                  placeholder="输入适用场景、注意事项和验证方式…"
                />
              </div>
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
                <span className="bake-form-label">适用场景</span>
                {newSop.triggerKeywords.map((keyword, index) => (
                  <div key={index} style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                    <input
                      className="bake-input"
                      value={keyword}
                      onChange={(e) => updateKeyword(index, e.target.value)}
                      placeholder={`场景 ${index + 1}`}
                      style={{ flex: 1 }}
                    />
                    {newSop.triggerKeywords.length > 1 && (
                      <BakeButton compact onClick={() => removeKeyword(index)}>删除</BakeButton>
                    )}
                  </div>
                ))}
                <BakeButton compact onClick={addKeyword}>+ 添加适用场景</BakeButton>
              </div>
            </div>
            <div className="bake-modal__footer">
              <BakeButton disabled={isSaving} onClick={() => setShowCreateDialog(false)}>取消</BakeButton>
              <BakeButton
                primary
                onClick={handleCreate}
                disabled={isSaving || !newSop.extractedProblem.trim() || newSop.steps.filter(s => s.trim()).length === 0}
              >
                {isSaving ? '保存中…' : '保存'}
              </BakeButton>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default BakeSopTab
