import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useAppStore } from '../../store/useAppStore'
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

type DocumentRefreshPolicy = 'auto' | 'always' | 'never'

const REFRESH_POLICY_OPTIONS: Array<{ value: DocumentRefreshPolicy; label: string }> = [
  { value: 'auto', label: '自动判断' },
  { value: 'always', label: '每次都刷新' },
  { value: 'never', label: '从不刷新' },
]

const refreshPolicyLabel = (policy?: string) => (
  REFRESH_POLICY_OPTIONS.find(option => option.value === policy)?.label ?? '自动判断'
)

const refreshStatusLabel = (status?: string) => {
  if (status === 'fresh_complete') return '已验证完整快照'
  if (status === 'fresh_partial') return '已验证部分快照'
  if (status === 'unavailable') return '当前不可用'
  return '历史版本'
}

const refreshReasonLabels: Record<string, string> = {
  SOURCE_REFRESH_CANCELLED: '本次刷新已取消，原正文保留',
  SOURCE_REFRESH_PAUSED: '来源刷新已暂停，待处理记录保留',
  SOURCE_WRITES_PAUSED: '来源写入已暂停，本次采集未更新正文',
  COVERAGE_UNVERIFIED: '尚未取得完整正文，原正文保留',
  AUTH_REQUIRED: '来源页面需要重新登录',
  FOCUS_POLICY_BLOCKED: '当前策略不允许切换到来源页面',
  PERMISSION_DENIED: '当前账号没有来源页面的读取权限',
  IDENTITY_MISMATCH: '打开的页面与文档来源不一致',
  PAGE_GONE: '来源页面已不存在',
  SCRAPE_EMPTY: '本次未取得有效正文',
  BODY_NOT_FOUND: '页面尚未加载出正文区域，请稍后重试',
  EXTRACTION_FAILED: '页面正文提取未完成，请稍后重试',
  NAVIGATION_TIMEOUT: '来源页面加载超时，请稍后重试',
  TAB_CLOSED: '来源页面在采集完成前关闭',
  BACKGROUND_TAB_BLOCKED: '浏览器未能创建后台读取页面',
  SCRAPE_TIMEOUT: '页面读取已达到时间预算',
  BROWSER_EXTENSION_UNAVAILABLE: '浏览器读取服务未连接',
  BROWSER_EXTENSION_TIMEOUT: '读取页面超时，可稍后重试',
  WORKER_INTERRUPTED: '上次采集中断，等待恢复',
  RETRY_LIMIT_REACHED: '已达到重试上限，请重新获取原文',
}
const refreshReasonLabel = (reason: string) => refreshReasonLabels[reason] || '本次未能完成来源检查，可稍后重试'

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
  onRefreshTemplate?: (templateId: string) => void | Promise<void>
  onLoadTemplate?: (templateId: string) => Promise<ArticleTemplate>
  onRetrySummary?: (templateId: string, expectedRevision?: number) => Promise<boolean>
  onCancelRefreshTemplate?: (templateId: string) => void | Promise<void>
  refreshingTemplateId?: string | null
  onSetTemplateRefreshPolicy?: (templateId: string, policy: DocumentRefreshPolicy) => boolean | Promise<boolean>
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
  onRefreshTemplate,
  onLoadTemplate,
  onRetrySummary,
  onCancelRefreshTemplate,
  refreshingTemplateId = null,
  onSetTemplateRefreshPolicy,
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
  const debugModeEnabled = useAppStore((state) => state.debugModeEnabled)
  const baseSelected = templates.find(item => item.id === selectedTemplateId) ?? templates[0]
  const [loadedTemplate, setLoadedTemplate] = useState<ArticleTemplate | null>(null)
  const selected = useMemo(() => baseSelected && loadedTemplate && loadedTemplate.id === baseSelected.id
    && (loadedTemplate.updatedAtMs ?? 0) >= (baseSelected.updatedAtMs ?? 0)
    ? { ...baseSelected, ...loadedTemplate, isFavorite: baseSelected.isFavorite }
    : baseSelected, [baseSelected, loadedTemplate])
  const isRefreshing = refreshingTemplateId != null && refreshingTemplateId === selected?.id
  const [drawerMode, setDrawerMode] = useState<'detail' | 'edit' | null>(null)
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [favoriteBusy, setFavoriteBusy] = useState(false)
  const [summaryRetrying, setSummaryRetrying] = useState(false)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [summaryRevision, setSummaryRevision] = useState(0)
  const summaryViewToken = useRef(0)
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null)
  const hasActiveFilters = Boolean(query.trim() || from || to || docType || focusId || favoriteFilter !== 'all')

  useEffect(() => {
    const token = ++summaryViewToken.current
    setSummaryRetrying(false)
    setSummaryError(null)
    let timer: number | undefined
    if (drawerMode === 'detail' && baseSelected?.id && onLoadTemplate) {
      const id = baseSelected.id
      const load = async () => {
        try {
          const item = await onLoadTemplate(id)
          if (summaryViewToken.current !== token || item.id !== id) return
          setLoadedTemplate(item)
          if (['pending', 'running'].includes(item.summaryStatus?.state || '')) {
            timer = window.setTimeout(() => void load(), 5000)
          }
        } catch {
          if (summaryViewToken.current === token) setSummaryError('摘要状态暂时无法更新，请重新打开文档重试。')
        }
      }
      void load()
    }
    return () => {
      summaryViewToken.current += 1
      window.clearTimeout(timer)
    }
  }, [drawerMode, baseSelected?.id, baseSelected?.updatedAtMs, onLoadTemplate, summaryRevision])

  const retrySummary = async (regenerate = false) => {
    if (!selected || !onRetrySummary || summaryRetrying) return
    const token = summaryViewToken.current
    setSummaryRetrying(true)
    setSummaryError(null)
    try {
      if (regenerate) await onRetrySummary(selected.id, selected.updatedAtMs)
      else await onRetrySummary(selected.id)
      if (summaryViewToken.current === token) setSummaryRevision(value => value + 1)
    } catch {
      if (summaryViewToken.current === token) setSummaryError('摘要重试未完成，请稍后再试。')
    } finally {
      if (summaryViewToken.current === token) setSummaryRetrying(false)
    }
  }

  const editingValues = useMemo(() => ({
    name: selected?.title || '',
    category: selected?.docType || '',
    content: selected?.fullContent || selected?.promptHint || '',
    refreshPolicy: (selected?.refreshPolicy ?? 'auto') as DocumentRefreshPolicy,
  }), [selected])

  const [draftName, setDraftName] = useState('')
  const [draftCategory, setDraftCategory] = useState('')
  const [draftContent, setDraftContent] = useState('')
  const [draftRefreshPolicy, setDraftRefreshPolicy] = useState<DocumentRefreshPolicy>('auto')
  const [newDocument, setNewDocument] = useState({
    title: '新文档',
    docType: 'general_document',
    fullContent: '',
  })

  useEffect(() => {
    setDraftName(editingValues.name)
    setDraftCategory(editingValues.category)
    setDraftContent(editingValues.content)
    setDraftRefreshPolicy(editingValues.refreshPolicy)
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
      if (result === false) return
      // 刷新策略走专用端点，不在文档全列更新里
      if (debugModeEnabled && onSetTemplateRefreshPolicy && draftRefreshPolicy !== editingValues.refreshPolicy) {
        const policyResult = await onSetTemplateRefreshPolicy(selected.id, draftRefreshPolicy)
        if (policyResult === false) return
      }
      setDrawerMode('detail')
    } finally {
      setIsSaving(false)
    }
  }

  const cancelEditing = () => {
    setDraftName(editingValues.name)
    setDraftCategory(editingValues.category)
    setDraftContent(editingValues.content)
    setDraftRefreshPolicy(editingValues.refreshPolicy)
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
              <input className="bake-input" value={draftQuery} onChange={(event) => onDraftQueryChange(event.target.value)} placeholder="搜索文档 ID、名称、内容或来源 URL" />
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
          </div>
          <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
            <div className="bake-list-toolbar__repository-primary-actions">
              <BakeButton compact type="button" onClick={onClearFilters}>清空</BakeButton>
              <BakeButton compact primary type="submit">搜索</BakeButton>
              <BakeButton compact primary type="button" onClick={() => setShowCreateDialog(true)}>新建</BakeButton>
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
          {debugModeEnabled && onRefreshTemplate && selected.sourceUrl && (
            <BakeButton disabled={isRefreshing} onClick={() => void Promise.resolve(onRefreshTemplate(selected.id))}>
              {isRefreshing ? '刷新中…' : '立即刷新'}
            </BakeButton>
          )}
          {onOpenGraph && <BakeButton onClick={() => { closeDrawer(); onOpenGraph(selected) }}>记忆图谱</BakeButton>}
          {debugModeEnabled && onCancelRefreshTemplate && (isRefreshing || ['pending', 'running'].includes(selected.sourceCollection?.state || '')) &&
            <BakeButton onClick={() => void onCancelRefreshTemplate(selected.id)}>取消本次刷新</BakeButton>}
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
            {debugModeEnabled && <label className="bake-form-field">
              <span className="bake-kv__title">即时刷新策略</span>
              <select
                className="bake-input"
                value={draftRefreshPolicy}
                onChange={(event) => setDraftRefreshPolicy(event.target.value as DocumentRefreshPolicy)}
                aria-label="即时刷新策略"
              >
                {REFRESH_POLICY_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <span className="bake-muted">创作召回时按策略自动检查来源页面并合入最新内容；“自动判断”会结合更新节奏与内容新鲜度决定是否打开浏览器。</span>
            </label>}
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
              <div className="bake-kv__title">摘要</div>
              {selected.summaryStatus && <div role="status" aria-label="摘要状态" className="bake-muted">
                {selected.summaryStatus.paused && ['pending', 'running'].includes(selected.summaryStatus.state)
                  ? '摘要生成已暂停，原文仍可查看'
                  : { ready: '摘要已根据当前原文生成', pending: '等待生成摘要', running: '正在生成摘要',
                    blocked: '摘要生成未完成，原文仍可查看', unverified: '摘要与当前原文尚未核对' }[selected.summaryStatus.state]}
                {selected.summaryStatus.state === 'unverified' && selected.summaryStatus.can_regenerate && onRetrySummary &&
                  <div>
                    <p>重新生成前会保留现有摘要记录。</p>
                    <BakeButton disabled={summaryRetrying || !selected.updatedAtMs} onClick={() => void retrySummary(true)}>{summaryRetrying ? '正在提交…' : '根据当前原文重新生成摘要'}</BakeButton>
                  </div>}
                {selected.summaryStatus.state === 'blocked' && onRetrySummary &&
                  <BakeButton disabled={summaryRetrying} onClick={() => void retrySummary()}>{summaryRetrying ? '正在提交…' : '重试摘要'}</BakeButton>}
              </div>}
              {summaryError && <p role="alert">{summaryError}</p>}
              {selected.summary ? <p>{selected.summary}</p> : <p className="bake-muted">暂无摘要</p>}
            </div>
            <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">文档内容</div>
              {selected.lastRefreshCompleteness !== 'complete' && <p className="bake-muted" role="status">
                {selected.lastRefreshCompleteness === 'partial'
                  ? '最近一次仅获取部分正文，未覆盖已有完整版本；引用时请核对实际取得的内容。'
                  : '当前内容来自历史采集，尚未校验原文完整性。可重新获取原文进行核对。'}
              </p>}
              {selected.contentFormat === 'plain_text'
                ? <div className="bake-source-plain-text">{selected.fullContent || selected.promptHint}</div>
                : <BakeMarkdown content={selected.fullContent || selected.promptHint} />}
            </div>
            {debugModeEnabled && <div className="bake-knowledge-detail__section">
              <div className="bake-kv__title">即时刷新</div>
              <div className="bake-related-summary">
                <div className="bake-related-row"><span className="bake-related-row__label">刷新策略</span><span className="bake-related-row__value">{refreshPolicyLabel(selected.refreshPolicy)}</span></div>
                <div className="bake-related-row"><span className="bake-related-row__label">上次检查</span><span className="bake-related-row__value">{formatTemplateTime(selected.lastRefreshCheckedAtMs, '尚未检查')}</span></div>
                <div className="bake-related-row"><span className="bake-related-row__label">校验状态</span><span className="bake-related-row__value">{refreshStatusLabel(selected.lastRefreshStatus)}</span></div>
                {selected.lastRefreshSuccessAtMs ? <div className="bake-related-row"><span className="bake-related-row__label">上次成功</span><span className="bake-related-row__value">{formatTemplateTime(selected.lastRefreshSuccessAtMs, '尚未成功')}</span></div> : null}
                {selected.lastRefreshCharacterCount ? <div className="bake-related-row"><span className="bake-related-row__label">采集范围</span><span className="bake-related-row__value">{selected.lastRefreshCharacterCount.toLocaleString()} 字符 · {selected.lastRefreshSegmentCount || 0} 段{selected.lastRefreshTruncated ? ' · 已截断' : ''}</span></div> : null}
                {selected.sourceCollection && <div role="status">
                  <div className="bake-related-row"><span className="bake-related-row__label">回访更新任务</span><span className="bake-related-row__value">{selected.sourceCollection.state === 'running' ? '正在采集原文' : selected.sourceCollection.state === 'blocked' ? '更新暂停，请重新获取原文' : selected.sourceCollection.state === 'completed' ? '已完成来源检查' : selected.sourceCollection.attempts > 0 ? '等待重试' : '等待采集'}</span></div>
                  {selected.sourceCollection.last_error && <div className="bake-related-row"><span className="bake-related-row__label">任务原因</span><span className="bake-related-row__value">{refreshReasonLabel(selected.sourceCollection.last_error)}</span></div>}
                  {selected.sourceCollection.state === 'pending' && selected.sourceCollection.next_attempt_at_ms > 0 && <div className="bake-related-row"><span className="bake-related-row__label">下次尝试不早于</span><span className="bake-related-row__value">{formatTemplateTime(selected.sourceCollection.next_attempt_at_ms)}</span></div>}
                </div>}
                {selected.lastRefreshError && <div className="bake-related-row"><span className="bake-related-row__label">上次未完成原因</span><span className="bake-related-row__value">{refreshReasonLabel(selected.lastRefreshError)}</span></div>}
                {!selected.sourceUrl && <div className="bake-related-row"><span className="bake-related-row__label">提示</span><span className="bake-related-row__value">没有来源网址，无法即时刷新</span></div>}
              </div>
            </div>}
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
