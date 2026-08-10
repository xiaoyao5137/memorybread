/**
 * RagPanel v2 — 记忆面包面板（优化版）
 *
 * 改进：
 * 1. 移除收起功能（不再支持收起到 buddy 模式）
 * 2. 更换图标为工作相关的图标
 * 3. 优化布局和样式
 * 4. 增加任务模板快捷入口：
 *    - 空状态：展示全量模板（按分类分组）
 *    - 有回答时：模板区折叠在参考来源下方，可展开/收起
 */

import React, { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { invoke } from '@tauri-apps/api/core'
import { ChevronDown, ExternalLink, Loader2 } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { useFetchRagHistory, useModelStatus, useRagQuery } from '../hooks/useApi'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { fetchBillingBalance } from '../utils/authApi'
import { createOptionalCloudRequestSignal, optionalCloudIsReachable } from '../utils/optionalCloud'
import { CREATION_MODEL_DEFS, LOCAL_CREATION_MODEL_ID, REMOTE_CREATION_MODEL_ID, canUseRemoteCreationModel, getEffectiveCreationModelId, getModelDisplayName } from '../utils/modelSelection'
import { BUILTIN_TEMPLATES, CATEGORY_COLORS, groupTemplatesByCategory } from '../data/taskTemplates'
import ModelSelect from './ModelSelect'
import { HistoryPagination, HistorySearch } from './HistoryBrowserControls'
import { BreadAppIcon, BreadToolIcon } from './icons/BreadIcons'
import type { RagContext, RagHistoryItem } from '../types'


interface RagPanelProps {
  className?: string
}

const GROUPED_TEMPLATES = groupTemplatesByCategory(BUILTIN_TEMPLATES)
const HISTORY_PAGE_SIZE = 20

type MarkdownBlock =
  | { type: 'markdown'; content: string }
  | { type: 'table'; headers: string[]; alignments: Array<'left' | 'center' | 'right'>; rows: string[][] }

const splitTableRow = (line: string) => {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  const cells: string[] = []
  let cell = ''

  for (let index = 0; index < trimmed.length; index += 1) {
    const char = trimmed[index]
    if (char === '|' && trimmed[index - 1] !== '\\') {
      cells.push(cell.replace(/\\\|/g, '|').trim())
      cell = ''
    } else {
      cell += char
    }
  }

  cells.push(cell.replace(/\\\|/g, '|').trim())
  return cells
}

const isTableSeparator = (line: string) => {
  const cells = splitTableRow(line)
  return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell))
}

const isPotentialTableRow = (line: string) => {
  const trimmed = line.trim()
  return trimmed.startsWith('|') && trimmed.endsWith('|') && splitTableRow(trimmed).length > 1
}

const tableAlignments = (separatorLine: string): Array<'left' | 'center' | 'right'> =>
  splitTableRow(separatorLine).map(cell => {
    if (cell.startsWith(':') && cell.endsWith(':')) return 'center'
    if (cell.endsWith(':')) return 'right'
    return 'left'
  })

const parseMarkdownBlocks = (content: string): MarkdownBlock[] => {
  const lines = content.split('\n')
  const blocks: MarkdownBlock[] = []
  let markdownBuffer: string[] = []
  let index = 0

  const flushMarkdown = () => {
    const markdown = markdownBuffer.join('\n').trim()
    if (markdown) blocks.push({ type: 'markdown', content: markdown })
    markdownBuffer = []
  }

  while (index < lines.length) {
    const current = lines[index]
    const next = lines[index + 1]
    if (isPotentialTableRow(current) && next && isTableSeparator(next)) {
      flushMarkdown()
      const headers = splitTableRow(current)
      const alignments = tableAlignments(next)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && isPotentialTableRow(lines[index])) {
        rows.push(splitTableRow(lines[index]))
        index += 1
      }
      blocks.push({ type: 'table', headers, alignments, rows })
      continue
    }

    markdownBuffer.push(current)
    index += 1
  }

  flushMarkdown()
  return blocks
}

