import React, { useState } from 'react'
import type { DataSnapshot, DataSource } from '../../types'
import { BakeButton, BakeCard, BakePill, BakeSectionHeader } from './BakeShared'

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

const BakeDataTab: React.FC<{
  items: DataSource[]
  total: number
  limit: number
  offset: number
  draftQuery: string
  selectedId: number | null
  loading: boolean
  refreshingId: number | null
  deletingId?: number | null
  onDraftQueryChange: (query: string) => void
  onSearch: () => void
  onClearSearch: () => void
  onSelect: (id: number) => void
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onRefresh: (id: number) => void
  onDelete?: (id: number) => void
  onViewTimeline?: (timelineId: number) => void
}> = ({
  items,
  total,
  limit,
  offset,
  draftQuery,
  selectedId,
  loading,
  refreshingId,
  deletingId,
  onDraftQueryChange,
  onSearch,
  onClearSearch,
  onSelect,
  onPageChange,
  onLimitChange,
  onRefresh,
  onDelete,
  onViewTimeline,
}) => {
  const selected = items.find(item => item.id === selectedId) ?? items[0]
  const selectedPresentation = sourcePresentation(selected)
  const selectedTimelineIds = selected?.latest_snapshot?.source_timeline_ids ?? []
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))
  const [pageInput, setPageInput] = useState('')

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <BakeCard>
        <BakeSectionHeader title="数据" />
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
                  placeholder="搜索数据含义、指标、数值或来源"
                />
              </label>
              <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--search">
                <BakeButton compact primary type="submit">搜索</BakeButton>
                {draftQuery && <BakeButton compact type="button" onClick={onClearSearch}>清除</BakeButton>}
              </div>
            </div>
          </div>
        </form>
      </BakeCard>

      <div className="bake-split-list-detail bake-split-list-detail--knowledge">
        <BakeCard className="bake-knowledge-list-card bake-data-list-card">
          {loading ? (
            <div className="bake-muted">正在加载数据记录…</div>
          ) : items.length === 0 ? (
            <div className="bake-muted">尚未提取到含义明确的数据。</div>
          ) : (
            <section className="bake-data-list-group" aria-labelledby="bake-data-records-title">
              <div className="bake-data-list-group__heading">
                <span id="bake-data-records-title">数据记录</span>
                <span>{items.length}</span>
              </div>
              <div className="bake-list bake-knowledge-list">
                {items.map((item) => {
                  const presentation = sourcePresentation(item)!
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => onSelect(item.id)}
                      className={`bake-list-item bake-knowledge-list-item bake-data-record-item ${item.id === selected?.id ? 'bake-list-item--active' : ''}`.trim()}
                    >
                      <div className="bake-data-record-item__eyebrow">数据 #{item.id}</div>
                      <div className="bake-data-record-item__title bake-line-clamp-2">{presentation.title}</div>
                      <div className="bake-data-record-item__summary bake-line-clamp-3">{presentation.summary}</div>
                      <div className="bake-data-record-item__source bake-line-clamp-1">来源：{item.title}</div>
                      {item.source_url && (
                        <div className="bake-data-record-item__source bake-line-clamp-1">网址：{item.source_url}</div>
                      )}
                      <div className="bake-memory-list-item__meta">
                        <span>{freshnessLabel(item)}</span>
                        <span>{formatTimestamp(item.latest_snapshot?.collected_at)}</span>
                      </div>
                    </button>
                  )
                })}
              </div>
            </section>
          )}

          <div className="bake-pagination bake-pagination--extended">
            <div className="bake-pagination__controls">
              <BakeButton compact disabled={offset === 0} onClick={() => onPageChange(Math.max(0, offset - limit))}>上一页</BakeButton>
              <BakeButton compact disabled={offset + limit >= total} onClick={() => onPageChange(offset + limit)}>下一页</BakeButton>
            </div>
            <div className="bake-pagination__summary-group bake-muted">
              <span className="bake-pagination__summary">共 {total} 条数据</span>
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

        <BakeCard className="bake-knowledge-detail-card bake-data-detail-card">
          {selected && selectedPresentation && selected.latest_snapshot ? (
            <div className="bake-kv bake-capture-detail bake-knowledge-detail">
              <div className="bake-data-detail-heading">
                <div className="bake-data-detail-heading__label">数据 #{selected.id}</div>
                <div className="bake-data-detail-heading__title">{selectedPresentation.title}</div>
                <div className="bake-data-detail-heading__summary">{selectedPresentation.summary}</div>
                <div className="bake-inline-pills" style={{ justifyContent: 'flex-start', marginTop: 8 }}>
                  <BakePill text={freshnessLabel(selected)} />
                  <BakePill text={`数据时间 ${formatTimestamp(selected.latest_snapshot.observed_at ?? selected.latest_snapshot.collected_at)}`} />
                </div>
              </div>

              <div className="bake-knowledge-detail__section bake-data-detail-section">
                <div className="bake-kv__title">数据表</div>
                {selectedPresentation.rows.length > 0 ? (
                  <>
                    <div className="bake-data-table-wrap">
                      <table className="bake-data-table">
                        <thead>
                          <tr>
                            <th scope="col">对象 / 范围</th>
                            <th scope="col">指标</th>
                            <th scope="col">数值</th>
                            <th scope="col">说明</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedPresentation.rows.map((row, index) => (
                            <tr key={`${row.dimension}-${row.metric}-${row.value}-${index}`}>
                              <td>{row.dimension || '整体'}</td>
                              <th scope="row">{row.metric}</th>
                              <td className="bake-data-table__value">{row.value}</td>
                              <td>{row.note || '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="bake-muted bake-data-table-note">
                      该数据表共 {selectedPresentation.rows.length} 行指标。
                    </div>
                  </>
                ) : (
                  <div className="bake-muted">这条数据需要重新提炼，当前不参与有效数据召回。</div>
                )}
              </div>

              <div className="bake-knowledge-detail__section bake-data-detail-section">
                <div className="bake-kv__title">来源与关联</div>
                <dl className="bake-data-source-meta">
                  <div><dt>数据 ID</dt><dd>#{selected.id}</dd></div>
                  <div><dt>快照 ID</dt><dd>#{selected.latest_snapshot.id}</dd></div>
                  <div><dt>来源</dt><dd>{selected.title}</dd></div>
                  <div><dt>类型</dt><dd>{sourceKindLabel(selected.source_kind)}</dd></div>
                  <div><dt>采集方式</dt><dd>{accessModeLabel(selected.access_mode)}</dd></div>
                  <div><dt>采集时间</dt><dd>{formatTimestamp(selected.latest_snapshot.collected_at)}</dd></div>
                </dl>
                {selected.source_url && (
                  <div className="bake-data-source-url">
                    <div className="bake-kv__title">来源网址</div>
                    <a
                      href={selected.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: '#2563EB', textDecoration: 'underline', wordBreak: 'break-all' }}
                    >
                      {selected.source_url}
                    </a>
                  </div>
                )}
                <div className="bake-data-timeline-links">
                  <span className="bake-muted">关联时间线</span>
                  {selectedTimelineIds.length > 0 ? selectedTimelineIds.map(timelineId => (
                    <button
                      key={timelineId}
                      type="button"
                      className="bake-stat-chip bake-stat-chip--button"
                      onClick={() => onViewTimeline?.(timelineId)}
                    >
                      时间线 #{timelineId}
                    </button>
                  )) : <span className="bake-muted">暂无</span>}
                </div>
                {selected.source_kind === 'report_url' && (
                  <div className="bake-muted bake-data-source-note">
                    需要当前数据时可即时刷新。登录态页面会在原浏览器安全上下文中读取，Cookie 不会保存到记忆面包。
                  </div>
                )}
                {selected.last_error_code && <div className="bake-data-error">最近刷新失败：{selected.last_error_code}</div>}
              </div>

              <details className="bake-data-disclosure">
                <summary>查看完整采集内容</summary>
                <div className="bake-data-content">{selected.latest_snapshot.content_text || '暂无完整采集内容'}</div>
              </details>

              <div className="bake-actions--primary">
                {selectedTimelineIds[0] && (
                  <BakeButton onClick={() => onViewTimeline?.(selectedTimelineIds[0])}>关联时间线</BakeButton>
                )}
                {selected.source_kind === 'report_url' && (
                  <BakeButton primary disabled={refreshingId === selected.id} onClick={() => onRefresh(selected.id)}>
                    {refreshingId === selected.id ? '刷新中…' : '即时刷新数据'}
                  </BakeButton>
                )}
                {selected.source_url && (
                  <BakeButton onClick={() => window.open(selected.source_url!, '_blank', 'noopener,noreferrer')}>打开原始来源</BakeButton>
                )}
                {onDelete && (
                  <BakeButton danger disabled={deletingId === selected.id} onClick={() => onDelete(selected.id)}>
                    {deletingId === selected.id ? '删除中…' : '删除数据'}
                  </BakeButton>
                )}
              </div>
            </div>
          ) : <div className="bake-muted">选择一条数据记录查看摘要、数据表和来源。</div>}
        </BakeCard>
      </div>
    </div>
  )
}

export default BakeDataTab
