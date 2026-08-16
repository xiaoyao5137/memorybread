import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { ArticleTemplate, MemoryFavoriteFilter } from '../../types'
import type { LocalCreationSkill } from '../../utils/creationSkills'
import BakeDocumentCategoryPicker, { documentCategoryLabel, documentCategoryOptions } from './BakeDocumentCategoryPicker'
import { BakeFavoriteButton, BakeFavoriteFilterControl } from './BakeFavoriteControls'
import BakeRichTextEditor from './BakeRichTextEditor'
import { BakeDetailDrawer, BakeRecordTable, BakeTableActionButton, type BakeRecordColumn } from './BakeRecordTable'
import { BakeButton, BakeMarkdown } from './BakeShared'

const formatTemplateTime = (timestamp?: number, fallback?: string) => {
  if (timestamp && timestamp > 0) {
    return new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
  }
  return fallback || '—'
}

const documentPreview = (template: ArticleTemplate) => (
  (template.summary || template.fullContent || template.promptHint || '暂无内容')
    .replace(/[#>*_`\[\]()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
)

const documentCategoryLabels = (value?: string) => {
  const categories = (value || '').split(/[、,，/|;；]+/).map(item => item.trim()).filter(Boolean)
  return [...new Set((categories.length ? categories : [value || '']).map(documentCategoryLabel))]
}

const documentStatusLabel = (status: ArticleTemplate['status']) => (
  status === 'enabled' ? '已启用' : status === 'draft' ? '草稿' : status === 'disabled' ? '已停用' : '待确认'
)

const BakeTemplatesTab: React.FC<{
  templates: ArticleTemplate[]
  total: number
  limit: number
  offset: number
  query: string
  from: string
  to: string
  docType?: string
  draftQuery: string
  draftFrom: string
  draftTo: string
  draftDocType?: string
  selectedTemplateId: string | null
  onSelectTemplate: (id: string | null) => void
  onCreateTemplate: (input: Pick<ArticleTemplate, 'title' | 'docType' | 'fullContent'>) => boolean | Promise<boolean>
  onUpdateTemplate: (templateId: string, updater: (template: ArticleTemplate) => ArticleTemplate) => void | boolean | Promise<void | boolean>
  onToggleTemplateStatus: (templateId: string) => void
  onDeleteTemplate: (templateId: string) => void | boolean | Promise<boolean>
  onSettleSkill?: (template: ArticleTemplate) => void
  relatedSkills?: LocalCreationSkill[]
  onOpenSkill?: (skill: LocalCreationSkill) => void
  onViewSourceMemory: (memoryId?: string) => void
  memoryTitleById?: Map<string, string>
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onDraftDocTypeChange?: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  favoriteFilter?: MemoryFavoriteFilter
  onFavoriteFilterChange?: (value: MemoryFavoriteFilter) => void
  onToggleFavorite?: (item: ArticleTemplate, isFavorite: boolean) => boolean | Promise<boolean>
  onOpenGraph?: (template: ArticleTemplate) => void
  focusId?: string | null
}> = ({
  templates,
  total,
  limit,
  offset,
  query,
  from,
  to,
  docType = '',
  draftQuery,
  draftFrom,
  draftTo,
  draftDocType = '',
  selectedTemplateId,
  onSelectTemplate,
  onCreateTemplate,
  onUpdateTemplate,
  onToggleTemplateStatus,
  onDeleteTemplate,
  onSettleSkill,
  relatedSkills = [],
  onOpenSkill,
  onViewSourceMemory,
  onPageChange,
  onLimitChange,
  onDraftQueryChange,
  onDraftFromChange,
  onDraftToChange,
  onDraftDocTypeChange = () => undefined,
  onSearch,
  onClearFilters,
  favoriteFilter = 'all',
  onFavoriteFilterChange,
  onToggleFavorite,
  onOpenGraph,
  focusId,
}) => {
  const selected = templates.find(item => item.id === selectedTemplateId) ?? templates[0]
  const [drawerMode, setDrawerMode] = useState<'detail' | 'edit' | null>(null)
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [favoriteBusy, setFavoriteBusy] = useState(false)
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null)
  const hasActiveFilters = Boolean(query.trim() || from || to || docType || focusId || favoriteFilter !== 'all')

  const editingValues = useMemo(() => ({
    name: selected?.title || '',
    category: selected?.docType || '',
    content: selected?.fullContent || selected?.promptHint || '',
  }), [selected])

  const [draftName, setDraftName] = useState('')
  const [draftCategory, setDraftCategory] = useState('')
  const [draftContent, setDraftContent] = useState('')
  const [newDocument, setNewDocument] = useState({
    title: '新文档',
    docType: 'general_document',
    fullContent: '',
  })

  useEffect(() => {
    setDraftName(editingValues.name)
    setDraftCategory(editingValues.category)
    setDraftContent(editingValues.content)
  }, [editingValues])

  useEffect(() => {
    if (focusId && selected?.id === focusId) setDrawerMode('detail')
  }, [focusId, selected?.id])

  const openDrawer = (item: ArticleTemplate, mode: 'detail' | 'edit', trigger: HTMLButtonElement) => {
    detailTriggerRef.current = trigger
    onSelectTemplate(item.id)
    setDrawerMode(mode)
  }

  const closeDrawer = () => {
    const trigger = detailTriggerRef.current
    setDrawerMode(null)
    onSelectTemplate(null)
    window.setTimeout(() => trigger?.focus(), 0)
  }

  const handleSave = async () => {
    if (!selected) return
    setIsSaving(true)
    try {
      const result = await onUpdateTemplate(selected.id, template => ({
        ...template,
        title: draftName.trim() || template.title,
        docType: draftCategory || template.docType,
        fullContent: draftContent.trim(),
        updatedAt: new Date().toLocaleString('zh-CN', { hour12: false }),
        updatedAtMs: Date.now(),
      }))
      if (result !== false) setDrawerMode('detail')
    } finally {
      setIsSaving(false)
    }
  }

  const cancelEditing = () => {
    setDraftName(editingValues.name)
    setDraftCategory(editingValues.category)
    setDraftContent(editingValues.content)
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

  const closeCreateDialog = () => {
    if (isSaving) return
    setShowCreateDialog(false)
    setNewDocument({ title: '新文档', docType: 'general_document', fullContent: '' })
  }

  const handleCreate = async () => {
    const title = newDocument.title.trim()
    const docType = newDocument.docType.trim()
    if (!title || !docType) return
    setIsSaving(true)
    try {
      const created = await onCreateTemplate({
        title,
        docType,
        fullContent: newDocument.fullContent.trim(),
      })
      if (created !== false) {
        setShowCreateDialog(false)
        setNewDocument({ title: '新文档', docType: 'general_document', fullContent: '' })
      }
    } finally {
      setIsSaving(false)
    }
  }

  const columns: BakeRecordColumn<ArticleTemplate>[] = [
    {
      key: 'created',
      label: '创建时间',
      className: 'bake-record-table__time',
      render: item => <><div>{formatTemplateTime(item.createdAtMs, item.createdAt)}</div><div className="bake-record-table__secondary">ID #{item.id}</div></>,
    },
    {
      key: 'title',
      label: '文档名称',
      className: 'bake-record-table__title',
      render: item => <div className="bake-record-table__primary bake-line-clamp-2">{item.title}</div>,
    },
    {
      key: 'category',
      label: '分类',
      className: 'bake-record-table__category',
      render: item => <div className="bake-record-table__tag-list">{documentCategoryLabels(item.docType).map(category => <span key={category} className="bake-record-table__badge">{category}</span>)}</div>,
    },
    {
      key: 'content',
      label: '内容摘要',
      render: item => <div className="bake-record-table__preview bake-line-clamp-3">{documentPreview(item)}</div>,
    },
  ]

  return (
    <>
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
              <input className="bake-input" value={draftQuery} onChange={(event) => onDraftQueryChange(event.target.value)} placeholder="搜索文档名称、内容或来源 URL" />
            </label>
          </div>
          <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--asset-filters">
            <label className="bake-form-field bake-filter-field bake-filter-field--type bake-filter-field--select">
              <span className="bake-filter-label">文档类型</span>
              <select className="bake-input" value={draftDocType} onChange={(event) => onDraftDocTypeChange(event.target.value)} aria-label="文档类型">
                <option value="">全部类型</option>
                {documentCategoryOptions.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
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
                <BakeButton compact primary type="button" onClick={() => setShowCreateDialog(true)}>新建</BakeButton>
              </div>
            </div>
          </div>
        </div>
      </form>

      <BakeRecordTable
        items={templates}
        total={total}
        limit={limit}
        offset={offset}
        columns={columns}
        getRowId={item => item.id}
        ariaLabel="文档表格"
        emptyTitle={hasActiveFilters ? '没有符合条件的文档' : '暂无文档'}
        emptyDescription={hasActiveFilters ? '请调整关键词、文档类型或时间范围。' : '点击“新建文档”创建第一份文档。'}
        activeId={drawerMode ? selected?.id : null}
        itemLabel="条文档"
        onPageChange={onPageChange}
        onLimitChange={onLimitChange}
        renderActions={item => <>
          <BakeTableActionButton kind="detail" label={`查看文档「${item.title}」详情`} onClick={(trigger) => openDrawer(item, 'detail', trigger)} />
          <BakeTableActionButton kind="edit" label={`编辑文档「${item.title}」`} onClick={(trigger) => openDrawer(item, 'edit', trigger)} />
          {onOpenGraph && <BakeTableActionButton kind="graph" label={`在记忆图谱中查看文档「${item.title}」`} onClick={() => onOpenGraph(item)} />}
        </>}
      />

      <BakeDetailDrawer
        open={Boolean(drawerMode && selected)}
        wide
        eyebrow={drawerMode === 'edit' ? '编辑文档' : '文档详情'}
        title={selected?.title || '文档'}
        meta={selected ? <>{documentCategoryLabels(selected.docType).join('、')} · ID #{selected.id} · 创建时间 {formatTemplateTime(selected.createdAtMs, selected.createdAt)} · 最近更新 {formatTemplateTime(selected.updatedAtMs, selected.updatedAt)}</> : undefined}
        ariaLabel={selected?.title || '文档详情'}
        closeLabel="关闭文档详情"
        onClose={closeDrawer}
        footer={selected && (drawerMode === 'edit' ? <>
          <BakeButton disabled={isSaving} onClick={cancelEditing}>取消</BakeButton>
          <BakeButton primary disabled={isSaving} onClick={handleSave}>{isSaving ? '保存中…' : '保存'}</BakeButton>
        </> : <>
          {onToggleFavorite && <BakeFavoriteButton isFavorite={Boolean(selected.isFavorite)} busy={favoriteBusy} onToggle={handleToggleFavorite} />}
          {onOpenGraph && <BakeButton onClick={() => { closeDrawer(); onOpenGraph(selected) }}>记忆图谱</BakeButton>}
          {selected.sourceMemoryIds[0] && <BakeButton compact onClick={() => onViewSourceMemory(selected.sourceMemoryIds[0])}>来源时间线</BakeButton>}
          {onSettleSkill && <BakeButton onClick={() => onSettleSkill(selected)}>沉淀技能</BakeButton>}
          <BakeButton onClick={() => onToggleTemplateStatus(selected.id)}>{selected.status === 'enabled' ? '停用' : '启用'}</BakeButton>
          <BakeButton danger onClick={() => {
            void Promise.resolve(onDeleteTemplate(selected.id)).then(deleted => {
              if (deleted !== false) closeDrawer()
            })
          }}>删除</BakeButton>
          <BakeButton primary onClick={() => setDrawerMode('edit')}>编辑</BakeButton>
        </>)}
      >
        {selected && <div className="bake-kv bake-knowledge-detail">
          {drawerMode === 'edit' ? <div className="bake-document-editor">
            <label className="bake-form-field">
              <span className="bake-kv__title">文档名称</span>
              <input className="bake-title-input" aria-label="文档名称" value={draftName} onChange={(event) => setDraftName(event.target.value)} placeholder="文档名称" />
            </label>
            <label className="bake-form-field">
              <span className="bake-kv__title">文档分类</span>
              <BakeDocumentCategoryPicker value={draftCategory} onChange={setDraftCategory} />
            </label>
            <div className="bake-form-field">
              <span className="bake-kv__title">文档内容</span>
              <BakeRichTextEditor value={draftContent} onChange={setDraftContent} ariaLabel="文档内容" placeholder="输入文档内容…" />
            </div>
          </div> : <>
            <div className="bake-related-summary">
              <div className="bake-related-row"><span className="bake-related-row__label">状态</span><span className="bake-related-row__value">{documentStatusLabel(selected.status)}</span></div>
              <div className="bake-related-row"><span className="bake-related-row__label">分类</span><span className="bake-related-row__value">{documentCategoryLabels(selected.docType).join('、')}</span></div>
            </div>
            {selected.sourceUrl && <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">来源网址</div>
              <a href={selected.sourceUrl} target="_blank" rel="noopener noreferrer" className="bake-source-url-link">{selected.sourceUrl}</a>
            </div>}
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">文档内容</div>
              <BakeMarkdown content={selected.fullContent || selected.promptHint} />
            </div>
            <div className="bake-knowledge-detail__section bake-related-skills">
              <div className="bake-kv__title">关联技能</div>
              {relatedSkills.length ? <div className="bake-related-skills__list">
                {relatedSkills.map(skill => <button type="button" key={skill.id} onClick={() => onOpenSkill?.(skill)}>
                  <span><strong>{skill.title}</strong><small>{skill.summary}</small></span>
                  <em>{skill.status === 'draft' ? '草稿' : skill.installed ? '已安装' : '已保存'}</em>
                </button>)}
              </div> : <div className="bake-muted">这份文档还没有关联技能，可点击下方“沉淀技能”创建。</div>}
            </div>
          </>}
        </div>}
      </BakeDetailDrawer>
      {showCreateDialog && (
        <div className="bake-modal-overlay" onClick={closeCreateDialog}>
          <div
            className="bake-modal bake-modal--document"
            role="dialog"
            aria-modal="true"
            aria-labelledby="bake-new-document-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="bake-modal__header">
              <h3 id="bake-new-document-title">新建文档</h3>
              <button className="bake-modal__close" type="button" aria-label="关闭" onClick={closeCreateDialog}>×</button>
            </div>
            <div className="bake-modal__body">
              <label className="bake-form-field">
                <span className="bake-form-label">文档名称 *</span>
                <input
                  className="bake-title-input bake-title-input--modal"
                  aria-label="新文档名称"
                  value={newDocument.title}
                  onChange={(event) => setNewDocument(previous => ({ ...previous, title: event.target.value }))}
                  placeholder="文档名称"
                  autoFocus
                />
              </label>
              <div className="bake-form-field">
                <span className="bake-form-label">文档分类 *</span>
                <BakeDocumentCategoryPicker
                  value={newDocument.docType}
                  onChange={(docType) => setNewDocument(previous => ({ ...previous, docType }))}
                  ariaLabel="新文档分类"
                />
              </div>
              <div className="bake-form-field">
                <span className="bake-form-label">文档内容</span>
                <BakeRichTextEditor
                  value={newDocument.fullContent}
                  onChange={(fullContent) => setNewDocument(previous => ({ ...previous, fullContent }))}
                  ariaLabel="新文档内容"
                  placeholder="输入文档内容…"
                />
              </div>
            </div>
            <div className="bake-modal__footer">
              <BakeButton disabled={isSaving} onClick={closeCreateDialog}>取消</BakeButton>
              <BakeButton
                primary
                disabled={isSaving || !newDocument.title.trim() || !newDocument.docType.trim()}
                onClick={handleCreate}
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

export default BakeTemplatesTab