const formatTs = (ts?: number | string | null) => {
  if (!ts) return ''
  const num = typeof ts === 'number' ? ts : Number(ts)
  if (Number.isFinite(num)) return new Date(num < 10_000_000_000 ? num * 1000 : num).toLocaleString('zh-CN')
  return String(ts)
}

const formatInferenceLatency = (latencyMs?: number | null) => {
  if (latencyMs == null) return '未记录'
  if (latencyMs < 1000) return `${latencyMs} ms`
  return `${(latencyMs / 1000).toFixed(latencyMs < 10_000 ? 1 : 0)} 秒`
}

const sanitizeRagAnswer = (content: string) => {
  if (!content) return content
  const cutMarkers = [
    '依据证据',
    '证据依据',
    '引用依据',
    '参考依据',
    '【量化证据】',
    '量化证据：',
    '以下是本周真实工作记录',
    '原始工作记录：',
  ]
  const lines = content.split('\n')
  const cleaned: string[] = []

  for (const line of lines) {
    if (cutMarkers.some(marker => line.includes(marker))) break
    cleaned.push(line)
  }

  return cleaned.join('\n').trim()
}

const compactButtonStyle: React.CSSProperties = {
  height: 32,
  padding: '0 10px',
  border: '1px solid #d0d5dd',
  borderRadius: 6,
  background: '#fff',
  color: '#344054',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
}

const HistoryScreenshotThumbnail = ({
  screenshotPath,
  onPreview,
}: {
  screenshotPath: string
  onPreview: (src: string) => void
}) => {
  const [src, setSrc] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    setSrc(null)
    setFailed(false)

    invoke<string>('read_floating_assist_image_data_url', { path: screenshotPath })
      .then((dataUrl) => {
        if (cancelled) return
        if (dataUrl) {
          setSrc(dataUrl)
        } else {
          setFailed(true)
        }
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })

    return () => {
      cancelled = true
    }
  }, [screenshotPath])

  const openPreview = (event: React.MouseEvent | React.KeyboardEvent) => {
    event.stopPropagation()
    if (!src) return
    onPreview(src)
  }

  return (
    <span
      role="button"
      tabIndex={0}
      className={`rag-panel__history-shot${failed ? ' rag-panel__history-shot--unavailable' : ''}`}
      onClick={openPreview}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          openPreview(event)
        }
      }}
      aria-label={src ? '查看悬浮球截屏' : failed ? '悬浮球截屏暂不可用' : '正在加载悬浮球截屏'}
      aria-disabled={!src}
    >
      {src ? (
        <img src={src} alt="悬浮球截屏缩略图" />
      ) : (
        <span className="rag-panel__history-shot-placeholder" aria-hidden="true">
          {failed ? '截屏暂不可用' : '加载中'}
        </span>
      )}
    </span>
  )
}

