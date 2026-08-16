import React, { useEffect, useRef, useState } from 'react'
import type { DataSnapshot, DataSource, MemoryFavoriteFilter } from '../../types'
import { BakeFavoriteButton, BakeFavoriteFilterControl } from './BakeFavoriteControls'
import { BakeDetailDrawer, BakeRecordTable, BakeTableActionButton, type BakeRecordColumn } from './BakeRecordTable'
import { BakeButton, BakePill } from './BakeShared'

type DataMetricRow = {
  dimension: string
  metric: string
  value: string
  note: string
}

type DataPresentation = {
  title: string
  summary: string
  rows: DataMetricRow[]
}

type DataEditorValue = {
  title: string
  summary: string
  rows: DataMetricRow[]
}

const emptyMetricRow = (): DataMetricRow => ({ dimension: '', metric: '', value: '', note: '' })

const emptyDataEditorValue = (): DataEditorValue => ({
  title: '',
  summary: '',
  rows: [emptyMetricRow()],
})

const formatTimestamp = (timestamp?: number | null) => {
  if (!timestamp) return '尚未采集'
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
}

const sourceKindLabel = (kind: DataSource['source_kind']) => (
  kind === 'report_url' ? '实时报表' : '工作记录'
)

const accessModeLabel = (mode: DataSource['access_mode']) => {
  if (mode === 'browser_session') return '浏览器会话'
  if (mode === 'direct_http') return '直接访问'
  return '本地记忆'
}

const freshnessLabel = (source: DataSource) => {
  if (!source.latest_snapshot) return '待采集'
  const ageMs = Date.now() - source.latest_snapshot.collected_at
  if (source.source_kind === 'report_url') {
    const ttlMs = Math.max(1, source.latest_snapshot.freshness_ttl_seconds) * 1000
    return ageMs <= ttlMs ? '当前可用' : '建议刷新'
  }
  if (ageMs <= 24 * 60 * 60 * 1000) return '近期数据'
  if (ageMs <= 7 * 24 * 60 * 60 * 1000) return '可能过期'
  return '历史数据'
}

