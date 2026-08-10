import React, { useEffect, useState } from 'react'
import { useAppStore } from '../../store/useAppStore'
import type { BakeCaptureItem } from '../../types'
import { BakeButton, BakeCard, BakePill, BakeSectionHeader } from './BakeShared'

const formatCaptureTime = (ts?: number) => {
  if (!ts) return '—'
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

const parseDateInputToMs = (value: string, endOfDay = false) => {
  if (!value) return undefined
  const iso = endOfDay ? `${value}T23:59:59.999` : `${value}T00:00:00.000`
  const ts = new Date(iso).getTime()
  return Number.isNaN(ts) ? undefined : ts
}

const textOrNull = (value?: string | null) => {
  const trimmed = value?.trim()
  return trimmed ? trimmed : null
}

const CAPTURE_TEXT_PENDING_WINDOW_MS = 10 * 60 * 1000

const captureNeedsTextRefresh = (item: BakeCaptureItem, now = Date.now()) => (
  Boolean(textOrNull(item.screenshotPath))
  && !textOrNull(item.axText)
  && !textOrNull(item.ocrText)
  && now <= item.ts + CAPTURE_TEXT_PENDING_WINDOW_MS
)

const captureTitle = (item: BakeCaptureItem) => {
  const title = textOrNull(item.winTitle) ?? textOrNull(item.webpageTitle)
  if (title) return title

  const appName = textOrNull(item.appName)
  return appName ? `${appName} · ID #${item.id}` : `ID #${item.id}`
}

const capturePreview = (item: BakeCaptureItem) => (
  textOrNull(item.summary) ??
  textOrNull(item.bestText) ??
  textOrNull(item.axText) ??
  textOrNull(item.ocrText) ??
  '暂无正文'
)

const captureTextInformation = (item: BakeCaptureItem) => {
  const texts = [item.axText, item.ocrText]
    .map(textOrNull)
    .filter((text): text is string => Boolean(text))
    .filter((text, index, values) => values.indexOf(text) === index)

  if (texts.length === 0) {
    return captureNeedsTextRefresh(item)
      ? '文本识别中，完成后将自动显示…'
      : '暂无文本信息'
  }
  if (texts.length === 1) return texts[0]

  const moreCompleteText = texts.find(text => texts.every(candidate => text.includes(candidate)))
  return moreCompleteText ?? texts.join('\n\n')
}

const CAPTURE_TRIGGER_META: Record<string, { label: string; description: string }> = {
  app_switch: {
    label: '应用切换',
    description: '检测到前台应用发生变化后触发本次采集。',
  },
  browser_navigation: {
    label: 'URL 变动',
    description: '检测到浏览器前台页面的 URL 发生变化后触发本次采集。',
  },
  mouse_click: {
    label: '鼠标事件',
    description: '检测到鼠标点击后触发；仅记录触发类型，不记录点击内容。',
  },
  scroll: {
    label: '滚动事件',
    description: '检测到页面或内容滚动后触发；仅记录触发类型。',
  },
  key_pause: {
    label: '键盘事件',
    description: '检测到键盘活动并短暂停顿后触发；不记录按键或输入内容。',
  },
  auto: {
    label: '定时器兜底',
    description: '达到采集间隔后由定时器触发，避免遗漏持续进行中的工作。',
  },
  manual: {
    label: '手动触发',
    description: '由用户操作或调试流程主动触发本次采集。',
  },
}

const captureTriggerMeta = (eventType?: string | null) => (
  CAPTURE_TRIGGER_META[eventType ?? ''] ?? {
    label: '其他触发信号',
    description: eventType
      ? `本次采集由事件 ${eventType} 触发。`
      : '当前记录没有可识别的触发信号。',
  }
)

const rawTypeLabel = (label?: string | null) => (
  textOrNull(label)?.replace(/^(原始模态|原始事件)[：:]\s*/, '') ?? '未识别'
)

const BakeCaptureTab: React.FC<{
  captures: BakeCaptureItem[]
  total: number
  limit: number
  offset: number
  query: string
  from: string
  to: string
  draftQuery: string
  draftFrom: string
  draftTo: string
  sourceCaptureId: string | null
  selectedCaptureId: string | null
  selectedCaptureDetail: BakeCaptureItem | null
  onSelectCapture: (id: string | null) => void
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  onViewLinkedTimeline: (timelineId?: string | null) => void
  onDeleteCapture: (id: string) => void
  canGoBack: boolean
  onGoBack: () => void
}> = ({
  captures,
  total,
  limit,
  offset,
  query,
  from,
  to,
  draftQuery,
  draftFrom,
  draftTo,
  sourceCaptureId,
  selectedCaptureId,
  selectedCaptureDetail,
  onSelectCapture,
  onPageChange,
  onLimitChange,
  onDraftQueryChange,
  onDraftFromChange,
  onDraftToChange,
  onSearch,
  onClearFilters,
  onViewLinkedTimeline,
  onDeleteCapture,
  canGoBack,
  onGoBack,
}) => {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)
  const debugModeEnabled = useAppStore((s) => s.debugModeEnabled)
  const [pageInput, setPageInput] = useState('')
  const [isScreenshotOpen, setIsScreenshotOpen] = useState(false)
  const selectedListItem = captures.find(item => item.id === selectedCaptureId) ?? null
  // 详情接口返回前继续使用列表项数据，避免选中态停留在上一条记录上
  const selected = selectedCaptureDetail && selectedCaptureDetail.id === selectedCaptureId
    ? selectedCaptureDetail
    : selectedListItem ?? captures[0] ?? null
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))
  const screenshotUrl = selected?.screenshotPath ? `${apiBaseUrl}/api/bake/captures/${encodeURIComponent(selected.id)}/screenshot` : null
  const triggerMeta = captureTriggerMeta(selected?.eventType)

  useEffect(() => {
    if (!screenshotUrl && isScreenshotOpen) {
      setIsScreenshotOpen(false)
    }
  }, [isScreenshotOpen, screenshotUrl])

  useEffect(() => {
    if (!isScreenshotOpen) return

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsScreenshotOpen(false)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isScreenshotOpen])

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
              <input
                className="bake-input"
                value={draftQuery}
                onChange={(event) => onDraftQueryChange(event.target.value)}
                placeholder="搜索标题、正文或文本信息"
              />
            </label>
            <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--search">
              <BakeButton compact primary type="submit">搜索</BakeButton>
            </div>
          </div>
          <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--dates">
            <label className="bake-form-field bake-filter-field">
              <span className="bake-filter-label">开始日期</span>
              <input
                className="bake-input"
                type="date"
                value={draftFrom}
                onChange={(event) => onDraftFromChange(event.target.value)}
              />
            </label>
            <label className="bake-form-field bake-filter-field">
              <span className="bake-filter-label">结束日期</span>
              <input
                className="bake-input"
                type="date"
                value={draftTo}
                onChange={(event) => onDraftToChange(event.target.value)}
              />
            </label>
            <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
              {(draftQuery || draftFrom || draftTo || query || from || to || sourceCaptureId) && (
                <BakeButton compact onClick={onClearFilters}>清除筛选</BakeButton>
              )}
            </div>
          </div>
        </div>
      </form>

      <div className="bake-split-list-detail bake-split-list-detail--capture">
        <BakeCard className="bake-capture-list-card">
          <BakeSectionHeader
            title="采集记录"
            right={canGoBack ? <BakeButton compact onClick={onGoBack}>返回上一步</BakeButton> : undefined}
          />

        <div className="bake-list bake-capture-list">
          {captures.length === 0 ? (
            <div className="bake-muted">当前筛选条件下没有可浏览的采集记录。</div>
          ) : captures.map(item => {
            const title = captureTitle(item)
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => onSelectCapture(item.id)}
                className={`bake-list-item bake-capture-list-item ${item.id === selected?.id ? 'bake-list-item--active' : ''}`.trim()}
              >
                <div className="bake-list-item__title bake-line-clamp-1">{title}</div>
                <div className="bake-muted bake-line-clamp-2">{capturePreview(item)}</div>
                <div className="bake-memory-list-item__meta">
                  <span>{item.appName || '未知应用'}</span>
                  <span>{formatCaptureTime(item.ts)}</span>
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
            <span className="bake-pagination__summary">共 {total} 条</span>
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
              <BakeButton
                compact
                onClick={() => {
                  const target = Number(pageInput)
                  if (!Number.isFinite(target) || target < 1) return
                  const nextPage = Math.min(totalPages, Math.floor(target))
                  onPageChange((nextPage - 1) * limit)
                  setPageInput('')
                }}
              >
                前往
              </BakeButton>
            </div>
          </div>
        </div>
        </BakeCard>

        <BakeCard className="bake-capture-detail-card">
        {selected ? (
          <div className="bake-kv bake-capture-detail">
            <div className="bake-inline-meta">
              <div>
                <div className="bake-title" style={{ fontSize: 18 }}>{captureTitle(selected)}</div>
                <div className="bake-muted" style={{ marginTop: 4 }}>{selected.appName || '未知应用'} · {formatCaptureTime(selected.ts)}</div>
              </div>
              <BakePill text={`ID #${selected.id}`} />
            </div>

            <div className={`bake-grid-2 bake-capture-detail__meta-grid ${debugModeEnabled ? '' : 'bake-capture-detail__meta-grid--single'}`.trim()}>
              <div className="bake-capture-detail__meta-card">
                <div className="bake-kv__title">窗口 / 页面</div>
                <div className="bake-muted" style={{ lineHeight: 1.7 }}>{selected.winTitle || selected.webpageTitle || '—'}</div>
              </div>
              {debugModeEnabled && (
                <div className="bake-capture-detail__meta-card bake-capture-detail__debug-card">
                  <div className="bake-capture-detail__debug-section">
                    <div className="bake-kv__title">触发信号</div>
                    <div className="bake-capture-detail__type-primary">{triggerMeta.label}</div>
                    <div className="bake-muted">{triggerMeta.description}</div>
                  </div>
                  <div className="bake-capture-detail__debug-section">
                    <div className="bake-kv__title">片段类型</div>
                    <div className="bake-capture-detail__type-stack">
                      <div className="bake-capture-detail__type-primary">{selected.semanticTypeLabel || '未识别类型'}</div>
                      <div className="bake-muted">原始模态：{rawTypeLabel(selected.rawTypeLabel || selected.eventType)}</div>
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div>
              <div className="bake-kv__title">截图预览</div>
              {screenshotUrl ? (
                <div className="bake-capture-detail__screenshot-wrap">
                  <button
                    type="button"
                    className="bake-capture-detail__screenshot-button"
                    onClick={() => setIsScreenshotOpen(true)}
                  >
                    <img
                      className="bake-capture-detail__screenshot-image"
                      src={screenshotUrl}
                      alt={captureTitle(selected)}
                      loading="lazy"
                    />
                    <span className="bake-capture-detail__screenshot-hint">点击查看大图</span>
                  </button>
                </div>
              ) : (
                <div className="bake-muted">当前没有截图文件。</div>
              )}
            </div>

            <div>
              <div className="bake-kv__title">文本信息</div>
              <div className="bake-capture-detail__text">{captureTextInformation(selected)}</div>
            </div>

            <div>
              <div className="bake-kv__title">输入 / 音频</div>
              <div className="bake-capture-detail__text">{selected.inputText || selected.audioText || '暂无输入或音频文本'}</div>
            </div>

            {(selected.url || selected.webpageTitle) && (
              <div>
                <div className="bake-kv__title">网址</div>
                <div className="bake-capture-detail__text">
                  {selected.webpageTitle && (
                    <div style={{ marginBottom: 4 }}>{selected.webpageTitle}</div>
                  )}
                  {selected.url && (
                    <a
                      href={selected.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: '#2563EB', textDecoration: 'underline', wordBreak: 'break-all' }}
                    >
                      {selected.url}
                    </a>
                  )}
                </div>
              </div>
            )}

            <div className="bake-actions">
              {selected.linkedTimelineId && (
                <BakeButton onClick={() => onViewLinkedTimeline(selected.linkedTimelineId)}>
                  查看所属时间线
                </BakeButton>
              )}
              <BakeButton compact danger onClick={() => onDeleteCapture(selected.id)}>删除</BakeButton>
            </div>
          </div>
        ) : (
          <div className="bake-muted">暂无采集记录详情</div>
        )}
        </BakeCard>

        {selected && screenshotUrl && isScreenshotOpen && (
          <div
            className="bake-capture-lightbox"
            role="dialog"
            aria-modal="true"
            aria-label="截图预览大图"
            onClick={() => setIsScreenshotOpen(false)}
          >
            <div
              className="bake-capture-lightbox__dialog"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="bake-capture-lightbox__header">
                <div>
                  <div className="bake-kv__title">截图大图</div>
                  <div className="bake-muted">{captureTitle(selected)}</div>
                </div>
                <BakeButton compact onClick={() => setIsScreenshotOpen(false)}>关闭</BakeButton>
              </div>
              <div className="bake-capture-lightbox__body">
                <img
                  className="bake-capture-lightbox__image"
                  src={screenshotUrl}
                  alt={captureTitle(selected)}
                />
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  )
}

export { captureNeedsTextRefresh, parseDateInputToMs }

export default BakeCaptureTab
