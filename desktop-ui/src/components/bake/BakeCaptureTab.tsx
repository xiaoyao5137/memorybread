import { Eye, Plus, X } from 'lucide-react'
import React, { useEffect, useRef, useState } from 'react'
import { useAppStore } from '../../store/useAppStore'
import { useCreateBakeCapture } from '../../hooks/useApi'
import type { BakeCaptureItem } from '../../types'
import { BakeButton, BakeCard, BakePill } from './BakeShared'

const formatCaptureTime = (ts?: number) => {
  if (!ts) return '时间未知'
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false })
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

const CAPTURE_PREVIEW_PENDING = '正在提炼中'

// OCR/AX 文本里大量硬换行会让摘要每行只剩几个字，折叠成空格后再展示能充分利用列宽
const flattenPreviewText = (text: string) => text.replace(/\s+/g, ' ').trim()

const capturePreview = (item: BakeCaptureItem) => {
  const extractedText = [item.axText, item.ocrText, item.inputText, item.audioText]
    .map(textOrNull)
    .map((text) => (text ? flattenPreviewText(text) : null))
    .find((text): text is string => Boolean(text))
  if (extractedText) return extractedText
  if (captureNeedsTextRefresh(item)) return CAPTURE_PREVIEW_PENDING

  const title = captureTitle(item)
  return [item.summary, item.bestText]
    .map(textOrNull)
    .map((text) => (text ? flattenPreviewText(text) : null))
    .find((text): text is string => Boolean(text && text !== title)) ?? '暂无正文'
}

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
  app: string
  from: string
  to: string
  draftQuery: string
  draftApp: string
  draftFrom: string
  draftTo: string
  sourceCaptureId: string | null
  selectedCaptureId: string | null
  selectedCaptureDetail: BakeCaptureItem | null
  onSelectCapture: (id: string | null) => void
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftAppChange: (app: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  onViewLinkedTimeline: (timelineId?: string | null) => void
  onDeleteCapture: (id: string) => void
  onRefresh: () => void
}> = ({
  captures,
  total,
  limit,
  offset,
  query,
  app,
  from,
  to,
  draftQuery,
  draftApp,
  draftFrom,
  draftTo,
  sourceCaptureId,
  selectedCaptureId,
  selectedCaptureDetail,
  onSelectCapture,
  onPageChange,
  onLimitChange,
  onDraftQueryChange,
  onDraftAppChange,
  onDraftFromChange,
  onDraftToChange,
  onSearch,
  onClearFilters,
  onViewLinkedTimeline,
  onDeleteCapture,
  onRefresh,
}) => {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)
  const debugModeEnabled = useAppStore((s) => s.debugModeEnabled)
  const [pageInput, setPageInput] = useState('')
  const [isDetailDrawerOpen, setIsDetailDrawerOpen] = useState(false)
  const [isScreenshotOpen, setIsScreenshotOpen] = useState(false)

  // 新建采集记录弹窗状态
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [createTitle, setCreateTitle] = useState('')
  const [createAppName, setCreateAppName] = useState('')
  const [createText, setCreateText] = useState('')
  const [createScreenshotPreview, setCreateScreenshotPreview] = useState<string | null>(null)
  const [createScreenshotBase64, setCreateScreenshotBase64] = useState<string | null>(null)
  const [isCreating, setIsCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const createScreenshotInputRef = useRef<HTMLInputElement>(null)
  const createCaptureHook = useCreateBakeCapture()

  const canSubmitCreate = !isCreating

  const resetCreateForm = () => {
    setCreateTitle('')
    setCreateAppName('')
    setCreateText('')
    setCreateScreenshotPreview(null)
    setCreateScreenshotBase64(null)
    setCreateError(null)
    setIsCreating(false)
  }

  const applyScreenshotResult = (result: string) => {
    // result 形如 "data:image/jpeg;base64,..."，提取纯 base64 部分
    const commaIndex = result.indexOf(',')
    const base64Data = commaIndex >= 0 ? result.slice(commaIndex + 1) : result
    setCreateScreenshotPreview(result)
    setCreateScreenshotBase64(base64Data)
  }

  const handleScreenshotSelect = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    if (!file.type.startsWith('image/')) {
      setCreateError('请选择图片文件')
      return
    }
    if (file.size > 10 * 1024 * 1024) {
      setCreateError('截图大小不能超过 10MB')
      return
    }
    setCreateError(null)
    const reader = new FileReader()
    reader.onload = () => {
      applyScreenshotResult(reader.result as string)
    }
    reader.onerror = () => setCreateError('读取图片失败')
    reader.readAsDataURL(file)
  }

  const handlePasteScreenshot = (event: React.ClipboardEvent) => {
    // 仅拦截剪贴板中的图片内容；粘贴纯文本（如文本信息输入框）不受影响
    const items = Array.from(event.clipboardData?.items ?? [])
    const imageItem = items.find(item => item.type.startsWith('image/'))
    if (!imageItem) return
    event.preventDefault()
    const file = imageItem.getAsFile()
    if (!file) return
    if (file.size > 10 * 1024 * 1024) {
      setCreateError('截图大小不能超过 10MB')
      return
    }
    setCreateError(null)
    const reader = new FileReader()
    reader.onload = () => {
      applyScreenshotResult(reader.result as string)
    }
    reader.onerror = () => setCreateError('读取剪贴板图片失败')
    reader.readAsDataURL(file)
  }

  const handleRemoveScreenshot = () => {
    setCreateScreenshotPreview(null)
    setCreateScreenshotBase64(null)
    if (createScreenshotInputRef.current) {
      createScreenshotInputRef.current.value = ''
    }
  }

  const handleCreateCapture = async () => {
    if (!canSubmitCreate) return
    setIsCreating(true)
    setCreateError(null)
    try {
      await createCaptureHook({
        title: createTitle.trim() || '手工录入',
        appName: createAppName.trim() || '手工录入',
        text: createText.trim() || undefined,
        screenshotBase64: createScreenshotBase64 || undefined,
      })
      resetCreateForm()
      setShowCreateDialog(false)
      onRefresh()
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : '新建失败，请重试')
    } finally {
      setIsCreating(false)
    }
  }

  const handleCloseCreateDialog = () => {
    if (isCreating) return
    resetCreateForm()
    setShowCreateDialog(false)
  }

  const closeDrawerButtonRef = useRef<HTMLButtonElement>(null)
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null)
  const selectedListItem = captures.find(item => item.id === selectedCaptureId) ?? null
  // 详情接口返回前继续使用列表项数据，避免选中态停留在上一条记录上
  const selected = selectedCaptureDetail && selectedCaptureDetail.id === selectedCaptureId
    ? selectedCaptureDetail
    : selectedListItem
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))
  const screenshotUrl = selected?.screenshotPath ? `${apiBaseUrl}/api/bake/captures/${encodeURIComponent(selected.id)}/screenshot` : null
  const triggerMeta = captureTriggerMeta(selected?.eventType)
  const isDetailLoading = Boolean(
    isDetailDrawerOpen
    && selectedCaptureId
    && selectedCaptureDetail?.id !== selectedCaptureId,
  )
  const appSuggestions = Array.from(new Set([
    app,
    draftApp,
    ...captures.map(item => item.appName ?? ''),
  ].map(value => value.trim()).filter(Boolean))).sort((left, right) => left.localeCompare(right, 'zh-CN'))

  const closeDetailDrawer = () => {
    const trigger = detailTriggerRef.current
    setIsScreenshotOpen(false)
    setIsDetailDrawerOpen(false)
    onSelectCapture(null)
    window.setTimeout(() => trigger?.focus(), 0)
  }

  const openDetailDrawer = (item: BakeCaptureItem, trigger: HTMLButtonElement) => {
    detailTriggerRef.current = trigger
    onSelectCapture(item.id)
    setIsDetailDrawerOpen(true)
  }

  useEffect(() => {
    if (!screenshotUrl && isScreenshotOpen) {
      setIsScreenshotOpen(false)
    }
  }, [isScreenshotOpen, screenshotUrl])

  // 关联跳转进入采集页时（sourceCaptureId 由跳转方设置），自动展开目标记录的详情抽屉，
  // 与其他 tab 的 focus 自动打开行为保持一致；用户手动关闭后选中态被清空，不会被重开
  useEffect(() => {
    if (sourceCaptureId && selectedCaptureId && selected?.id === selectedCaptureId) {
      setIsDetailDrawerOpen(true)
    }
  }, [sourceCaptureId, selectedCaptureId, selected?.id])

  useEffect(() => {
    if (!isDetailDrawerOpen || !selected) return
    closeDrawerButtonRef.current?.focus()
  }, [isDetailDrawerOpen, selected])

  useEffect(() => {
    if (!isDetailDrawerOpen && !isScreenshotOpen) return

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        if (isScreenshotOpen) {
          setIsScreenshotOpen(false)
          return
        }
        closeDetailDrawer()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isDetailDrawerOpen, isScreenshotOpen, onSelectCapture])

  useEffect(() => {
    if (!selectedCaptureId && isDetailDrawerOpen) {
      setIsDetailDrawerOpen(false)
      setIsScreenshotOpen(false)
    }
  }, [isDetailDrawerOpen, selectedCaptureId])

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
                placeholder="搜索采集 ID、标题、正文或文本信息"
              />
            </label>
          </div>
          <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--capture-filters">
            <label className="bake-form-field bake-filter-field bake-filter-field--app">
              <span className="bake-filter-label">应用</span>
              <input
                className="bake-input"
                value={draftApp}
                list="bake-capture-app-options"
                autoComplete="off"
                onChange={(event) => onDraftAppChange(event.target.value)}
                placeholder="输入应用名称"
              />
              <datalist id="bake-capture-app-options">
                {appSuggestions.map(option => <option key={option} value={option} />)}
              </datalist>
            </label>
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
              <div className="bake-list-toolbar__repository-primary-actions">
                <BakeButton compact type="button" onClick={onClearFilters}>清空</BakeButton>
                <BakeButton compact primary type="submit">搜索</BakeButton>
                <BakeButton compact primary type="button" onClick={() => setShowCreateDialog(true)}>
                  <Plus size={14} strokeWidth={2.2} style={{ marginRight: 4, verticalAlign: -2 }} />
                  新建
                </BakeButton>
              </div>
            </div>
          </div>
        </div>
      </form>

      <BakeCard className="bake-capture-table-card">
        <div
          className="bake-capture-table-region"
          role="region"
          aria-label="采集记录表格"
          tabIndex={0}
        >
          {captures.length === 0 ? (
            <div className="bake-capture-table-empty">
              <div className="bake-capture-table-empty__title">暂无采集记录</div>
              <div className="bake-muted">当前筛选条件下没有可浏览的内容，请调整关键词或日期范围。</div>
            </div>
          ) : (
            <table className="bake-capture-table">
              <caption className="bake-visually-hidden">当前筛选条件下的采集记录</caption>
              <thead>
                <tr>
                  <th className="bake-capture-table__time" scope="col">采集时间</th>
                  <th className="bake-capture-table__app" scope="col">应用</th>
                  <th className="bake-capture-table__title" scope="col">窗口 / 页面</th>
                  <th className="bake-capture-table__content" scope="col">内容摘要</th>
                  <th className="bake-capture-table__actions" scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {captures.map(item => {
                  const preview = capturePreview(item)
                  return (
                    <tr
                      key={item.id}
                      className={item.id === selectedCaptureId && isDetailDrawerOpen ? 'bake-capture-table__row--active' : undefined}
                    >
                      <td className="bake-capture-table__time">
                        <time>{formatCaptureTime(item.ts)}</time>
                        <span className="bake-capture-table__id">ID #{item.id}</span>
                      </td>
                      <td className="bake-capture-table__app">
                        <span className="bake-capture-table__app-label">{item.appName || '未知应用'}</span>
                      </td>
                      <td className="bake-capture-table__title">
                        <div className="bake-capture-table__primary bake-line-clamp-2">{captureTitle(item)}</div>
                        {item.webpageTitle && item.webpageTitle !== item.winTitle && (
                          <div className="bake-capture-table__secondary bake-line-clamp-1">{item.webpageTitle}</div>
                        )}
                      </td>
                      <td className="bake-capture-table__content">
                        <div className={`bake-capture-table__preview bake-line-clamp-2${preview === CAPTURE_PREVIEW_PENDING ? ' bake-capture-table__preview--pending' : ''}`}>{preview}</div>
                      </td>
                      <td className="bake-capture-table__actions">
                        <div className="bake-capture-table__action-group">
                          <button
                            type="button"
                            className="bake-capture-table__action-button"
                            aria-label={`查看采集记录 #${item.id} 详情`}
                            title="查看详情"
                            onClick={(event) => openDetailDrawer(item, event.currentTarget)}
                          >
                            <Eye size={15} strokeWidth={1.9} aria-hidden="true" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        <div className="bake-pagination bake-pagination--extended">
          <div className="bake-pagination__controls">
            <BakeButton compact disabled={offset <= 0} onClick={() => onPageChange(Math.max(0, offset - limit))}>上一页</BakeButton>
            <BakeButton compact disabled={offset + limit >= total} onClick={() => onPageChange(offset + limit)}>下一页</BakeButton>
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

      {isDetailDrawerOpen && selected && (
        <>
          <div className="bake-capture-drawer-overlay" aria-hidden="true" onClick={closeDetailDrawer} />
          <section
            className="bake-capture-drawer"
            role="dialog"
            aria-modal="true"
            aria-labelledby="bake-capture-drawer-title"
          >
            <header className="bake-capture-drawer__header">
              <div className="bake-capture-drawer__heading">
                <div className="bake-capture-drawer__eyebrow">采集记录详情</div>
                <div id="bake-capture-drawer-title" className="bake-capture-drawer__title">{captureTitle(selected)}</div>
                <div className="bake-capture-drawer__meta">
                  <span>{selected.appName || '未知应用'}</span>
                  <span>{formatCaptureTime(selected.ts)}</span>
                  <BakePill text={`ID #${selected.id}`} />
                </div>
              </div>
              <button
                ref={closeDrawerButtonRef}
                type="button"
                className="bake-capture-drawer__close"
                aria-label="关闭采集记录详情"
                onClick={closeDetailDrawer}
              >
                <X size={18} aria-hidden="true" />
              </button>
            </header>

            <div className="bake-capture-drawer__body">
              {isDetailLoading && (
                <div className="bake-capture-drawer__loading" role="status">正在加载完整详情…</div>
              )}
              <div className="bake-kv bake-capture-detail">
                <div className={`bake-grid-2 bake-capture-detail__meta-grid ${debugModeEnabled ? '' : 'bake-capture-detail__meta-grid--single'}`.trim()}>
                  <div className="bake-capture-detail__meta-card">
                    <div className="bake-kv__title">窗口 / 页面</div>
                    <div className="bake-muted" style={{ lineHeight: 1.7 }}>{selected.winTitle || selected.webpageTitle || '暂无'}</div>
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
                          className="bake-source-url-link"
                        >
                          {selected.url}
                        </a>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>

            <footer className="bake-capture-drawer__footer">
              {selected.linkedTimelineId ? (
                <BakeButton onClick={() => onViewLinkedTimeline(selected.linkedTimelineId)}>
                  查看所属时间线
                </BakeButton>
              ) : <span className="bake-muted">该记录尚未归入时间线</span>}
              <BakeButton compact danger onClick={() => onDeleteCapture(selected.id)}>删除</BakeButton>
            </footer>
          </section>
        </>
      )}

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

      {showCreateDialog && (
        <div className="bake-modal-overlay" onClick={handleCloseCreateDialog}>
          <div className="bake-modal bake-modal--wide" onClick={(event) => event.stopPropagation()}>
            <div className="bake-modal__header">
              <h3>新建采集记录</h3>
              <button className="bake-modal__close" aria-label="关闭" onClick={handleCloseCreateDialog}>×</button>
            </div>
            <div className="bake-modal__body" onPaste={handlePasteScreenshot}>
              <label className="bake-form-field">
                <span className="bake-form-label">窗口 / 页面标题（可选）</span>
                <input
                  className="bake-input"
                  value={createTitle}
                  autoFocus
                  onChange={(event) => setCreateTitle(event.target.value)}
                  placeholder="未填写时默认为「手工录入」"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">应用名称（可选）</span>
                <input
                  className="bake-input"
                  value={createAppName}
                  onChange={(event) => setCreateAppName(event.target.value)}
                  placeholder="例如：飞书、Chrome；未填写时默认为「手工录入」"
                />
              </label>
              <label className="bake-form-field">
                <span className="bake-form-label">文本信息</span>
                <textarea
                  className="bake-textarea"
                  rows={5}
                  value={createText}
                  onChange={(event) => setCreateText(event.target.value)}
                  placeholder="输入需要采集的文本内容，保存后将自动进入提炼流程"
                />
              </label>
              <div className="bake-form-field">
                <span className="bake-form-label">截图</span>
                <div className="bake-muted bake-capture-create__screenshot-tip">
                  支持选择图片文件，或直接 Ctrl+V 粘贴截图
                </div>
                <input
                  ref={createScreenshotInputRef}
                  type="file"
                  accept="image/*"
                  className="bake-input bake-input--file"
                  onChange={handleScreenshotSelect}
                />
                {createScreenshotPreview && (
                  <div className="bake-capture-create__screenshot-preview">
                    <img src={createScreenshotPreview} alt="截图预览" />
                    <button
                      type="button"
                      className="bake-capture-create__screenshot-remove"
                      aria-label="移除截图"
                      onClick={handleRemoveScreenshot}
                    >
                      <X size={14} aria-hidden="true" />
                    </button>
                  </div>
                )}
              </div>
              {createError && (
                <div className="bake-inline-message bake-inline-message--error">{createError}</div>
              )}
              <div className="bake-muted bake-capture-create__hint">
                标题与应用名称可留空，保存后默认为「手工录入」；手工新建的记录将自动进入提炼流程，与自动采集的记录一样被提炼为时间线和知识。
              </div>
            </div>
            <div className="bake-modal__footer">
              <BakeButton disabled={isCreating} onClick={handleCloseCreateDialog}>取消</BakeButton>
              <BakeButton primary disabled={!canSubmitCreate} onClick={handleCreateCapture}>
                {isCreating ? '保存中…' : '保存'}
              </BakeButton>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export { captureNeedsTextRefresh, parseDateInputToMs }

export default BakeCaptureTab