const normalizeText = (value: unknown) => {
  if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

const fallbackDataTitle = (rows: DataMetricRow[]) => {
  const metrics = [...new Set(rows.map(row => row.metric).filter(Boolean))]
  if (metrics.length === 0) return '数据指标概况'
  if (metrics.length === 1) {
    const comparisonCount = new Set(rows.map(row => row.dimension).filter(Boolean)).size
    return `${metrics[0]}${comparisonCount >= 2 ? '对比' : '数据概况'}`
  }
  return `${metrics.slice(0, 2).join('与')}指标`
}

const presentSnapshot = (snapshot: DataSnapshot): DataPresentation => {
  const structured = snapshot.structured_data ?? {}
  const rows = Array.isArray(structured.metric_rows)
    ? structured.metric_rows.flatMap((value): DataMetricRow[] => {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return []
        const row = value as Record<string, unknown>
        const metric = normalizeText(row.metric)
        const metricValue = normalizeText(row.value)
        if (!metric || !metricValue) return []
        return [{
          dimension: normalizeText(row.dimension),
          metric,
          value: metricValue,
          note: normalizeText(row.note),
        }]
      })
    : []
  return {
    title: normalizeText(structured.title) || fallbackDataTitle(rows),
    summary: normalizeText(structured.summary) || '这条数据尚未形成可理解的摘要',
    rows,
  }
}

const sourcePresentation = (source?: DataSource | null) => (
  source?.latest_snapshot ? presentSnapshot(source.latest_snapshot) : null
)

const DataRowsEditor: React.FC<{
  rows: DataMetricRow[]
  onChange: (index: number, field: keyof DataMetricRow, value: string) => void
  onAdd: () => void
  onRemove: (index: number) => void
}> = ({ rows, onChange, onAdd, onRemove }) => (
  <div className="bake-form-field">
    <span className="bake-kv__title">数据表</span>
    <div className="bake-data-rows-editor">
      <div className="bake-data-rows-editor__head" aria-hidden="true">
        <span>对象 / 范围</span><span>指标 *</span><span>数值 *</span><span>说明</span><span />
      </div>
      {rows.map((row, index) => (
        <div className="bake-data-rows-editor__row" key={index}>
          <input className="bake-input" aria-label={`第 ${index + 1} 行对象或范围`} value={row.dimension} onChange={(event) => onChange(index, 'dimension', event.target.value)} placeholder="整体" />
          <input className="bake-input" aria-label={`第 ${index + 1} 行指标`} value={row.metric} onChange={(event) => onChange(index, 'metric', event.target.value)} placeholder="指标名称" />
          <input className="bake-input" aria-label={`第 ${index + 1} 行数值`} value={row.value} onChange={(event) => onChange(index, 'value', event.target.value)} placeholder="数值" />
          <input className="bake-input" aria-label={`第 ${index + 1} 行说明`} value={row.note} onChange={(event) => onChange(index, 'note', event.target.value)} placeholder="可选" />
          <BakeButton compact disabled={rows.length <= 1} onClick={() => onRemove(index)}>删除</BakeButton>
        </div>
      ))}
    </div>
    <BakeButton compact onClick={onAdd}>+ 添加一行</BakeButton>
  </div>
)

const BakeDataTab: React.FC<{
  items: DataSource[]
  total: number
  limit: number
  offset: number
  query?: string
  draftQuery: string
  sourceKind?: '' | DataSource['source_kind']
  draftSourceKind?: '' | DataSource['source_kind']
  from?: string
  to?: string
  draftFrom?: string
  draftTo?: string
  selectedId: number | null
  loading: boolean
  refreshingId: number | null
  deletingId?: number | null
  onDraftQueryChange: (query: string) => void
  onDraftSourceKindChange?: (value: '' | DataSource['source_kind']) => void
  onDraftFromChange?: (value: string) => void
  onDraftToChange?: (value: string) => void
  onSearch: () => void
  onClearSearch: () => void
  favoriteFilter?: MemoryFavoriteFilter
  onFavoriteFilterChange?: (value: MemoryFavoriteFilter) => void
  onToggleFavorite?: (item: DataSource, isFavorite: boolean) => boolean | Promise<boolean>
  onOpenGraph?: (source: DataSource) => void
  onSelect: (id: number) => void
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onRefresh: (id: number) => void
  onDelete?: (id: number) => void | boolean | Promise<boolean>
  onViewTimeline?: (timelineId: number) => void
  onCreate?: (value: DataEditorValue) => boolean | Promise<boolean>
  onUpdate?: (id: number, value: DataEditorValue) => boolean | Promise<boolean>
  focusId?: number | null
}> = ({
  items,
  total,
  limit,
  offset,
  query = '',
  draftQuery,
  sourceKind = '',
  draftSourceKind = '',
  from = '',
  to = '',
  draftFrom = '',
  draftTo = '',
  selectedId,
  loading,
  refreshingId,
  deletingId,
  onDraftQueryChange,
  onDraftSourceKindChange,
  onDraftFromChange,
  onDraftToChange,
  onSearch,
  onClearSearch,
  favoriteFilter = 'all',
  onFavoriteFilterChange,
  onToggleFavorite,
  onOpenGraph,
  onSelect,
  onPageChange,
  onLimitChange,
  onRefresh,
  onDelete,
  onViewTimeline,
  onCreate,
  onUpdate,
  focusId,
}) => {
  const selected = items.find(item => item.id === selectedId) ?? items[0]
  const selectedPresentation = sourcePresentation(selected)
  const selectedTimelineIds = selected?.latest_snapshot?.source_timeline_ids ?? []
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [drawerMode, setDrawerMode] = useState<'detail' | 'edit' | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [favoriteBusy, setFavoriteBusy] = useState(false)
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null)
  const [newData, setNewData] = useState<DataEditorValue>(emptyDataEditorValue)
  const [draftData, setDraftData] = useState<DataEditorValue>(emptyDataEditorValue)
  const hasActiveFilters = Boolean(query.trim() || sourceKind || from || to || focusId)

  useEffect(() => {
    const presentation = sourcePresentation(selected)
    setDraftData(presentation ? {
      title: presentation.title,
      summary: presentation.summary,
      rows: presentation.rows.length ? presentation.rows.map(row => ({ ...row })) : [emptyMetricRow()],
    } : emptyDataEditorValue())
  }, [selected])

  useEffect(() => {
    if (focusId && selected?.id === focusId) setDrawerMode('detail')
  }, [focusId, selected?.id])

  const openDrawer = (item: DataSource, mode: 'detail' | 'edit', trigger: HTMLButtonElement) => {
    detailTriggerRef.current = trigger
    onSelect(item.id)
    setDrawerMode(mode)
  }

  const closeDrawer = () => {
    const trigger = detailTriggerRef.current
    setDrawerMode(null)
    window.setTimeout(() => trigger?.focus(), 0)
  }

  const setRow = (target: 'new' | 'draft', index: number, field: keyof DataMetricRow, value: string) => {
    const setter = target === 'new' ? setNewData : setDraftData
    setter(previous => ({
      ...previous,
      rows: previous.rows.map((row, rowIndex) => rowIndex === index ? { ...row, [field]: value } : row),
    }))
  }

  const addRow = (target: 'new' | 'draft') => {
    const setter = target === 'new' ? setNewData : setDraftData
    setter(previous => ({ ...previous, rows: [...previous.rows, emptyMetricRow()] }))
  }

  const removeRow = (target: 'new' | 'draft', index: number) => {
    const setter = target === 'new' ? setNewData : setDraftData
    setter(previous => ({
      ...previous,
      rows: previous.rows.length > 1 ? previous.rows.filter((_, rowIndex) => rowIndex !== index) : previous.rows,
    }))
  }

  const canSave = (value: DataEditorValue) => Boolean(
    value.title.trim() && value.rows.some(row => row.metric.trim() && row.value.trim())
  )

  const handleCreate = async () => {
    if (!onCreate || !canSave(newData)) return
    setIsSaving(true)
    const created = await onCreate(newData)
    setIsSaving(false)
    if (created === false) return
    setShowCreateDialog(false)
    setNewData(emptyDataEditorValue())
  }

  const handleUpdate = async () => {
    if (!selected || !onUpdate || !canSave(draftData)) return
    setIsSaving(true)
    const updated = await onUpdate(selected.id, draftData)
    setIsSaving(false)
    if (updated !== false) setDrawerMode('detail')
  }

  const cancelEditing = () => {
    setDraftData({
      title: selectedPresentation?.title || '',
      summary: selectedPresentation?.summary || '',
      rows: selectedPresentation?.rows.length
        ? selectedPresentation.rows.map(row => ({ ...row }))
        : [emptyMetricRow()],
    })
    setDrawerMode('detail')
  }

  const handleToggleFavorite = async () => {
    if (!selected || !onToggleFavorite || favoriteBusy) return
    const nextFavorite = !Boolean(selected.is_favorite)
    setFavoriteBusy(true)
    const updated = await onToggleFavorite(selected, nextFavorite)
    setFavoriteBusy(false)
    if (updated !== false && favoriteFilter !== 'all') closeDrawer()
  }

  const columns: BakeRecordColumn<DataSource>[] = [
    {
      key: 'created',
      label: '创建时间',
      className: 'bake-record-table__time',
      render: item => (
        <>
          <div>{formatTimestamp(item.created_at ?? item.first_seen_at)}</div>
          <div className="bake-record-table__secondary">ID #{item.id}</div>
        </>
      ),
    },
    {
      key: 'title',
      label: '数据名称',
      className: 'bake-record-table__title',
      render: item => <div className="bake-record-table__primary bake-line-clamp-2">{sourcePresentation(item)?.title || item.title || '未命名数据'}</div>,
    },
    {
      key: 'summary',
      label: '数据摘要',
      render: item => <div className="bake-record-table__preview bake-line-clamp-2">{sourcePresentation(item)?.summary || '暂无数据摘要'}</div>,
    },
    {
      key: 'source',
      label: '来源',
      render: item => (
        <>
          <div className="bake-record-table__preview bake-line-clamp-1">{item.title || '未知来源'}</div>
          <div className="bake-record-table__secondary">{sourceKindLabel(item.source_kind)}</div>
        </>
      ),
    },
  ]

  return (
    <div style={{ display: 'grid', gap: 16 }}>
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
                placeholder="搜索数据内容、指标、数值或来源 URL"
              />
            </label>
          </div>
          <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--asset-filters">
            <label className="bake-form-field bake-filter-field bake-filter-field--type bake-filter-field--select">
              <span className="bake-filter-label">数据类型</span>
              <select className="bake-input" value={draftSourceKind} onChange={(event) => onDraftSourceKindChange?.(event.target.value as '' | DataSource['source_kind'])} aria-label="数据类型">
                <option value="">全部类型</option>
                <option value="work_memory">工作记录</option>
                <option value="report_url">实时报表</option>
              </select>
            </label>
            {onFavoriteFilterChange && <BakeFavoriteFilterControl value={favoriteFilter} onChange={onFavoriteFilterChange} />}
            <label className="bake-form-field bake-filter-field bake-filter-field--date">
              <span className="bake-filter-label">起始时间</span>
              <input className="bake-input" type="date" value={draftFrom} onChange={(event) => onDraftFromChange?.(event.target.value)} />
            </label>
            <label className="bake-form-field bake-filter-field bake-filter-field--date">
              <span className="bake-filter-label">结束时间</span>
              <input className="bake-input" type="date" value={draftTo} onChange={(event) => onDraftToChange?.(event.target.value)} />
            </label>
            <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
              <div className="bake-list-toolbar__repository-primary-actions">
                <BakeButton compact type="button" onClick={onClearSearch}>清空</BakeButton>
                <BakeButton compact primary type="submit">搜索</BakeButton>
                {onCreate && <BakeButton compact primary type="button" onClick={() => setShowCreateDialog(true)}>新建</BakeButton>}
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
        ariaLabel="数据表格"
        emptyTitle={hasActiveFilters ? '没有匹配的数据' : '还没有数据'}
        emptyDescription={hasActiveFilters ? '调整关键词、数据类型或时间范围后再试。' : '新建数据或完成采集后，数据会展示在这里。'}
        loading={loading}
        activeId={drawerMode ? selected?.id : null}
        itemLabel="条数据"
        onPageChange={onPageChange}
        onLimitChange={onLimitChange}
        renderActions={item => (
          <>
            <BakeTableActionButton kind="detail" label={`查看数据：${sourcePresentation(item)?.title || item.title}`} onClick={trigger => openDrawer(item, 'detail', trigger)} />
            {onUpdate && <BakeTableActionButton kind="edit" label={`编辑数据：${sourcePresentation(item)?.title || item.title}`} onClick={trigger => openDrawer(item, 'edit', trigger)} />}
            {onOpenGraph && <BakeTableActionButton kind="graph" label={`在记忆图谱中查看数据：${sourcePresentation(item)?.title || item.title}`} onClick={() => onOpenGraph(item)} />}
          </>
        )}
      />

      <BakeDetailDrawer
        open={Boolean(drawerMode && selected && selectedPresentation && selected.latest_snapshot)}
        wide
        eyebrow={drawerMode === 'edit' ? '编辑数据' : '数据详情'}
        title={selectedPresentation?.title || selected?.title || '未命名数据'}
        meta={selected ? <>数据 #{selected.id} · {sourceKindLabel(selected.source_kind)} · {freshnessLabel(selected)}</> : undefined}
        closeLabel="关闭数据详情"
        onClose={closeDrawer}
        footer={selected && selectedPresentation && selected.latest_snapshot && (drawerMode === 'edit' ? (
          <>
            <BakeButton disabled={isSaving} onClick={cancelEditing}>取消</BakeButton>
            <BakeButton primary disabled={isSaving || !canSave(draftData)} onClick={handleUpdate}>{isSaving ? '保存中…' : '保存'}</BakeButton>
          </>
        ) : (
          <>
            {onToggleFavorite && <BakeFavoriteButton isFavorite={Boolean(selected.is_favorite)} busy={favoriteBusy} onToggle={handleToggleFavorite} />}
            {onOpenGraph && <BakeButton onClick={() => { closeDrawer(); onOpenGraph(selected) }}>记忆图谱</BakeButton>}
            {selectedTimelineIds[0] && <BakeButton onClick={() => onViewTimeline?.(selectedTimelineIds[0])}>关联时间线</BakeButton>}
            {selected.source_kind === 'report_url' && (
              <BakeButton disabled={refreshingId === selected.id} onClick={() => onRefresh(selected.id)}>
                {refreshingId === selected.id ? '刷新中…' : '即时刷新数据'}
              </BakeButton>
            )}
            {selected.source_url && <BakeButton onClick={() => window.open(selected.source_url!, '_blank', 'noopener,noreferrer')}>打开原始来源</BakeButton>}
            {onDelete && (
              <BakeButton danger disabled={deletingId === selected.id} onClick={() => {
                void Promise.resolve(onDelete(selected.id)).then(deleted => {
                  if (deleted !== false) closeDrawer()
                })
              }}>
                {deletingId === selected.id ? '删除中…' : '删除数据'}
              </BakeButton>
            )}
            {onUpdate && <BakeButton primary onClick={() => setDrawerMode('edit')}>编辑</BakeButton>}
          </>
        ))}
      >
        {selected && selectedPresentation && selected.latest_snapshot && (drawerMode === 'edit' ? (
          <div className="bake-document-editor">
            <label className="bake-form-field">
              <span className="bake-kv__title">数据名称</span>
              <input
                className="bake-title-input"
                aria-label="数据名称"
                value={draftData.title}
                onChange={(event) => setDraftData(previous => ({ ...previous, title: event.target.value }))}
                placeholder="数据名称"
              />
            </label>
            <label className="bake-form-field">
              <span className="bake-kv__title">数据说明</span>
              <textarea
                className="bake-textarea"
                rows={3}
                value={draftData.summary}
                onChange={(event) => setDraftData(previous => ({ ...previous, summary: event.target.value }))}
                placeholder="说明这组数据的含义"
              />
            </label>
            <DataRowsEditor
              rows={draftData.rows}
              onChange={(index, field, value) => setRow('draft', index, field, value)}
              onAdd={() => addRow('draft')}
              onRemove={(index) => removeRow('draft', index)}
            />
          </div>
        ) : (
          <div className="bake-kv bake-capture-detail bake-knowledge-detail">
            <div className="bake-data-detail-heading">
              <div className="bake-data-detail-heading__summary">{selectedPresentation.summary}</div>
              <div className="bake-inline-pills" style={{ justifyContent: 'flex-start', marginTop: 8 }}>
                <BakePill text={freshnessLabel(selected)} />
                <BakePill text={`数据时间 ${formatTimestamp(selected.latest_snapshot.observed_at ?? selected.latest_snapshot.collected_at)}`} />
              </div>
            </div>
            <section className="bake-knowledge-detail__section bake-data-detail-section">
              <div className="bake-kv__title">数据表</div>
              {selectedPresentation.rows.length > 0 ? (
                <>
                  <div className="bake-data-table-wrap">
                    <table className="bake-data-table">
                      <thead><tr><th scope="col">对象 / 范围</th><th scope="col">指标</th><th scope="col">数值</th><th scope="col">说明</th></tr></thead>
                      <tbody>
                        {selectedPresentation.rows.map((row, index) => (
                          <tr key={`${row.dimension}-${row.metric}-${row.value}-${index}`}>
                            <td>{row.dimension || '整体'}</td><th scope="row">{row.metric}</th><td className="bake-data-table__value">{row.value}</td><td>{row.note || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="bake-muted bake-data-table-note">该数据表共 {selectedPresentation.rows.length} 行指标。</div>
                </>
              ) : <div className="bake-muted">这条数据需要重新提炼，当前不参与有效数据召回。</div>}
            </section>
            <section className="bake-knowledge-detail__section bake-data-detail-section">
              <div className="bake-kv__title">来源与关联</div>
              <dl className="bake-data-source-meta">
                <div><dt>数据 ID</dt><dd>#{selected.id}</dd></div>
                <div><dt>快照 ID</dt><dd>#{selected.latest_snapshot.id}</dd></div>
                <div><dt>来源</dt><dd>{selected.title}</dd></div>
                <div><dt>类型</dt><dd>{sourceKindLabel(selected.source_kind)}</dd></div>
                <div><dt>创建时间</dt><dd>{formatTimestamp(selected.created_at ?? selected.first_seen_at)}</dd></div>
                <div><dt>采集方式</dt><dd>{accessModeLabel(selected.access_mode)}</dd></div>
                <div><dt>采集时间</dt><dd>{formatTimestamp(selected.latest_snapshot.collected_at)}</dd></div>
              </dl>
              {selected.source_url && (
                <div className="bake-data-source-url">
                  <div className="bake-kv__title">来源网址</div>
                  <a href={selected.source_url} target="_blank" rel="noopener noreferrer" className="bake-source-url-link">{selected.source_url}</a>
                </div>
              )}
              <div className="bake-data-timeline-links">
                <span className="bake-muted">关联时间线</span>
                {selectedTimelineIds.length > 0 ? selectedTimelineIds.map(timelineId => (
                  <button key={timelineId} type="button" className="bake-stat-chip bake-stat-chip--button" onClick={() => onViewTimeline?.(timelineId)}>时间线 #{timelineId}</button>
                )) : <span className="bake-muted">暂无</span>}
              </div>
              {selected.source_kind === 'report_url' && <div className="bake-muted bake-data-source-note">需要当前数据时可即时刷新。登录态页面会在原浏览器安全上下文中读取，Cookie 不会保存到记忆面包。</div>}
              {selected.last_error_code && <div className="bake-data-error">最近刷新失败：{selected.last_error_code}</div>}
            </section>
            <details className="bake-data-disclosure">
              <summary>查看完整采集内容</summary>
              <div className="bake-data-content">{selected.latest_snapshot.content_text || '暂无完整采集内容'}</div>
            </details>
          </div>
        ))}
      </BakeDetailDrawer>
      {showCreateDialog && (
        <div className="bake-modal-overlay" onClick={() => !isSaving && setShowCreateDialog(false)}>
          <div className="bake-modal bake-modal--wide" onClick={(event) => event.stopPropagation()}>
            <div className="bake-modal__header">
              <h3>新建数据</h3>
              <button className="bake-modal__close" aria-label="关闭" onClick={() => setShowCreateDialog(false)}>×</button>
            </div>
            <div className="bake-modal__body">
              <label className="bake-form-field">
                <span className="bake-form-label">数据名称 *</span>
                <input
                  className="bake-input"
                  value={newData.title}
                  onChange={(event) => setNewData(previous => ({ ...previous, title: event.target.value }))}
                  placeholder="例如：本周核心经营指标"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">数据说明</span>
                <textarea
                  className="bake-textarea"
                  rows={3}
                  value={newData.summary}
                  onChange={(event) => setNewData(previous => ({ ...previous, summary: event.target.value }))}
                  placeholder="说明这组数据的时间范围和含义"
                />
              </label>
              <DataRowsEditor
                rows={newData.rows}
                onChange={(index, field, value) => setRow('new', index, field, value)}
                onAdd={() => addRow('new')}
                onRemove={(index) => removeRow('new', index)}
              />
            </div>
            <div className="bake-modal__footer">
              <BakeButton disabled={isSaving} onClick={() => setShowCreateDialog(false)}>取消</BakeButton>
              <BakeButton primary disabled={isSaving || !canSave(newData)} onClick={handleCreate}>{isSaving ? '保存中…' : '保存'}</BakeButton>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default BakeDataTab