const RagPanel: React.FC<RagPanelProps> = ({ className = '' }) => {
  const {
    ragQuery,
    ragAnswer,
    ragContexts,
    ragLoading,
    ragError,
    setRagQuery,
    adminApiBaseUrl,
    authToken,
    currentUser,
    cloudBalance,
    setCloudBalance,
    creationModelConfigs,
    setCreationModelConfig,
  } = useAppStore()

  const [inputValue, setInputValue] = useState(ragQuery)
  const [topTab, setTopTab] = useState<'consult' | 'history'>('consult')
  const [activeBottomTab, setActiveBottomTab] = useState<'references' | 'templates' | null>(null)
  const [ragHistory, setRagHistory] = useState<RagHistoryItem[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(1)
  const [historySearch, setHistorySearch] = useState('')
  const [debouncedHistorySearch, setDebouncedHistorySearch] = useState('')
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [copySuccess, setCopySuccess] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [screenshotPreview, setScreenshotPreview] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const answerRef = useRef<HTMLDivElement>(null)
  const queryImeGuard = useImeCompositionGuard<HTMLTextAreaElement>()
  const doQuery = useRagQuery()
  const fetchRagHistory = useFetchRagHistory()
  const { status: modelStatus, ready: modelsReady, loading: modelStatusLoading } = useModelStatus()
  const remoteModelAllowed = canUseRemoteCreationModel(currentUser, cloudBalance)
  const activeModelId = getEffectiveCreationModelId(creationModelConfigs, remoteModelAllowed)

  const handleSelectModel = useCallback((modelId: string) => {
    if (modelId === REMOTE_CREATION_MODEL_ID && !remoteModelAllowed) return
    setCreationModelConfig(modelId, { enabled: true })
  }, [remoteModelAllowed, setCreationModelConfig])

  useEffect(() => {
    if (!authToken || !currentUser) {
      setCloudBalance(null)
      return
    }
    let cancelled = false
    let refreshing = false
    const lifecycleController = new AbortController()
    const refreshBalance = async () => {
      if (cancelled || refreshing || !optionalCloudIsReachable()) return
      refreshing = true
      const request = createOptionalCloudRequestSignal(lifecycleController.signal)
      try {
        const balance = await fetchBillingBalance(adminApiBaseUrl, authToken, request.signal)
        if (!cancelled) setCloudBalance(balance)
      } catch {
        if (!cancelled) setCloudBalance(null)
      } finally {
        request.dispose()
        refreshing = false
      }
    }
    const useLocalModelOffline = () => setCloudBalance(null)
    void refreshBalance()
    window.addEventListener('online', refreshBalance)
    window.addEventListener('offline', useLocalModelOffline)
    return () => {
      cancelled = true
      lifecycleController.abort()
      window.removeEventListener('online', refreshBalance)
      window.removeEventListener('offline', useLocalModelOffline)
    }
  }, [adminApiBaseUrl, authToken, currentUser, setCloudBalance])

  useEffect(() => {
    const active = creationModelConfigs.find(config => config.enabled)?.id
    if (active === REMOTE_CREATION_MODEL_ID && !remoteModelAllowed) {
      setCreationModelConfig(LOCAL_CREATION_MODEL_ID, { enabled: true })
    }
  }, [creationModelConfigs, remoteModelAllowed, setCreationModelConfig])

  // 内容变化时自动调整高度
  const adjustHeight = useCallback((el: HTMLTextAreaElement) => {
    const maxHeight = 220
    el.style.height = 'auto'
    const nextHeight = Math.min(el.scrollHeight, maxHeight)
    el.style.height = `${nextHeight}px`
    el.style.overflowY = el.scrollHeight > maxHeight ? 'auto' : 'hidden'
  }, [])

  // inputValue 变化时（含模板填入、外部更新）同步高度
  useEffect(() => {
    if (textareaRef.current) adjustHeight(textareaRef.current)
  }, [inputValue, adjustHeight])

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInputValue(e.target.value)
    adjustHeight(e.target)
  }, [adjustHeight])

  const handleInputWheel = useCallback((event: React.WheelEvent<HTMLTextAreaElement>) => {
    const el = event.currentTarget
    if (el.scrollHeight <= el.clientHeight) return

    const scrollingUp = event.deltaY < 0
    const scrollingDown = event.deltaY > 0
    const canScrollUp = el.scrollTop > 0
    const canScrollDown = el.scrollTop + el.clientHeight < el.scrollHeight

    if ((scrollingUp && canScrollUp) || (scrollingDown && canScrollDown)) {
      event.preventDefault()
      event.stopPropagation()
      el.scrollTop += event.deltaY
    }
  }, [])

  const refreshHistory = useCallback(async (signal?: AbortSignal) => {
    setHistoryLoading(true)
    setHistoryError(null)
    try {
      const result = await fetchRagHistory({
        limit: HISTORY_PAGE_SIZE,
        offset: (historyPage - 1) * HISTORY_PAGE_SIZE,
        query: debouncedHistorySearch,
      }, signal)
      if (signal?.aborted) return
      setRagHistory(result.items)
      setHistoryTotal(result.total)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setRagHistory([])
      setHistoryTotal(0)
      setHistoryError('咨询记录加载失败，请稍后重试。')
    } finally {
      if (!signal?.aborted) setHistoryLoading(false)
    }
  }, [debouncedHistorySearch, fetchRagHistory, historyPage])

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (queryImeGuard.shouldBlockSubmit()) return
      const q = inputValue.trim()
      if (!q) return
      setRagQuery(q)
      try {
        await doQuery(q)
        setActiveBottomTab('references')
        if (historyPage === 1) {
          void refreshHistory()
        } else {
          setHistoryPage(1)
        }
      } catch {
        // error is set in store by useRagQuery
      }
    },
    [inputValue, setRagQuery, doQuery, historyPage, refreshHistory, queryImeGuard]
  )

  const handleTemplateClick = useCallback(
    (instruction: string) => {
      setInputValue(instruction)
      setRagQuery(instruction)
    },
    [setRagQuery]
  )

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setHistoryPage(1)
      setDebouncedHistorySearch(historySearch.trim())
    }, 300)
    return () => window.clearTimeout(timer)
  }, [historySearch])

  useEffect(() => {
    const controller = new AbortController()
    void refreshHistory(controller.signal)
    return () => controller.abort()
  }, [refreshHistory])

  useEffect(() => {
    if (!ragLoading) return
    setElapsedSeconds(0)
    const timer = window.setInterval(() => {
      setElapsedSeconds(prev => prev + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [ragLoading])

  useEffect(() => {
    if (answerRef.current) answerRef.current.scrollTop = answerRef.current.scrollHeight
  }, [ragAnswer])

  const handleCopy = async () => {
    await navigator.clipboard.writeText(sanitizeRagAnswer(ragAnswer))
    setCopySuccess(true)
    setTimeout(() => setCopySuccess(false), 2000)
  }

  const handleOpenReference = (item: RagContext) => {
    const sourceType = item.source_type || item.source
    const referenceType = ['document', 'bake_knowledge', 'operation', 'action'].includes(sourceType || '')
      ? sourceType
      : item.knowledge_id
        ? 'knowledge'
        : sourceType || 'capture'
    const hasInternalTarget = Boolean(
      item.knowledge_id ||
      item.capture_id ||
      item.document_id ||
      item.artifact_id ||
      item.doc_key ||
      sourceType === 'document' ||
      sourceType === 'bake_knowledge' ||
      sourceType === 'operation',
    )
    if (!hasInternalTarget && (item.source_url || item.url)) {
      window.open(item.source_url || item.url || '', '_blank', 'noopener,noreferrer')
      return
    }
    window.dispatchEvent(new CustomEvent('view-rag-reference', {
      detail: {
        type: referenceType,
        captureId: item.capture_id,
        knowledgeId: item.knowledge_id,
        artifactId: item.artifact_id,
        documentId: item.document_id,
        docKey: item.doc_key,
      },
    }))
  }

  const handleRestoreHistory = (item: RagHistoryItem) => {
    setRagQuery(item.query)
    setInputValue(item.query)
    useAppStore.getState().setRagResult(sanitizeRagAnswer(item.answer), item.contexts ?? [])
    setTopTab('consult')
    setActiveBottomTab('references')
    setTimeout(() => answerRef.current?.scrollTo({ top: 0, behavior: 'smooth' }), 100)
  }

  const historyScreenshot = (item: RagHistoryItem) =>
    (item.contexts ?? []).find(ctx => ctx.source_type === 'floating_assist' && ctx.screenshot_path)

  const toggleBottomTab = (tab: 'references' | 'templates') =>
    setActiveBottomTab(prev => prev === tab ? null : tab)
  const thinkingProgress = ragLoading
    ? Math.min(95, Math.max(5, Math.round((elapsedSeconds / 120) * 100)))
    : ragAnswer
      ? 100
      : 0

  const markdownComponents = {
    h1: ({ node, children, ...props }: any) => <h1 style={{ fontSize: 26, lineHeight: 1.25, margin: '0 0 18px' }} {...props}>{children}</h1>,
    h2: ({ node, children, ...props }: any) => <h2 style={{ fontSize: 20, lineHeight: 1.35, margin: '24px 0 12px' }} {...props}>{children}</h2>,
    h3: ({ node, children, ...props }: any) => <h3 style={{ fontSize: 16, lineHeight: 1.45, margin: '18px 0 9px' }} {...props}>{children}</h3>,
    p: ({ node, ...props }: any) => <p style={{ margin: '9px 0', lineHeight: 1.75 }} {...props} />,
    li: ({ node, ...props }: any) => <li style={{ margin: '6px 0', lineHeight: 1.65 }} {...props} />,
    code: ({ node, ...props }: any) => <code style={{ background: '#f2f4f7', padding: '2px 5px', borderRadius: 4 }} {...props} />,
    a: ({ node, href, children, ...props }: any) => (
      <a href={href} target={href?.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer" style={{ color: '#0f766e', textDecoration: 'underline' }} {...props}>{children}</a>
    ),
  }

  const renderHistoryList = () => {
    if (historyLoading && ragHistory.length === 0) {
      return <div className="history-browser__state">正在加载咨询记录…</div>
    }
    if (historyError) {
      return (
        <div className="history-browser__state history-browser__state--error" role="alert">
          <span>{historyError}</span>
          <button type="button" onClick={() => void refreshHistory()}>重新加载</button>
        </div>
      )
    }
    if (ragHistory.length === 0) {
      return (
        <div className="history-browser__state">
          {debouncedHistorySearch ? '没有找到匹配的咨询记录。' : '暂无咨询记录。'}
        </div>
      )
    }
    return (
      <div className="rag-panel__history-list">
        {ragHistory.map((item) => {
          const shot = historyScreenshot(item)
          const shotPath = shot?.screenshot_path || ''
          return (
            <button key={item.id} className="rag-panel__history-item" onClick={() => handleRestoreHistory(item)}>
              {shotPath && (
                <HistoryScreenshotThumbnail
                  screenshotPath={shotPath}
                  onPreview={setScreenshotPreview}
                />
              )}
              <span className="rag-panel__history-main">
                <span className="rag-panel__history-query">{item.query}</span>
                <span className="rag-panel__history-meta">
                  {formatTs(item.ts)} · 模型：{getModelDisplayName(item.model)} · 推理耗时：{formatInferenceLatency(item.latency_ms)} · {shotPath ? '悬浮截屏 · ' : ''}{item.context_count} 条参考
                </span>
              </span>
            </button>
          )
        })}
      </div>
    )
  }

  return (
    <div
      className={`rag-panel ${className}`}
      data-testid="rag-panel"
      role="dialog"
      aria-label="记忆面包问答面板"
    >
      <div className="rag-panel__top-tabs" data-testid="rag-panel-header">
        {([
          { key: 'consult', label: '咨询' },
          { key: 'history', label: `咨询记录${historyTotal ? ` (${historyTotal})` : ''}` },
        ] as const).map(({ key, label }) => (
          <button
            key={key}
            type="button"
            onClick={() => setTopTab(key)}
            className={`rag-panel__top-tab${topTab === key ? ' rag-panel__top-tab--active' : ''}`}
          >
            {label}
          </button>
        ))}
      </div>

      {topTab === 'history' ? (
        <div className="rag-panel__history-page">
          <HistorySearch
            value={historySearch}
            onChange={setHistorySearch}
            placeholder="搜索问题或回答"
            ariaLabel="搜索咨询记录"
            total={historyTotal}
            loading={historyLoading}
          />
          <div className="history-browser__list-scroll">
            {renderHistoryList()}
          </div>
          <HistoryPagination
            page={historyPage}
            pageSize={HISTORY_PAGE_SIZE}
            total={historyTotal}
            loading={historyLoading}
            onPageChange={setHistoryPage}
          />
        </div>
      ) : (
        <>
      {/* 模型未就绪提示 */}
      {!modelStatusLoading && !modelsReady && (
        <div style={{
          margin: '12px 16px',
          padding: '12px',
          background: '#FFF3CD',
          border: '1px solid #FFE69C',
          borderRadius: 8,
          fontSize: 13,
          color: '#856404',
        }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>AI 能力尚未就绪</div>
          <div style={{ marginBottom: 8 }}>
            {!modelStatus.runtime && '本地 AI 能力尚未就绪。'}
            {!modelStatus.llm && '分析模型尚未加载。'}
            {!modelStatus.embedding && '语义索引尚未加载。'}
          </div>
          <div style={{ fontSize: 12 }}>
            请前往「AI 能力」检查状态。
          </div>
        </div>
      )}

      {/* 输入区域 */}
      <form
        className="rag-panel__form"
        onSubmit={handleSubmit}
        data-testid="rag-panel-form"
      >
        <textarea
          ref={textareaRef}
          className="rag-panel__input"
          data-testid="rag-panel-input"
          placeholder={modelStatusLoading || modelsReady ? "问我任何工作相关的问题..." : "AI 能力尚未就绪，请先完成配置"}
          value={inputValue}
          onChange={handleInputChange}
          onWheel={handleInputWheel}
          onCompositionStart={queryImeGuard.onCompositionStart}
          onCompositionEnd={queryImeGuard.onCompositionEnd}
          onBlur={queryImeGuard.onBlur}
          onKeyDown={(event) => {
            if (
              event.key === 'Enter'
              && !event.shiftKey
              && !queryImeGuard.isImeEvent(event)
            ) {
              event.preventDefault()
              event.currentTarget.form?.requestSubmit()
            }
          }}
          rows={3}
          style={{ resize: 'none' }}
          disabled={ragLoading || !modelsReady}
        />
        <div className="rag-panel__input-toolbar">
          <ModelSelect
            label="模型"
            value={activeModelId}
            options={CREATION_MODEL_DEFS}
            disabled={ragLoading}
            remoteAllowed={remoteModelAllowed}
            onChange={handleSelectModel}
            title="选择咨询生成模型"
          />
          {activeModelId === REMOTE_CREATION_MODEL_ID && cloudBalance && (
            <span className="rag-panel__credit-balance">
              Credit {cloudBalance.available}
            </span>
          )}
          <button
            type="submit"
            className="rag-panel__submit"
            data-testid="rag-panel-submit"
            disabled={ragLoading || !inputValue.trim() || !modelsReady}
          >
            {ragLoading ? (
              <>
                {/* 加载图标 */}
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="rag-panel__loading-icon"
                >
                  <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                </svg>
                思考中...
              </>
            ) : (
              <>
                <BreadToolIcon name="send" size={16} framed={false} />
                提问
              </>
            )}
          </button>
        </div>
      </form>

      {ragLoading && (
        <ProgressStrip
          label={`已思考 ${elapsedSeconds} 秒`}
          percent={thinkingProgress}
        />
      )}

      {/* 错误提示 */}
      {ragError && (
        <div
          className="rag-panel__error"
          data-testid="rag-panel-error"
          role="alert"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="12" cy="12" r="10" />
            <path d="m15 9-6 6" />
            <path d="m9 9 6 6" />
          </svg>
          {ragError}
        </div>
      )}

      <section className="rag-panel__document" data-testid="rag-panel-answer">
        <div className="rag-panel__document-header">
          <span className="rag-panel__document-title"><BreadAppIcon name="consult" size={20} />咨询输出</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {ragLoading && (
              <span style={{ fontSize: 12, color: '#0f766e', fontWeight: 650 }}>
                {thinkingProgress}% · {elapsedSeconds} 秒
              </span>
            )}
            <button onClick={handleCopy} disabled={!ragAnswer} style={compactButtonStyle}>
              <BreadToolIcon name="copy" size={16} />
              {copySuccess ? '已复制' : '复制'}
            </button>
          </div>
        </div>
        <div ref={answerRef} className="rag-panel__document-body">
          {ragAnswer ? (
            <MarkdownContent content={sanitizeRagAnswer(ragAnswer)} components={markdownComponents} />
          ) : ragLoading ? (
            <div className="rag-panel__document-empty">
              <Loader2 size={28} className="spin" color="#0f766e" />
              <div>
                <div style={{ fontWeight: 650, color: '#0f766e', marginBottom: 4 }}>正在整理答案</div>
                <div>已思考 {elapsedSeconds} 秒，预计进度 {thinkingProgress}%</div>
              </div>
            </div>
          ) : (
            <div className="rag-panel__document-empty">选择模板或输入问题后，咨询输出会在这里呈现。</div>
          )}
        </div>
      </section>

      <div className="rag-panel__bottom-tabs">
        {([
          { key: 'references', label: '参考资料', badge: ragContexts.length, icon: <BreadAppIcon name="memory" size={18} /> },
          { key: 'templates', label: '任务模板', badge: BUILTIN_TEMPLATES.length, icon: <BreadAppIcon name="creation" size={18} /> },
        ] as const).map(({ key, label, badge, icon }) => (
          <button
            key={key}
            onClick={() => toggleBottomTab(key)}
            className={`rag-panel__bottom-tab${activeBottomTab === key ? ' rag-panel__bottom-tab--active' : ''}`}
          >
            {icon}
            {label}{badge > 0 ? ` (${badge})` : ''}
            <ChevronDown size={14} style={{ transform: activeBottomTab === key ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }} />
          </button>
        ))}
      </div>

      {activeBottomTab && (
        <div className="rag-panel__bottom-panel">
          {activeBottomTab === 'references' && (
            ragContexts.length ? (
              <div className="rag-panel__reference-list" data-testid="rag-panel-contexts">
                {ragContexts.map((ctx, idx) => (
                  <ReferenceRow key={`${ctx.doc_key || ctx.capture_id}-${idx}`} item={ctx} index={idx} onOpenReference={handleOpenReference} />
                ))}
              </div>
            ) : (
              <div className="rag-panel__bottom-empty">暂无参考资料。完成一次咨询后会显示召回来源。</div>
            )
          )}
          {activeBottomTab === 'templates' && (
            <div className="rag-panel__templates">
              {Object.entries(GROUPED_TEMPLATES).map(([category, templates]) => (
                <div key={category} className="rag-panel__template-group">
                  <div
                    className="rag-panel__template-category"
                    style={{ borderColor: CATEGORY_COLORS[category] ?? '#999', color: CATEGORY_COLORS[category] ?? '#999' }}
                  >
                    {category}
                  </div>
                  <div className="rag-panel__template-chips">
                    {templates.map((tpl) => (
                      <button
                        key={tpl.id}
                        className="rag-panel__template-chip"
                        style={{ '--chip-color': CATEGORY_COLORS[category] ?? '#4a90e2' } as React.CSSProperties}
                        onClick={() => handleTemplateClick(tpl.user_instruction)}
                        disabled={ragLoading}
                        title={tpl.user_instruction}
                      >
                        {tpl.name}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
        </>
      )}

      {screenshotPreview && (
        <div className="rag-panel__screenshot-preview" onClick={() => setScreenshotPreview(null)}>
          <img src={screenshotPreview} alt="悬浮球截屏预览" />
        </div>
      )}
    </div>
  )
}

const MarkdownContent = ({ content, components }: { content: string; components: any }) => {
  const inlineComponents = {
    ...components,
    p: ({ children }: any) => <>{children}</>,
  }

  return (
    <>
      {parseMarkdownBlocks(content).map((block, index) => {
        if (block.type === 'markdown') {
          return <ReactMarkdown key={`markdown-${index}`} components={components}>{block.content}</ReactMarkdown>
        }

        return (
          <div key={`table-${index}`} style={{ overflowX: 'auto', margin: '16px 0' }}>
            <table style={{ width: '100%', minWidth: 720, borderCollapse: 'collapse', fontSize: 14, lineHeight: 1.55 }}>
              <thead>
                <tr>
                  {block.headers.map((header, cellIndex) => (
                    <th key={cellIndex} style={{ border: '1px solid #d0d5dd', background: '#f8fafc', color: '#172033', fontWeight: 700, padding: '10px 12px', textAlign: block.alignments[cellIndex] || 'left', verticalAlign: 'top' }}>
                      <ReactMarkdown components={inlineComponents}>{header}</ReactMarkdown>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {block.headers.map((_, cellIndex) => (
                      <td key={cellIndex} style={{ border: '1px solid #d0d5dd', padding: '10px 12px', textAlign: block.alignments[cellIndex] || 'left', verticalAlign: 'top', background: rowIndex % 2 === 0 ? '#fff' : '#fbfcfe' }}>
                        <ReactMarkdown components={inlineComponents}>{row[cellIndex] || ''}</ReactMarkdown>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      })}
    </>
  )
}

const sourceLabel = (item: RagContext) => {
  const source = item.source_type || item.source
  if (source === 'floating_assist') return '悬浮截屏'
  if (item.knowledge_id || source === 'knowledge') return '时间线'
  if (source === 'capture') return '采集记录'
  if (source === 'document') return '文档'
  if (source === 'operation' || source === 'action') return '操作'
  if (source === 'bake_knowledge') return '知识'
  return '参考资料'
}

const referenceTitle = (item: RagContext) =>
  item.title || item.overview || item.summary || item.win_title || item.app_name || `${sourceLabel(item)} #${item.knowledge_id || item.document_id || item.artifact_id || item.capture_id}`

const referenceText = (item: RagContext) => {
  const text = item.text || ''
  if (text && !/^历史咨询关联的采集记录 #\d+$/.test(text)) return text
  const parts = [
    item.summary && `摘要：${item.summary}`,
    item.overview && `概述：${item.overview}`,
    (item.source_url || item.url) && `URL：${item.source_url || item.url}`,
    item.activity_type && `活动：${item.activity_type}`,
    item.content_origin && `来源：${item.content_origin}`,
  ].filter(Boolean)
  return parts.join('\n') || `${sourceLabel(item)} #${item.knowledge_id || item.document_id || item.artifact_id || item.capture_id}`
}

const ProgressStrip = ({ label, percent }: { label: string; percent: number }) => (
  <div style={{ margin: '0 16px 12px', display: 'grid', gap: 6 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: '#475467' }}>
      <span>{label}</span>
      <span>{percent}%</span>
    </div>
    <div style={{ height: 6, borderRadius: 999, background: '#e4e7ec', overflow: 'hidden' }}>
      <div style={{ width: `${percent}%`, height: '100%', borderRadius: 999, background: '#0f766e', transition: 'width 0.25s ease' }} />
    </div>
  </div>
)

const ReferenceRow = ({ item, index, onOpenReference }: { item: RagContext; index: number; onOpenReference: (item: RagContext) => void }) => {
  const label = sourceLabel(item)
  const primaryTime = item.observed_at || item.event_time_start || item.time || item.start_time || item.end_time
  const openLabel = item.source_url || item.url
    ? '打开文档'
    : item.knowledge_id ? '打开时间线' : '打开采集'

  return (
    <div className="rag-panel__reference-item" data-testid={`context-item-${index}`}>
      <div className="rag-panel__reference-head">
        <div>
          <div className="rag-panel__reference-title">R#{index + 1} · {label} · {referenceTitle(item)}</div>
          <div className="rag-panel__reference-meta">
            {item.app_name ? `${item.app_name} · ` : ''}{item.win_title ? `${item.win_title} · ` : ''}{formatTs(primaryTime)}
            {item.score ? ` · 相关 ${Math.round(item.score * 100)}%` : ''}
            {item.evidence_strength ? ` · ${item.evidence_strength}` : ''}
          </div>
        </div>
        <button type="button" onClick={() => onOpenReference(item)} style={compactButtonStyle}>
          <ExternalLink size={13} />
          {openLabel}
        </button>
      </div>
      <div className="rag-panel__reference-text">
        {referenceText(item).split('\n').map((line, lineIndex) => (
          <React.Fragment key={lineIndex}>{line}{lineIndex < referenceText(item).split('\n').length - 1 && <br />}</React.Fragment>
        ))}
      </div>
    </div>
  )
}

export default RagPanel
