import React, { useEffect, useRef, useState } from 'react'
import type { BakeKnowledgeItem, MemoryFavoriteFilter } from '../../types'
import { BakeFavoriteButton, BakeFavoriteFilterControl } from './BakeFavoriteControls'
import BakeRichTextEditor from './BakeRichTextEditor'
import { BakeDetailDrawer, BakeRecordTable, BakeTableActionButton, type BakeRecordColumn } from './BakeRecordTable'
import { BakeButton, BakeMarkdown } from './BakeShared'

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
  favoriteFilter?: MemoryFavoriteFilter
  onFavoriteFilterChange?: (value: MemoryFavoriteFilter) => void
  onToggleFavorite?: (item: BakeKnowledgeItem, isFavorite: boolean) => boolean | Promise<boolean>
  onOpenGraph?: (knowledge: BakeKnowledgeItem) => void
  onDeleteKnowledge: (id: string) => void | boolean | Promise<boolean>
  onViewSourceTimeline: (timelineId?: string) => void
  sourceTimelineTitle?: string
  onCreateKnowledge?: (knowledge: Pick<BakeKnowledgeItem, 'summary' | 'overview' | 'detailedContent' | 'importance'>) => boolean | Promise<boolean>
  onUpdateKnowledge?: (knowledge: BakeKnowledgeItem) => boolean | Promise<boolean>
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
  favoriteFilter = 'all',
  onFavoriteFilterChange,
  onToggleFavorite,
  onOpenGraph,
  onDeleteKnowledge,
  onViewSourceTimeline,
  sourceTimelineTitle,
  onCreateKnowledge,
  onUpdateKnowledge,
  focusId,
}) => {
  const selected = items.find(item => item.id === selectedKnowledgeId) ?? items[0]
  const hasActiveFilters = Boolean(query.trim() || from || to || focusId || favoriteFilter !== 'all')
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [drawerMode, setDrawerMode] = useState<'detail' | 'edit' | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [favoriteBusy, setFavoriteBusy] = useState(false)
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null)
  const [newKnowledge, setNewKnowledge] = useState({
    summary: '',
    overview: '',
    detailedContent: '',
    importance: 5,
  })
  const [draftKnowledge, setDraftKnowledge] = useState({
    summary: '',
    overview: '',
    detailedContent: '',
    importance: 5,
  })

  useEffect(() => {
    setDraftKnowledge({
      summary: selected?.summary || '',
      overview: selected?.overview || '',
      detailedContent: selected?.detailedContent || '',
      importance: selected?.importance || 5,
    })
  }, [selected])

  useEffect(() => {
    if (focusId && selected?.id === focusId) setDrawerMode('detail')
  }, [focusId, selected?.id])

  const openDrawer = (item: BakeKnowledgeItem, mode: 'detail' | 'edit', trigger: HTMLButtonElement) => {
    detailTriggerRef.current = trigger
    onSelectKnowledge(item.id)
    setDrawerMode(mode)
  }

  const closeDrawer = () => {
    const trigger = detailTriggerRef.current
    setDrawerMode(null)
    onSelectKnowledge(null)
    window.setTimeout(() => trigger?.focus(), 0)
  }

  const handleCreate = async () => {
    if (!newKnowledge.summary.trim()) return
    setIsSaving(true)
    const created = await onCreateKnowledge?.({ ...newKnowledge })
    setIsSaving(false)
    if (created === false) return
    setShowCreateDialog(false)
    setNewKnowledge({
      summary: '',
      overview: '',
      detailedContent: '',
      importance: 5,
    })
  }

  const handleUpdate = async () => {
    if (!selected || !draftKnowledge.summary.trim() || !onUpdateKnowledge) return
    setIsSaving(true)
    const saved = await onUpdateKnowledge({
      ...selected,
      ...draftKnowledge,
      summary: draftKnowledge.summary.trim(),
      overview: draftKnowledge.overview.trim(),
      detailedContent: draftKnowledge.detailedContent.trim(),
    })
    setIsSaving(false)
    if (saved !== false) setDrawerMode('detail')
  }

  const cancelEditing = () => {
    setDraftKnowledge({
      summary: selected?.summary || '',
      overview: selected?.overview || '',
      detailedContent: selected?.detailedContent || '',
      importance: selected?.importance || 5,
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

  const columns: BakeRecordColumn<BakeKnowledgeItem>[] = [
    {
      key: 'created',
      label: '创建时间',
      className: 'bake-record-table__time',
      render: item => <><div>{formatCreatedTime(item)}</div><div className="bake-record-table__secondary">ID #{item.id}</div></>,
    },
    {
      key: 'title',
      label: '知识标题',
      className: 'bake-record-table__title',
      render: item => <div className="bake-record-table__primary bake-line-clamp-2">{item.summary}</div>,
    },
    {
      key: 'overview',
      label: '概述',
      render: item => <div className="bake-record-table__preview bake-line-clamp-2">{item.overview || '暂无概述'}</div>,
    },
  ]

  return (
    <>
      <form className="bake-list-toolbar bake-list-toolbar--repository" onSubmit={(event) => { event.preventDefault(); onSearch() }}>
        <div className="bake-list-toolbar__repository">
          <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--search">
            <label className="bake-form-field bake-filter-field bake-filter-field--search">
              <span className="bake-filter-label">关键词</span>
              <input className="bake-input" value={draftQuery} onChange={(event) => onDraftQueryChange(event.target.value)} placeholder="搜索知识标题、内容、分类或来源 URL" />
            </label>
          </div>
          <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--asset-filters bake-list-toolbar__repository-row--asset-filters-simple">
            {onFavoriteFilterChange && <BakeFavoriteFilterControl value={favoriteFilter} onChange={onFavoriteFilterChange} />}
            <label className="bake-form-field bake-filter-field bake-filter-field--date">
              <span className="bake-filter-label">起始时间</span>
              <input className="bake-input" type="date" value={draftFrom} onChange={(event) => onDraftFromChange(event.target.value)} />
            </label>
            <label className="bake-form-field bake-filter-field bake-filter-field--date">
              <span className="bake-filter-label">结束时间</span>
              <input className="bake-input" type="date" value={draftTo} onChange={(event) => onDraftToChange(event.target.value)} />
            </label>
            <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
              <div className="bake-list-toolbar__repository-primary-actions">
                <BakeButton compact type="button" onClick={onClearFilters}>清空</BakeButton>
                <BakeButton compact primary type="submit">搜索</BakeButton>
                {onCreateKnowledge && <BakeButton compact primary type="button" onClick={() => setShowCreateDialog(true)}>新建</BakeButton>}
              </div>
            </div>
          </div>
        </div>
      </form>

      <BakeRecordTable
        items={items}
        total={total}
        limit={limit}
        offset={offset}
        columns={columns}
        getRowId={item => item.id}
        ariaLabel="知识表格"
        emptyTitle={hasActiveFilters ? '没有符合条件的知识' : '暂无知识'}
        emptyDescription={hasActiveFilters ? '请调整关键词或时间范围。' : '点击“新建知识”添加第一条知识。'}
        activeId={drawerMode ? selected?.id : null}
        itemLabel="条知识"
        onPageChange={onPageChange}
        onLimitChange={onLimitChange}
        renderActions={item => <>
          <BakeTableActionButton kind="detail" label={`查看知识「${item.summary}」详情`} onClick={(trigger) => openDrawer(item, 'detail', trigger)} />
          {onUpdateKnowledge && <BakeTableActionButton kind="edit" label={`编辑知识「${item.summary}」`} onClick={(trigger) => openDrawer(item, 'edit', trigger)} />}
          {onOpenGraph && <BakeTableActionButton kind="graph" label={`在记忆图谱中查看知识「${item.summary}」`} onClick={() => onOpenGraph(item)} />}
        </>}
      />

      <BakeDetailDrawer
        open={Boolean(drawerMode && selected)}
        wide
        eyebrow={drawerMode === 'edit' ? '编辑知识' : '知识详情'}
        title={selected?.summary || '知识'}
        meta={selected ? <>ID #{selected.id} · 创建于 {formatCreatedTime(selected)} · 重要度 {selected.importance}/10</> : undefined}
        ariaLabel={selected?.summary || '知识详情'}
        closeLabel="关闭知识详情"
        onClose={closeDrawer}
        footer={selected && (drawerMode === 'edit' ? <>
          <BakeButton disabled={isSaving} onClick={cancelEditing}>取消</BakeButton>
          <BakeButton primary disabled={isSaving || !draftKnowledge.summary.trim()} onClick={handleUpdate}>{isSaving ? '保存中…' : '保存'}</BakeButton>
        </> : <>
          {onToggleFavorite && <BakeFavoriteButton isFavorite={Boolean(selected.isFavorite)} busy={favoriteBusy} onToggle={handleToggleFavorite} />}
          {onOpenGraph && <BakeButton onClick={() => { closeDrawer(); onOpenGraph(selected) }}>记忆图谱</BakeButton>}
          {selected.sourceTimelineId && <BakeButton onClick={() => onViewSourceTimeline(selected.sourceTimelineId)}>关联时间线</BakeButton>}
          <BakeButton danger onClick={() => {
            void Promise.resolve(onDeleteKnowledge(selected.id)).then(deleted => {
              if (deleted !== false) closeDrawer()
            })
          }}>删除知识</BakeButton>
          {onUpdateKnowledge && <BakeButton primary onClick={() => setDrawerMode('edit')}>编辑</BakeButton>}
        </>)}
      >
        {selected && <div className="bake-kv bake-capture-detail bake-knowledge-detail">
          {drawerMode === 'edit' ? <div className="bake-document-editor">
            <label className="bake-form-field">
              <span className="bake-kv__title">知识标题</span>
              <input className="bake-title-input" aria-label="知识标题" value={draftKnowledge.summary} onChange={(event) => setDraftKnowledge(previous => ({ ...previous, summary: event.target.value }))} placeholder="知识标题" />
            </label>
            <label className="bake-form-field">
              <span className="bake-kv__title">概述</span>
              <textarea className="bake-textarea" rows={3} value={draftKnowledge.overview} onChange={(event) => setDraftKnowledge(previous => ({ ...previous, overview: event.target.value }))} placeholder="概括这条知识" />
            </label>
            <div className="bake-form-field">
              <span className="bake-kv__title">详细内容</span>
              <BakeRichTextEditor value={draftKnowledge.detailedContent} onChange={(value) => setDraftKnowledge(previous => ({ ...previous, detailedContent: value }))} ariaLabel="知识详细内容" placeholder="输入知识的详细内容…" />
            </div>
            <label className="bake-form-field bake-select-field">
              <span className="bake-kv__title">重要度</span>
              <select className="bake-input" value={draftKnowledge.importance} onChange={(event) => setDraftKnowledge(previous => ({ ...previous, importance: Number(event.target.value) }))}>
                {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(value => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
          </div> : <>
            <div className="bake-related-summary">
              <div className="bake-related-row"><span className="bake-related-row__label">状态</span><span className="bake-related-row__value">{selected.status === 'confirmed' ? '已确认' : selected.status === 'ignored' ? '已忽略' : '待确认'}</span></div>
              <div className="bake-related-row"><span className="bake-related-row__label">重要度</span><span className="bake-related-row__value">{selected.importance}/10</span></div>
            </div>
            {selected.sourceUrl && <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">来源网址</div>
              <a href={selected.sourceUrl} target="_blank" rel="noopener noreferrer" className="bake-source-url-link">{selected.sourceUrl}</a>
            </div>}
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">概述</div>
              <div className="bake-muted" style={{ lineHeight: 1.7 }}>{selected.overview || '暂无概述'}</div>
            </div>
            {selected.detailedContent && <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">详细内容</div>
              <BakeMarkdown content={selected.detailedContent} />
            </div>}
            <div className="bake-related-summary">
              <div className="bake-related-row"><span className="bake-related-row__label">关联时间线</span><span className="bake-related-row__value">{sourceTimelineTitle || (selected.sourceTimelineId ? `时间线 #${selected.sourceTimelineId}` : '暂无')}</span></div>
            </div>
          </>}
        </div>}
      </BakeDetailDrawer>
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
              <div className="bake-form-field">
                <span className="bake-form-label">详细内容</span>
                <BakeRichTextEditor
                  value={newKnowledge.detailedContent}
                  onChange={(value) => setNewKnowledge({ ...newKnowledge, detailedContent: value })}
                  ariaLabel="新知识详细内容"
                  placeholder="输入知识的详细内容…"
                />
              </div>
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
              <BakeButton disabled={isSaving} onClick={() => setShowCreateDialog(false)}>取消</BakeButton>
              <BakeButton primary onClick={handleCreate} disabled={isSaving || !newKnowledge.summary.trim()}>{isSaving ? '保存中…' : '保存'}</BakeButton>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default BakeKnowledgeTab
