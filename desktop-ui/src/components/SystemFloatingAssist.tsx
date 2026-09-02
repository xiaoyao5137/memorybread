import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import ReactMarkdown from 'react-markdown'
import { ChevronDown, ChevronUp, EyeOff, Home, Loader2, MessageSquare, Sparkles, X } from 'lucide-react'
import { listen } from '@tauri-apps/api/event'
import {
  RAG_REFERENCE_LIMIT,
  runGatewayRagQueryStream,
  runRagQueryStream,
  type RagStreamCallbacks,
  type RagStreamStatus,
} from '../hooks/useApi'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { useAppStore } from '../store/useAppStore'
import type { BreadcrumbDefinition, RagContext } from '../types'
import { buildAttachmentMetadata, buildAttachmentPrompt, filesToAttachments, formatAttachmentSize, type UserAttachment } from '../utils/attachments'
import { fetchBillingBalance } from '../utils/authApi'
import { BREADCRUMBS_CHANGED_KEY, fetchBreadcrumbProfile } from '../utils/breadcrumbApi'
import { createOptionalCloudRequestSignal, optionalCloudIsReachable } from '../utils/optionalCloud'
import {
  FLOATING_ASSIST_AUTO_TASK_KEY,
  FLOATING_ASSIST_ENABLED_KEY,
  detectFloatingAssistTaskFromOcr,
  readFloatingAssistAutoTaskConfig,
  writeFloatingAssistAutoTaskConfig,
  type FloatingAssistAutoTaskConfig,
  type FloatingAssistTaskDetection,
} from '../utils/floatingAssistAutoTask'
import { REMOTE_CREATION_MODEL_ID, canUseRemoteCreationModel, getEffectiveCreationModelId } from '../utils/modelSelection'
import {
  INTERACTION_SETTINGS_CHANGED_EVENT,
  INTERACTION_SETTINGS_KEY,
  floatingBallActionHint,
  normalizeInteractionSettings,
  readInteractionSettings,
  type FloatingBallAction,
  type InteractionSettings,
} from '../utils/interactionSettings'
import { toUserFacingError } from '../utils/userFacingError'
import { BreadToolIcon } from './icons/BreadIcons'
import breadRollAsset from '../assets/floating-assist/bread-roll.png'
import './SystemFloatingAssist.css'

type AssistPhase = 'idle' | 'receiving' | 'capturing' | 'answering' | 'done' | 'error'

const FLOATING_ASSIST_BALL_SIZE = 82
const FLOATING_ASSIST_CONTEXT_MENU_WIDTH = 214
const FLOATING_ASSIST_CONTEXT_MENU_TOP = 86
const FLOATING_ASSIST_CONTEXT_MENU_HEIGHT = 160
const FLOATING_ASSIST_CONTEXT_MENU_WINDOW_HEIGHT = FLOATING_ASSIST_CONTEXT_MENU_TOP + FLOATING_ASSIST_CONTEXT_MENU_HEIGHT
const FLOATING_ASSIST_DONE_IDLE_DELAY_MS = 5 * 60 * 1000
const DEFAULT_VISIBLE_REFERENCE_COUNT = 5
const AUTO_TASK_SCAN_INITIAL_DELAY_MS = 10_000
const AUTO_TASK_SCAN_INTERVAL_MS = 120_000
const AUTO_TASK_USER_IDLE_GUARD_MS = 3_000
const AUTO_TASK_DEDUP_CACHE_LIMIT = 64
const FLOATING_ASSIST_DRAG_TICK_MS = 33
const FLOATING_ASSIST_AMBIENT_ACTIVE_MS = 2_200
const FLOATING_ASSIST_AMBIENT_PERIOD_MS = 8_000
const STREAM_STAGE_PROGRESS: Record<string, { target: number; durationMs: number }> = {
  queued: { target: 27, durationMs: 12_000 },
  understanding: { target: 41, durationMs: 30_000 },
  retrieving: { target: 57, durationMs: 30_000 },
  waiting_generation: { target: 61, durationMs: 60_000 },
  answering: { target: 94, durationMs: 120_000 },
}

const isAssistPhase = (value: string | null): value is AssistPhase =>
  value === 'idle' || value === 'receiving' || value === 'capturing' || value === 'answering' || value === 'done' || value === 'error'

interface FloatingAssistOcrResult {
  text: string
  confidence: number
  screenshot_path: string
  width: number
  height: number
  screenshot_source?: string
  app_bundle_id?: string | null
  app_name?: string | null
  window_title?: string | null
}

interface FloatingAssistDragOrigin {
  offset_x: number
  offset_y: number
}

interface RunAssistOptions {
  automatic?: boolean
  detection?: FloatingAssistTaskDetection
}

interface PendingFloatingAssistTask {
  ocr: FloatingAssistOcrResult
  detection: FloatingAssistTaskDetection
}

const hasSeenAutoTaskFingerprint = (seen: Map<string, number>, fingerprint: string) =>
  seen.has(fingerprint)

const rememberAutoTaskFingerprint = (seen: Map<string, number>, fingerprint: string, ts: number) => {
  if (!fingerprint) return
  seen.delete(fingerprint)
  seen.set(fingerprint, ts)
  while (seen.size > AUTO_TASK_DEDUP_CACHE_LIMIT) {
    const oldest = seen.keys().next().value
    if (!oldest) break
    seen.delete(oldest)
  }
}

type MarkdownBlock =
  | { type: 'markdown'; content: string }
  | { type: 'table'; headers: string[]; alignments: Array<'left' | 'center' | 'right'>; rows: string[][] }

const splitFloatingAssistAnswer = (content: string) => {
  const understandingMatch = content.match(/^#{2,6}\s*用户问题理解\s*[:：]?\s*$/im)
  if (!understandingMatch || understandingMatch.index == null) {
    return { userQuestionUnderstanding: '', responseContent: content.trim() }
  }

  const beforeUnderstanding = content.slice(0, understandingMatch.index).trim()
  const understandingStart = understandingMatch.index + understandingMatch[0].length
  const afterUnderstanding = content.slice(understandingStart)
  const nextHeadingMatch = afterUnderstanding.match(/\n#{2,6}\s+/)
  const understandingEnd = nextHeadingMatch?.index ?? afterUnderstanding.length
  const userQuestionUnderstanding = afterUnderstanding.slice(0, understandingEnd).trim()
  const restAfterUnderstanding = afterUnderstanding.slice(understandingEnd).replace(/^\n#{2,6}\s*回答\s*[:：]?\s*/i, '').trim()
  const responseContent = [beforeUnderstanding, restAfterUnderstanding].filter(Boolean).join('\n\n').trim()

  return { userQuestionUnderstanding, responseContent }
}

const splitTableRow = (line: string) => {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  const cells: string[] = []
  let cell = ''
  let escaped = false
  for (const char of trimmed) {
    if (char === '|' && !escaped) {
      cells.push(cell.replace(/\\\|/g, '|').trim())
      cell = ''
    } else {
      cell += char
    }
    escaped = char === '\\' && !escaped
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

const buildFloatingAssistQuery = (ocrText: string) => {
  const trimmed = ocrText.trim()
  return [
    '你是记忆面包的工作场景助手。下面是用户当前屏幕 OCR 内容。',
    '请先直接推断用户正在处理的问题，再给出可直接复制使用的答案、步骤或文本。',
    '如果屏幕里出现的是用户准备发送或询问的一句话，必须把这句话当作待回答的问题，不要改写成让用户去问别人的提问话术。',
    '对于“是什么样”“结果如何”“有没有结论”这类问题，必须直接回答结果和依据，不要输出追问模板。',
    '即使信息不足，也要基于可见内容和参考资料给出最可能的判断；不要追问用户，不要只要求用户补充信息。',
    '当前屏幕 OCR 是最高优先级；历史参考资料只能辅助，不得覆盖或替换当前屏幕内容。',
    '',
    '输出要求：',
    '- 先用一句话说明你判断出的用户需求。',
    '- 再给出可直接使用的结果或操作步骤。',
    '- 不要提及供应商模型、密钥、成本或内部实现。',
    '',
    `当前屏幕 OCR：\n${trimmed}`,
  ].join('\n')
}

const buildManualFloatingAssistQuery = (instruction: string, ocrText?: string) => {
  const trimmedInstruction = instruction.trim()
  const trimmedOcr = ocrText?.trim()
  return [
    '你是记忆面包的工作场景助手。用户在悬浮咨询面板中手工输入了一条指令。',
    '请优先回答这条手工指令；如果同时提供了当前屏幕 OCR 内容，请把它作为辅助上下文。',
    '不要提及供应商模型、密钥、成本或内部实现。',
    '',
    `用户手工指令：\n${trimmedInstruction}`,
    trimmedOcr ? `\n当前屏幕 OCR：\n${trimmedOcr}` : '',
  ].filter(Boolean).join('\n')
}

const isAbortError = (error: unknown, signal?: AbortSignal) =>
  signal?.aborted === true || (error instanceof DOMException && error.name === 'AbortError')

interface SystemFloatingAssistProps {
  developmentMode?: boolean
}

const SystemFloatingAssist: React.FC<SystemFloatingAssistProps> = ({
  developmentMode = import.meta.env.DEV,
}) => {
  const debugParams = useMemo(() => new URLSearchParams(window.location.search), [])
  const debugPreviewEnabled = window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost'
  const debugPhaseValue = debugParams.get('debugPhase')
  const debugPhase: AssistPhase | null = debugPreviewEnabled && isAssistPhase(debugPhaseValue)
    ? debugPhaseValue
    : null
  const debugBackground = debugPreviewEnabled && debugParams.get('debugBg') === 'checker'
  const debugAnswer = debugPreviewEnabled ? debugParams.get('debugAnswer') : null
  const debugDoneIdleMsParam = debugParams.get('debugDoneIdleMs')
  const debugDoneIdleMs = debugDoneIdleMsParam == null ? NaN : Number(debugDoneIdleMsParam)
  const doneIdleDelayMs = debugPreviewEnabled && Number.isFinite(debugDoneIdleMs) && debugDoneIdleMs >= 0
    ? debugDoneIdleMs
    : FLOATING_ASSIST_DONE_IDLE_DELAY_MS
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)
  const adminApiBaseUrl = useAppStore((s) => s.adminApiBaseUrl)
  const gatewayApiBaseUrl = useAppStore((s) => s.gatewayApiBaseUrl)
  const authToken = useAppStore((s) => s.authToken)
  const currentUser = useAppStore((s) => s.currentUser)
  const cloudBalance = useAppStore((s) => s.cloudBalance)
  const setCloudBalance = useAppStore((s) => s.setCloudBalance)
  const creationModelConfigs = useAppStore((s) => s.creationModelConfigs)
  const [phase, setPhase] = useState<AssistPhase>(debugPhase ?? 'idle')
  const [answer, setAnswer] = useState(debugAnswer ?? '')
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [revealing, setRevealing] = useState(false)
  const [screenshot, setScreenshot] = useState<FloatingAssistOcrResult | null>(null)
  const [screenshotSrc, setScreenshotSrc] = useState('')
  const [references, setReferences] = useState<RagContext[]>([])
  const [referencesExpanded, setReferencesExpanded] = useState(false)
  const [outputTruncated, setOutputTruncated] = useState(false)
  const [inferenceElapsedMs, setInferenceElapsedMs] = useState<number | null>(null)
  const [streamStatus, setStreamStatus] = useState('')
  const [previewOpen, setPreviewOpen] = useState(false)
  const [canvasOpen, setCanvasOpen] = useState(false)
  const [contextMenuOpen, setContextMenuOpen] = useState(false)
  const [nativeHovering, setNativeHovering] = useState(false)
  const [ambientAnimating, setAmbientAnimating] = useState(false)
  const [manualInstruction, setManualInstruction] = useState('')
  const manualInputImeGuard = useImeCompositionGuard<HTMLTextAreaElement>()
  const [attachments, setAttachments] = useState<UserAttachment[]>([])
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [floatingBadge, setFloatingBadge] = useState<BreadcrumbDefinition | null>(null)
  const [progress, setProgress] = useState(0)
  const [autoTaskConfig, setAutoTaskConfig] = useState(readFloatingAssistAutoTaskConfig)
  const [interactionSettings, setInteractionSettings] = useState(readInteractionSettings)
  const [pendingAutoTask, setPendingAutoTask] = useState<PendingFloatingAssistTask | null>(null)
  const revealTimerRef = useRef<number | null>(null)
  const clickTimerRef = useRef<number | null>(null)
  const progressTimerRef = useRef<number | null>(null)
  const doneIdleTimerRef = useRef<number | null>(null)
  const autoTaskScanInFlightRef = useRef(false)
  const dragUpdateInFlightRef = useRef(false)
  const lastUserInteractionAtRef = useRef(Date.now())
  const seenAutoTasksRef = useRef<Map<string, number>>(new Map())
  const activeAssistTaskRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const runAssistRef = useRef<() => Promise<void>>(async () => {})
  const fileInputRef = useRef<HTMLInputElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const followStreamingAnswerRef = useRef(true)
  const dragRef = useRef({
    active: false,
    dragging: false,
    startX: 0,
    startY: 0,
    pointerId: -1,
    offsetX: 0,
    offsetY: 0,
    tickId: null as number | null,
  })
  const suppressClickRef = useRef(false)

  const statusText = useMemo(() => {
    if (phase === 'receiving') return '接住新任务'
    if (phase === 'capturing') return `正在识别屏幕 ${progress}%`
    if (phase === 'answering') return `${streamStatus || '正在整理答案'} ${progress}%`
    if (revealing) return '正在生成'
    if (phase === 'done') return '已生成'
    if (phase === 'error') return '需要处理'
    if (answer.trim()) return '已生成'
    if (pendingAutoTask) return '发现可能任务'
    if (autoTaskConfig.enabled) return '自动识别中'
    return '待咨询'
  }, [answer, autoTaskConfig.enabled, pendingAutoTask, phase, progress, revealing, streamStatus])

  const clearDoneIdleTimer = useCallback(() => {
    if (doneIdleTimerRef.current != null) {
      window.clearTimeout(doneIdleTimerRef.current)
      doneIdleTimerRef.current = null
    }
  }, [])

  const resetDoneMascotToIdle = useCallback(() => {
    clearDoneIdleTimer()
    setPhase(current => current === 'done' ? 'idle' : current)
  }, [clearDoneIdleTimer])

  const stopProgress = () => {
    if (progressTimerRef.current != null) {
      window.clearInterval(progressTimerRef.current)
      progressTimerRef.current = null
    }
  }

  const startProgress = (from: number, to: number, durationMs: number) => {
    stopProgress()
    const startedAt = Date.now()
    setProgress(from)
    progressTimerRef.current = window.setInterval(() => {
      const elapsed = Date.now() - startedAt
      const ratio = Math.min(1, elapsed / durationMs)
      const eased = 1 - Math.pow(1 - ratio, 2)
      const nextProgress = Math.min(to, Math.round(from + (to - from) * eased))
      setProgress(current => Math.max(current, nextProgress))
    }, 220)
  }

  const continueStreamProgress = (status: RagStreamStatus) => {
    const plan = STREAM_STAGE_PROGRESS[status.stage] ?? {
      target: Math.min(94, status.progress + 10),
      durationMs: 30_000,
    }
    startProgress(status.progress, Math.max(status.progress, plan.target), plan.durationMs)
  }

  const runFloatingAssistQuery = async (
    query: string,
    metadata: Record<string, unknown>,
    signal: AbortSignal,
    callbacks: RagStreamCallbacks,
  ) => {
    const runLocalQuery = () => runRagQueryStream(
      apiBaseUrl,
      creationModelConfigs,
      query,
      RAG_REFERENCE_LIMIT,
      metadata,
      false,
      signal,
      callbacks,
    )

    if (activeModelId !== REMOTE_CREATION_MODEL_ID || !currentUser?.id) {
      return runLocalQuery()
    }

    try {
      return await runGatewayRagQueryStream(
        apiBaseUrl,
        gatewayApiBaseUrl,
        query,
        currentUser.id,
        signal,
        { source: 'floating_assist', metadata },
        callbacks,
      )
    } catch (cloudError) {
      if (isAbortError(cloudError, signal)) throw cloudError
      callbacks.onStatus?.({
        stage: 'local_fallback',
        message: '云端咨询暂时不可用，正在切换本地能力',
        progress: 58,
      })
      try {
        return await runLocalQuery()
      } catch (localError) {
        if (isAbortError(localError, signal)) throw localError
        throw new Error('云端咨询失败，本地备用能力也未完成，请稍后重试')
      }
    }
  }

  const waitForPaint = () => new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => resolve())
    })
  })

  const stopDrag = (releaseClick = true) => {
    const drag = dragRef.current
    const wasDragging = drag.dragging
    if (drag.tickId != null) {
      window.clearInterval(drag.tickId)
    }
    drag.active = false
    drag.dragging = false
    drag.pointerId = -1
    drag.tickId = null
    dragUpdateInFlightRef.current = false
    if (releaseClick && wasDragging) {
      window.setTimeout(() => {
        suppressClickRef.current = false
      }, 260)
    }
  }

  const hasCanvas = canvasOpen
  const busy = phase === 'receiving' || phase === 'capturing' || phase === 'answering'
  const hasGeneratedAnswer = answer.trim().length > 0
  const hasStreamingAnswer = phase === 'answering' && hasGeneratedAnswer
  const remoteModelAllowed = canUseRemoteCreationModel(currentUser, cloudBalance)
  const activeModelId = getEffectiveCreationModelId(creationModelConfigs, remoteModelAllowed)
  const collapseExpandedSurface = () => {
    setCanvasOpen(false)
    setContextMenuOpen(false)
    setPreviewOpen(false)
  }
  const canvasHeight = phase === 'answering' || phase === 'done' || (phase === 'idle' && hasGeneratedAnswer)
    ? Math.min(
      560,
      Math.max(
        phase === 'answering' ? 500 : 420,
        330 + Math.ceil(answer.length / 2.4) + Math.min(references.length, 3) * 42,
      ),
    )
    : phase === 'error'
      ? 370
      : phase === 'idle'
        ? pendingAutoTask ? 330 : 220
        : screenshot
          ? 414
          : 330
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
    let cancelled = false
    let refreshing = false
    const lifecycleController = new AbortController()
    const refreshBadge = () => {
      if (cancelled || refreshing) return
      refreshing = true
      fetchBreadcrumbProfile(apiBaseUrl, lifecycleController.signal)
        .then((profile) => {
          if (!cancelled) setFloatingBadge(profile.equipped.floating_avatar ?? null)
        })
        .catch(() => {
          if (!cancelled) setFloatingBadge(null)
        })
        .finally(() => {
          refreshing = false
        })
    }
    const handleStorage = (event: StorageEvent) => {
      if (event.key === BREADCRUMBS_CHANGED_KEY) refreshBadge()
    }
    refreshBadge()
    window.addEventListener('storage', handleStorage)
    window.addEventListener(BREADCRUMBS_CHANGED_KEY, refreshBadge)
    return () => {
      cancelled = true
      lifecycleController.abort()
      window.removeEventListener('storage', handleStorage)
      window.removeEventListener(BREADCRUMBS_CHANGED_KEY, refreshBadge)
    }
  }, [apiBaseUrl])

  useEffect(() => {
    document.documentElement.classList.add('floating-assist-html')
    document.body.classList.add('floating-assist-body')
    const tauriCleanups: Array<() => void> = []
    const markUserInteraction = () => {
      lastUserInteractionAtRef.current = Date.now()
    }
    const stopGlobalDrag = () => stopDrag()
    window.addEventListener('pointerup', stopGlobalDrag)
    window.addEventListener('mouseup', stopGlobalDrag)
    window.addEventListener('blur', stopGlobalDrag)
    const closeContextMenu = () => setContextMenuOpen(false)
    const handleKeyDown = (event: KeyboardEvent) => {
      markUserInteraction()
      if (event.key === 'Escape') collapseExpandedSurface()
    }
    window.addEventListener('pointerdown', markUserInteraction, { passive: true })
    window.addEventListener('wheel', markUserInteraction, { passive: true })
    window.addEventListener('click', closeContextMenu)
    window.addEventListener('keydown', handleKeyDown)
    const syncInteractionSettings = () => setInteractionSettings(readInteractionSettings())
    const handleInteractionSettingsChanged = (event: Event) => {
      const detail = (event as CustomEvent<InteractionSettings>).detail
      setInteractionSettings(detail ? normalizeInteractionSettings(detail) : readInteractionSettings())
    }
    const handleInteractionStorage = (event: StorageEvent) => {
      if (event.key === INTERACTION_SETTINGS_KEY || event.key === null) {
        syncInteractionSettings()
      }
    }
    window.addEventListener(INTERACTION_SETTINGS_CHANGED_EVENT, handleInteractionSettingsChanged)
    window.addEventListener('storage', handleInteractionStorage)
    void listen<InteractionSettings>(INTERACTION_SETTINGS_CHANGED_EVENT, event => {
      setInteractionSettings(normalizeInteractionSettings(event.payload))
    }).then(dispose => {
      tauriCleanups.push(dispose)
    }).catch(() => {})
    void listen('floating-assist-reset', () => {
      if (revealTimerRef.current != null) {
        window.clearInterval(revealTimerRef.current)
        revealTimerRef.current = null
      }
      if (clickTimerRef.current != null) {
        window.clearTimeout(clickTimerRef.current)
        clickTimerRef.current = null
      }
      clearDoneIdleTimer()
      stopProgress()
      setProgress(0)
      stopDrag(false)
      suppressClickRef.current = false
      collapseExpandedSurface()
      setRevealing(false)
    }).then(dispose => {
      tauriCleanups.push(dispose)
    }).catch(() => {})
    void listen<boolean | FloatingAssistAutoTaskConfig>('floating-assist-auto-task-changed', event => {
      const nextConfig = typeof event.payload === 'boolean'
        ? writeFloatingAssistAutoTaskConfig({
          ...readFloatingAssistAutoTaskConfig(),
          enabled: Boolean(event.payload),
        })
        : writeFloatingAssistAutoTaskConfig(event.payload)
      try {
        localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, String(nextConfig.enabled))
      } catch {
        // ignore
      }
      setAutoTaskConfig(nextConfig)
    }).then(dispose => {
      tauriCleanups.push(dispose)
    }).catch(() => {})
    void listen<boolean>('tray-floating-assist-changed', event => {
      if (event.payload) return
      try {
        writeFloatingAssistAutoTaskConfig({
          ...readFloatingAssistAutoTaskConfig(),
          enabled: false,
        })
      } catch {
        // ignore
      }
      setAutoTaskConfig(readFloatingAssistAutoTaskConfig())
    }).then(dispose => {
      tauriCleanups.push(dispose)
    }).catch(() => {})
    void listen<boolean>('floating-assist-native-hover-changed', event => {
      setNativeHovering(Boolean(event.payload))
    }).then(dispose => {
      tauriCleanups.push(dispose)
    }).catch(() => {})
    return () => {
      document.documentElement.classList.remove('floating-assist-html')
      document.body.classList.remove('floating-assist-body')
      if (revealTimerRef.current != null) {
        window.clearInterval(revealTimerRef.current)
      }
      if (clickTimerRef.current != null) {
        window.clearTimeout(clickTimerRef.current)
      }
      clearDoneIdleTimer()
      stopProgress()
      stopDrag(false)
      window.removeEventListener('pointerdown', markUserInteraction)
      window.removeEventListener('wheel', markUserInteraction)
      window.removeEventListener('pointerup', stopGlobalDrag)
      window.removeEventListener('mouseup', stopGlobalDrag)
      window.removeEventListener('blur', stopGlobalDrag)
      window.removeEventListener('click', closeContextMenu)
      window.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener(INTERACTION_SETTINGS_CHANGED_EVENT, handleInteractionSettingsChanged)
      window.removeEventListener('storage', handleInteractionStorage)
      tauriCleanups.forEach(cleanup => cleanup())
    }
  }, [])

  useEffect(() => {
    if (phase !== 'idle' && phase !== 'done' && phase !== 'error') {
      setAmbientAnimating(false)
      return
    }

    let cancelled = false
    let frameId: number | null = null
    let activeTimer: number | null = null

    const playAmbientCycle = () => {
      setAmbientAnimating(false)
      frameId = window.requestAnimationFrame(() => {
        frameId = null
        if (cancelled) return
        setAmbientAnimating(true)
        activeTimer = window.setTimeout(() => {
          activeTimer = null
          if (!cancelled) setAmbientAnimating(false)
        }, FLOATING_ASSIST_AMBIENT_ACTIVE_MS)
      })
    }

    playAmbientCycle()
    const cycleTimer = window.setInterval(playAmbientCycle, FLOATING_ASSIST_AMBIENT_PERIOD_MS)
    return () => {
      cancelled = true
      window.clearInterval(cycleTimer)
      if (frameId != null) window.cancelAnimationFrame(frameId)
      if (activeTimer != null) window.clearTimeout(activeTimer)
      setAmbientAnimating(false)
    }
  }, [phase])

  useEffect(() => {
    if (phase !== 'done') {
      clearDoneIdleTimer()
      return
    }

    clearDoneIdleTimer()
    doneIdleTimerRef.current = window.setTimeout(() => {
      doneIdleTimerRef.current = null
      setPhase(current => current === 'done' ? 'idle' : current)
    }, doneIdleDelayMs)

    return clearDoneIdleTimer
  }, [clearDoneIdleTimer, doneIdleDelayMs, phase])

  useEffect(() => {
    const width = previewOpen
      ? 720
      : hasCanvas
        ? 392
        : contextMenuOpen
          ? FLOATING_ASSIST_CONTEXT_MENU_WIDTH
          : FLOATING_ASSIST_BALL_SIZE
    const height = previewOpen
      ? 540
      : hasCanvas
        ? Math.max(FLOATING_ASSIST_BALL_SIZE + 8 + canvasHeight, contextMenuOpen ? FLOATING_ASSIST_CONTEXT_MENU_HEIGHT : FLOATING_ASSIST_BALL_SIZE)
        : contextMenuOpen
          ? FLOATING_ASSIST_CONTEXT_MENU_WINDOW_HEIGHT
          : FLOATING_ASSIST_BALL_SIZE
    invoke('set_floating_assist_size', { width, height }).catch(() => {})
  }, [canvasHeight, contextMenuOpen, hasCanvas, previewOpen])

  useEffect(() => {
    if (phase !== 'answering' || !answer || !followStreamingAnswerRef.current) return
    const frameId = window.requestAnimationFrame(() => {
      const body = bodyRef.current
      if (body) body.scrollTop = body.scrollHeight
    })
    return () => window.cancelAnimationFrame(frameId)
  }, [answer, phase])

  const revealAnswer = (content: string) => {
    if (revealTimerRef.current != null) {
      window.clearInterval(revealTimerRef.current)
    }
    setAnswer('')
    setRevealing(true)
    let cursor = 0
    revealTimerRef.current = window.setInterval(() => {
      cursor = Math.min(content.length, cursor + 18)
      setAnswer(content.slice(0, cursor))
      if (cursor >= content.length) {
        if (revealTimerRef.current != null) {
          window.clearInterval(revealTimerRef.current)
          revealTimerRef.current = null
        }
        setRevealing(false)
      }
    }, 28)
  }

  const clearAnswerReveal = () => {
    if (revealTimerRef.current != null) {
      window.clearInterval(revealTimerRef.current)
      revealTimerRef.current = null
    }
    setRevealing(false)
  }

  const clearCanvasContent = () => {
    abortRef.current?.abort()
    abortRef.current = null
    activeAssistTaskRef.current = false
    clearDoneIdleTimer()
    clearAnswerReveal()
    stopProgress()
    setPhase('idle')
    setProgress(0)
    setAnswer('')
    setError(null)
    setCopied(false)
    setScreenshot(null)
    setScreenshotSrc('')
    setReferences([])
    setReferencesExpanded(false)
    setOutputTruncated(false)
    setInferenceElapsedMs(null)
    setStreamStatus('')
    setPreviewOpen(false)
    setManualInstruction('')
    setAttachments([])
    setAttachmentError(null)
    setPendingAutoTask(null)
    followStreamingAnswerRef.current = true
  }

  const stopAssist = () => {
    abortRef.current?.abort()
    abortRef.current = null
    activeAssistTaskRef.current = false
    clearAnswerReveal()
    stopProgress()
    setPhase(answer ? 'done' : 'idle')
    setProgress(0)
  }

  const addFiles = async (files: Iterable<File>) => {
    setAttachmentError(null)
    try {
      const next = await filesToAttachments(files, attachments.length)
      setAttachments(prev => [...prev, ...next])
    } catch (err) {
      setAttachmentError(toUserFacingError(err, '附件读取失败'))
    }
  }

  const runAssistWithOcr = async (preparedOcr?: FloatingAssistOcrResult, options: RunAssistOptions = {}) => {
    if (!options.automatic && suppressClickRef.current) return false
    if (activeAssistTaskRef.current || busy) return false
    activeAssistTaskRef.current = true
    setPendingAutoTask(null)
    clearDoneIdleTimer()
    setContextMenuOpen(false)
    setCanvasOpen(true)
    if (revealTimerRef.current != null) {
      window.clearInterval(revealTimerRef.current)
      revealTimerRef.current = null
    }
    setRevealing(false)
    setCopied(false)
    setAnswer('')
    setError(null)
    setScreenshot(null)
    setScreenshotSrc('')
    setReferences([])
    setReferencesExpanded(false)
    setOutputTruncated(false)
    setInferenceElapsedMs(null)
    setStreamStatus('')
    setPreviewOpen(false)
    followStreamingAnswerRef.current = true
    const controller = new AbortController()
    abortRef.current = controller
    try {
      setPhase('receiving')
      startProgress(4, 12, 900)
      await waitForPaint()
      setPhase('capturing')
      startProgress(12, 34, 9000)
      await waitForPaint()
      const ocr = preparedOcr ?? await invoke<FloatingAssistOcrResult>(
        'capture_screen_ocr_for_floating_assist',
        { hideFloatingWindow: false },
      )
      const rawText = ocr.text.trim()
      const detectedSnippets = options.automatic ? options.detection?.snippets.filter(Boolean) ?? [] : []
      const text = detectedSnippets.length > 0 ? detectedSnippets.join('\n') : rawText
      setScreenshot(ocr)
      try {
        setScreenshotSrc(await invoke<string>('read_floating_assist_image_data_url', { path: ocr.screenshot_path }))
      } catch {
        setScreenshotSrc('')
      }
      if (!text) {
        throw new Error('这次截图没有识别到可用文字。请把目标窗口放到前台后再试。')
      }

      setPhase('answering')
      startProgress(35, 92, 60000)
      await waitForPaint()
      const attachmentPrompt = buildAttachmentPrompt(attachments)
      const query = buildFloatingAssistQuery(text)
      const queryWithAttachments = attachmentPrompt ? `${query}\n\n${attachmentPrompt}` : query
      const metadata = {
        source: 'floating_assist',
        screenshot_path: options.automatic ? undefined : ocr.screenshot_path,
        screenshot_width: ocr.width,
        screenshot_height: ocr.height,
        screenshot_source: ocr.screenshot_source,
        app_bundle_id: ocr.app_bundle_id,
        app_name: ocr.app_name,
        window_title: ocr.window_title,
        ocr_text: text,
        trigger: options.automatic ? 'auto_task_detection' : 'screen_recognition',
        auto_task_detection: options.detection
          ? {
            score: options.detection.score,
            reasons: options.detection.reasons,
            fingerprint: options.detection.fingerprint,
            snippets: options.detection.snippets,
            requires_confirmation: options.detection.requiresConfirmation,
          }
          : undefined,
        attachments: buildAttachmentMetadata(attachments),
      }
      const streamCallbacks: RagStreamCallbacks = {
        onStatus: status => {
          setStreamStatus(status.message)
          continueStreamProgress(status)
        },
        onReferences: contexts => {
          setReferences(contexts)
        },
        onDelta: (_delta, accumulated) => {
          setPhase('answering')
          setRevealing(true)
          setAnswer(accumulated)
          setProgress(current => Math.min(94, Math.max(58, current + 1)))
        },
      }
      const result = await runFloatingAssistQuery(
        queryWithAttachments,
        metadata,
        controller.signal,
        streamCallbacks,
      )
      setReferences(result.contexts ?? [])
      setOutputTruncated(Boolean(result.output_truncated))
      setInferenceElapsedMs(result.inference_elapsed_ms ?? result.elapsed_ms ?? null)
      stopProgress()
      setProgress(100)
      setPhase('done')
      setRevealing(false)
      setAnswer(result.answer?.trim() || '本次没有生成咨询输出，请重试。')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return true
      stopProgress()
      setRevealing(false)
      setPhase('error')
      setError(toUserFacingError(err, '暂时无法完成请求，请稍后重试'))
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      activeAssistTaskRef.current = false
    }
    return true
  }

  const runAssist = async () => {
    await runAssistWithOcr()
  }
  runAssistRef.current = runAssist

  useEffect(() => {
    let dispose: (() => void) | null = null
    let cancelled = false
    const consumePendingAction = async () => {
      try {
        const action = await invoke<string | null>('take_pending_floating_assist_action')
        if (!cancelled && action === 'recognize_screen_task') {
          await runAssistRef.current()
        }
      } catch {
        // 浏览器预览环境没有原生悬浮球动作队列。
      }
    }

    void consumePendingAction()
    void listen('floating-assist-action-triggered', () => {
      void consumePendingAction()
    }).then(cleanup => {
      if (cancelled) cleanup()
      else dispose = cleanup
    }).catch(() => {})

    return () => {
      cancelled = true
      dispose?.()
    }
  }, [])

  const showPendingAutoTask = async (ocr: FloatingAssistOcrResult, detection: FloatingAssistTaskDetection) => {
    clearDoneIdleTimer()
    setContextMenuOpen(false)
    setPreviewOpen(false)
    setCanvasOpen(true)
    setPhase('idle')
    setError(null)
    setCopied(false)
    setAnswer('')
    setReferences([])
    setReferencesExpanded(false)
    setOutputTruncated(false)
    setInferenceElapsedMs(null)
    setStreamStatus('')
    setScreenshot(ocr)
    setPendingAutoTask({ ocr, detection })
    try {
      setScreenshotSrc(await invoke<string>('read_floating_assist_image_data_url', { path: ocr.screenshot_path }))
    } catch {
      setScreenshotSrc('')
    }
  }

  const confirmPendingAutoTask = async () => {
    const pending = pendingAutoTask
    if (!pending || busy) return
    setPendingAutoTask(null)
    await runAssistWithOcr(pending.ocr, { automatic: true, detection: pending.detection })
  }

  const dismissPendingAutoTask = () => {
    setPendingAutoTask(null)
    setScreenshot(null)
    setScreenshotSrc('')
    setPreviewOpen(false)
  }

  useEffect(() => {
    if (!autoTaskConfig.enabled) return

    let cancelled = false
    const scanForTask = async () => {
      if (cancelled || autoTaskScanInFlightRef.current) return
      if (
        activeAssistTaskRef.current
        || busy
        || dragRef.current.active
        || dragRef.current.dragging
        || Date.now() - lastUserInteractionAtRef.current < AUTO_TASK_USER_IDLE_GUARD_MS
        || canvasOpen
        || contextMenuOpen
        || previewOpen
        || revealing
        || phase !== 'idle'
        || manualInstruction.trim()
        || attachments.length > 0
      ) {
        return
      }

      autoTaskScanInFlightRef.current = true
      try {
        const ocr = await invoke<FloatingAssistOcrResult>('capture_screen_ocr_for_floating_assist')
        if (cancelled) return
        const detection = detectFloatingAssistTaskFromOcr(ocr.text, {
          requireImWindow: true,
          screenshotSource: ocr.screenshot_source,
          appBundleId: ocr.app_bundle_id,
          appName: ocr.app_name,
          windowTitle: ocr.window_title,
          appTargets: autoTaskConfig.appTargets,
          triggerWords: autoTaskConfig.triggerWords,
        })

        const now = Date.now()
        if (hasSeenAutoTaskFingerprint(seenAutoTasksRef.current, detection.fingerprint)) {
          return
        }

        if (!detection.matched) {
          if (detection.requiresConfirmation) {
            rememberAutoTaskFingerprint(seenAutoTasksRef.current, detection.fingerprint, now)
            await showPendingAutoTask(ocr, detection)
          }
          return
        }
        if (activeAssistTaskRef.current) return

        const started = await runAssistWithOcr(ocr, { automatic: true, detection })
        if (started) {
          rememberAutoTaskFingerprint(seenAutoTasksRef.current, detection.fingerprint, now)
        }
      } catch (err) {
        console.warn('floating assist auto task scan failed', err)
      } finally {
        autoTaskScanInFlightRef.current = false
      }
    }

    const initialTimer = window.setTimeout(() => {
      void scanForTask()
    }, AUTO_TASK_SCAN_INITIAL_DELAY_MS)
    const interval = window.setInterval(() => {
      void scanForTask()
    }, AUTO_TASK_SCAN_INTERVAL_MS)

    return () => {
      cancelled = true
      window.clearTimeout(initialTimer)
      window.clearInterval(interval)
    }
  }, [
    activeModelId,
    apiBaseUrl,
    attachments,
    autoTaskConfig,
    busy,
    canvasOpen,
    contextMenuOpen,
    creationModelConfigs,
    currentUser?.id,
    gatewayApiBaseUrl,
    manualInstruction,
    pendingAutoTask,
    phase,
    previewOpen,
    remoteModelAllowed,
    revealing,
  ])

  const runManualAssist = async () => {
    const instruction = manualInstruction.trim()
    if (!instruction || activeAssistTaskRef.current || busy) return
    activeAssistTaskRef.current = true
    setPendingAutoTask(null)
    clearDoneIdleTimer()
    setContextMenuOpen(false)
    setCanvasOpen(true)
    if (revealTimerRef.current != null) {
      window.clearInterval(revealTimerRef.current)
      revealTimerRef.current = null
    }
    setRevealing(false)
    setCopied(false)
    setAnswer('')
    setError(null)
    setReferences([])
    setReferencesExpanded(false)
    setOutputTruncated(false)
    setInferenceElapsedMs(null)
    setStreamStatus('')
    setPreviewOpen(false)
    followStreamingAnswerRef.current = true
    const controller = new AbortController()
    abortRef.current = controller
    try {
      setPhase('receiving')
      startProgress(8, 18, 900)
      await waitForPaint()
      setPhase('answering')
      startProgress(18, 92, 60000)
      await waitForPaint()
      const attachmentPrompt = buildAttachmentPrompt(attachments)
      const query = buildManualFloatingAssistQuery(instruction, screenshot?.text)
      const queryWithAttachments = attachmentPrompt ? `${query}\n\n${attachmentPrompt}` : query
      const metadata = screenshot ? {
        source: 'floating_assist',
        screenshot_path: screenshot.screenshot_path,
        screenshot_width: screenshot.width,
        screenshot_height: screenshot.height,
        ocr_text: screenshot.text,
        manual_instruction: instruction,
        attachments: buildAttachmentMetadata(attachments),
      } : {
        source: 'floating_assist',
        manual_instruction: instruction,
        attachments: buildAttachmentMetadata(attachments),
      }
      const streamCallbacks: RagStreamCallbacks = {
        onStatus: status => {
          setStreamStatus(status.message)
          continueStreamProgress(status)
        },
        onReferences: contexts => {
          setReferences(contexts)
        },
        onDelta: (_delta, accumulated) => {
          setPhase('answering')
          setRevealing(true)
          setAnswer(accumulated)
          setProgress(current => Math.min(94, Math.max(58, current + 1)))
        },
      }
      const result = await runFloatingAssistQuery(
        queryWithAttachments,
        metadata,
        controller.signal,
        streamCallbacks,
      )
      setReferences(result.contexts ?? [])
      setOutputTruncated(Boolean(result.output_truncated))
      setInferenceElapsedMs(result.inference_elapsed_ms ?? result.elapsed_ms ?? null)
      stopProgress()
      setProgress(100)
      setPhase('done')
      setManualInstruction('')
      setRevealing(false)
      setAnswer(result.answer?.trim() || '本次没有生成咨询输出，请重试。')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      stopProgress()
      setRevealing(false)
      setPhase('error')
      setError(toUserFacingError(err, '暂时无法完成请求，请稍后重试'))
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      activeAssistTaskRef.current = false
    }
  }

  const handleManualSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (manualInputImeGuard.shouldBlockSubmit()) return
    void runManualAssist()
  }

  const copyAnswer = async () => {
    if (!answer) return
    await navigator.clipboard.writeText(answer)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1400)
  }

  const toggleCanvas = () => {
    if (suppressClickRef.current) return
    if (!canvasOpen) resetDoneMascotToIdle()
    setCanvasOpen(value => !value)
  }

  const expandCanvas = () => {
    resetDoneMascotToIdle()
    setCanvasOpen(true)
    setContextMenuOpen(false)
  }

  const handleBallContextMenu = (event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    if (suppressClickRef.current) return
    if (clickTimerRef.current != null) {
      window.clearTimeout(clickTimerRef.current)
      clickTimerRef.current = null
    }
    setContextMenuOpen(value => !value)
  }

  const openMainPanel = () => {
    setContextMenuOpen(false)
    invoke('set_floating_assist_size', {
      width: FLOATING_ASSIST_BALL_SIZE,
      height: FLOATING_ASSIST_BALL_SIZE,
    })
      .catch(() => {})
      .finally(() => {
        invoke('show_main_panel_from_floating_assist').catch(() => {})
      })
  }

  const runFloatingBallAction = (action: FloatingBallAction) => {
    if (action === 'none') return
    if (action === 'open_floating_consult') {
      toggleCanvas()
      return
    }
    if (action === 'open_main_panel') {
      openMainPanel()
      return
    }
    void runAssist()
  }

  const handleBallClick = () => {
    if (suppressClickRef.current) return
    setContextMenuOpen(false)
    if (clickTimerRef.current != null) {
      window.clearTimeout(clickTimerRef.current)
    }
    clickTimerRef.current = window.setTimeout(() => {
      clickTimerRef.current = null
      runFloatingBallAction(interactionSettings.floatingBall.singleClick)
    }, 220)
  }

  const handleManualWheel = useCallback((event: React.WheelEvent<HTMLTextAreaElement>) => {
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

  const hideFloatingAssist = () => {
    setContextMenuOpen(false)
    invoke('set_floating_assist_visible', { enabled: false }).catch(() => {})
  }

  const handleBallDoubleClick = () => {
    if (clickTimerRef.current != null) {
      window.clearTimeout(clickTimerRef.current)
      clickTimerRef.current = null
    }
    runFloatingBallAction(interactionSettings.floatingBall.doubleClick)
  }

  const startDrag = (event: React.PointerEvent<HTMLElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    dragRef.current = {
      active: true,
      dragging: false,
      startX: event.clientX,
      startY: event.clientY,
      pointerId: event.pointerId,
      offsetX: 0,
      offsetY: 0,
      tickId: null,
    }
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // ignore
    }
  }

  const startManualDrag = async () => {
    const drag = dragRef.current
    if (!drag.active || drag.dragging) return
    drag.dragging = true
    drag.active = false
    suppressClickRef.current = true
    if (clickTimerRef.current != null) {
      window.clearTimeout(clickTimerRef.current)
      clickTimerRef.current = null
    }
    try {
      const origin = await invoke<FloatingAssistDragOrigin>('begin_floating_assist_drag')
      if (!drag.dragging) return
      drag.offsetX = origin.offset_x
      drag.offsetY = origin.offset_y
      const tick = () => {
        if (dragUpdateInFlightRef.current) return
        dragUpdateInFlightRef.current = true
        invoke('update_floating_assist_drag', {
          offsetX: drag.offsetX,
          offsetY: drag.offsetY,
        })
          .catch((reason) => {
            console.warn('floating assist drag update failed', reason)
            stopDrag()
          })
          .finally(() => {
            dragUpdateInFlightRef.current = false
          })
      }
      tick()
      drag.tickId = window.setInterval(tick, FLOATING_ASSIST_DRAG_TICK_MS)
    } catch (reason) {
      console.warn('floating assist drag start failed', reason)
      stopDrag()
    }
  }

  const moveDrag = (event: React.PointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag.active || drag.dragging || event.pointerId !== drag.pointerId) return
    if ((event.buttons & 1) !== 1) {
      stopDrag(false)
      return
    }
    const dx = event.clientX - drag.startX
    const dy = event.clientY - drag.startY
    if (!drag.dragging && Math.hypot(dx, dy) < 3) return

    void startManualDrag()
  }

  const endDrag = (event: React.PointerEvent<HTMLElement>) => {
    try {
      event.currentTarget.releasePointerCapture(event.pointerId)
    } catch {
      // ignore
    }
    stopDrag()
  }

  const openReference = (item: RagContext) => {
    const sourceType = item.source_type || item.source || 'capture'
    const referenceType = ['document', 'bake_knowledge', 'operation', 'action'].includes(sourceType)
      ? sourceType
      : item.knowledge_id
        ? 'knowledge'
        : sourceType
    invoke('open_floating_assist_reference', {
      detail: {
        type: referenceType,
        captureId: item.capture_id,
        knowledgeId: item.knowledge_id,
        artifactId: item.artifact_id,
        documentId: item.document_id,
        docKey: item.doc_key,
      },
    }).catch(() => {})
  }

  const visibleReferences = references.filter(item => (item.source_type || item.source) !== 'floating_assist')
  const displayedReferences = referencesExpanded
    ? visibleReferences
    : visibleReferences.slice(0, DEFAULT_VISIBLE_REFERENCE_COUNT)
  const hiddenReferenceCount = visibleReferences.length - DEFAULT_VISIBLE_REFERENCE_COUNT
  const floatingAnswer = useMemo(() => splitFloatingAssistAnswer(answer), [answer])
  const displayedAnswer = floatingAnswer.responseContent
    || (!screenshotSrc ? floatingAnswer.userQuestionUnderstanding : '')
    || (floatingAnswer.userQuestionUnderstanding ? '' : answer)
  const showAnswer = phase === 'answering'
    || (Boolean(displayedAnswer) && (phase === 'done' || phase === 'idle'))
  const ballActionHint = floatingBallActionHint(interactionSettings.floatingBall)

  return (
    <div
      className={`system-floating-assist ${debugBackground ? 'system-floating-assist--debug-bg' : ''} ${hasCanvas ? 'system-floating-assist--open' : ''} ${hasCanvas || contextMenuOpen ? 'system-floating-assist--dismissable' : ''}`}
    >
      <div className="system-floating-assist__dock">
        <button
          className={`system-floating-assist__ball system-floating-assist__ball--${phase}${nativeHovering ? ' system-floating-assist__ball--native-hover' : ''}${ambientAnimating ? ' system-floating-assist__ball--ambient-active' : ''}${developmentMode ? ' system-floating-assist__ball--development' : ''}`}
          type="button"
          onClick={handleBallClick}
          onContextMenu={handleBallContextMenu}
          onDoubleClick={handleBallDoubleClick}
          onPointerEnter={() => setNativeHovering(true)}
          onPointerLeave={() => setNativeHovering(false)}
          onPointerDown={startDrag}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          aria-label={ballActionHint}
          title={ballActionHint}
        >
          {autoTaskConfig.enabled && (
            <span className="system-floating-assist__auto-mark" aria-hidden="true">auto</span>
          )}
          {developmentMode && (
            <span className="system-floating-assist__development-mark">开发模式</span>
          )}
          <span className="system-floating-assist__mascot system-floating-assist__bread-person" aria-hidden="true">
            <span className="system-floating-assist__bread-shadow" />
            <span className="system-floating-assist__bread-arm system-floating-assist__bread-arm--left" />
            <span className="system-floating-assist__bread-arm system-floating-assist__bread-arm--right" />
            <span className="system-floating-assist__bread-leg system-floating-assist__bread-leg--left" />
            <span className="system-floating-assist__bread-leg system-floating-assist__bread-leg--right" />
            <img className="system-floating-assist__bread-body" src={breadRollAsset} alt="" draggable={false} />
            <span className="system-floating-assist__bread-face">
              <span className="system-floating-assist__bread-eye system-floating-assist__bread-eye--left" />
              <span className="system-floating-assist__bread-eye system-floating-assist__bread-eye--right" />
              <span className="system-floating-assist__bread-mouth" />
            </span>
            <span className="system-floating-assist__bread-cheek system-floating-assist__bread-cheek--left" />
            <span className="system-floating-assist__bread-cheek system-floating-assist__bread-cheek--right" />
            <span className="system-floating-assist__task-card">
              <span />
            </span>
            <span className="system-floating-assist__scan-ring system-floating-assist__scan-ring--outer" />
            <span className="system-floating-assist__scan-ring system-floating-assist__scan-ring--inner" />
            <span className="system-floating-assist__thinking-dots" />
            <span className="system-floating-assist__crumbs" />
            <span className="system-floating-assist__sparkles" />
            <span className="system-floating-assist__bread-sweat" />
          </span>
          {floatingBadge && (
            <span
              className={`system-floating-assist__equipped-badge system-floating-assist__equipped-badge--${floatingBadge.palette_key}`}
              title={`已佩戴 ${floatingBadge.name}`}
            >
              <span>{Array.from(floatingBadge.name)[0]}</span>
            </span>
          )}
          <span className="system-floating-assist__ball-badge" aria-hidden="true">
            <span className="system-floating-assist__ball-check" />
            <span className="system-floating-assist__ball-alert" />
          </span>
        </button>
      </div>

      {contextMenuOpen && (
        <div className="system-floating-assist__context-menu" role="menu" onClick={(event) => event.stopPropagation()}>
          <button type="button" role="menuitem" onClick={openMainPanel}>
            <Home size={15} />
            <span>打开主面板</span>
          </button>
          <button type="button" role="menuitem" onClick={runAssist} disabled={busy}>
            <MessageSquare size={15} />
            <span>识别屏幕咨询</span>
          </button>
          <button type="button" role="menuitem" onClick={expandCanvas}>
            <Sparkles size={15} />
            <span>展开咨询面板</span>
          </button>
          <button type="button" role="menuitem" onClick={hideFloatingAssist}>
            <EyeOff size={15} />
            <span>隐藏悬浮球</span>
          </button>
        </div>
      )}

      {hasCanvas && (
        <section className="system-floating-assist__canvas" aria-live="polite">
          <header className="system-floating-assist__canvas-head">
            <div>
              <span>记忆咨询</span>
              <strong>{statusText}</strong>
            </div>
            <div className="system-floating-assist__canvas-actions">
              <button
                className="system-floating-assist__small-btn"
                type="button"
                onClick={copyAnswer}
                disabled={!answer}
              >
                <BreadToolIcon name="copy" size={16} />
                {copied ? '已复制' : '复制'}
              </button>
              {busy && (
                <button
                  className="system-floating-assist__small-btn system-floating-assist__small-btn--danger"
                  type="button"
                  onClick={stopAssist}
                >
                  <BreadToolIcon name="stop" size={15} />
                  中止
                </button>
              )}
              <button
                className="system-floating-assist__small-btn"
                type="button"
                onClick={clearCanvasContent}
                disabled={busy}
              >
                <BreadToolIcon name="clear" size={16} />
                清空
              </button>
              <button
                className="system-floating-assist__small-btn"
                type="button"
                onClick={runAssist}
                disabled={busy}
              >
                <BreadToolIcon name="retry" size={16} />
                重试
              </button>
            </div>
          </header>

          <div
            ref={bodyRef}
            className="system-floating-assist__body"
            onScroll={(event) => {
              if (phase !== 'answering') return
              const body = event.currentTarget
              followStreamingAnswerRef.current =
                body.scrollHeight - body.scrollTop - body.clientHeight <= 48
            }}
          >
            {screenshotSrc && (
              <div className="system-floating-assist__consult-screen">
                <div className="system-floating-assist__consult-title">用户咨询：</div>
                <div className="system-floating-assist__screenshot-wrap">
                  <button
                    className="system-floating-assist__screenshot"
                    type="button"
                    onClick={() => setPreviewOpen(true)}
                    aria-label="查看本次截屏"
                    title="查看本次截屏"
                  >
                    <img src={screenshotSrc} alt="本次截屏缩略图" />
                  </button>
                  <button
                    className="system-floating-assist__screenshot-remove"
                    type="button"
                    onClick={() => {
                      setScreenshot(null)
                      setScreenshotSrc('')
                      setPreviewOpen(false)
                    }}
                    aria-label="移除本次截屏"
                    title="移除本次截屏"
                    disabled={busy}
                  >
                    <X size={13} />
                  </button>
                </div>
                {floatingAnswer.userQuestionUnderstanding && (
                  <div className="system-floating-assist__question-understanding">
                    <MarkdownContent content={floatingAnswer.userQuestionUnderstanding} />
                  </div>
                )}
              </div>
            )}

            {(phase === 'receiving' || phase === 'capturing' || phase === 'answering') && (
              <div className={`system-floating-assist__thinking${hasStreamingAnswer ? ' system-floating-assist__thinking--streaming' : ''}`}>
                <div className="system-floating-assist__thinking-row">
                  <Loader2 size={18} className="system-floating-assist__spin" />
                  <span>{statusText}</span>
                </div>
                <div className="system-floating-assist__progress" aria-hidden="true">
                  <span style={{ width: `${progress}%` }} />
                </div>
              </div>
            )}

            {phase === 'error' && (
              <div className="system-floating-assist__error">
                {error || '悬浮球处理失败，请稍后重试。'}
              </div>
            )}

            {pendingAutoTask && phase === 'idle' && !showAnswer && (
              <div className="system-floating-assist__auto-confirm">
                <strong>发现可能任务</strong>
                <p>当前页面像是文档或资料页，确认后再交给记忆面包咨询。</p>
                {pendingAutoTask.detection.snippets.length > 0 && (
                  <div className="system-floating-assist__auto-confirm-snippets">
                    {pendingAutoTask.detection.snippets.slice(0, 3).map((snippet, index) => (
                      <span key={`${snippet}-${index}`}>{snippet}</span>
                    ))}
                  </div>
                )}
                <div className="system-floating-assist__auto-confirm-actions">
                  <button type="button" onClick={confirmPendingAutoTask} disabled={busy}>
                    咨询
                  </button>
                  <button type="button" onClick={dismissPendingAutoTask} disabled={busy}>
                    忽略
                  </button>
                </div>
              </div>
            )}

            {showAnswer && (
              <div className={`system-floating-assist__answer${phase === 'answering' ? ' system-floating-assist__answer--streaming' : ''}`}>
                <div className="system-floating-assist__output-title">
                  <span>咨询输出{phase === 'answering' ? ' · 正在生成' : ''}</span>
                  {phase === 'done' && inferenceElapsedMs != null && (
                    <small>推理耗时 {formatElapsed(inferenceElapsedMs)}</small>
                  )}
                </div>
                {outputTruncated && (
                  <div className="system-floating-assist__answer-notice">
                    本次回答触达输出长度上限，已返回可用部分。请点击重试生成更完整版本。
                  </div>
                )}
                <MarkdownContent content={displayedAnswer} />
              </div>
            )}
            {phase === 'idle' && !displayedAnswer && !pendingAutoTask && (
              <div className="system-floating-assist__empty">
                <strong>暂无咨询内容</strong>
              </div>
            )}
          </div>

          <form className="system-floating-assist__manual" onSubmit={handleManualSubmit}>
            <div className="system-floating-assist__manual-main">
              <textarea
                value={manualInstruction}
                onChange={(event) => setManualInstruction(event.target.value)}
                onWheel={handleManualWheel}
                onPaste={(event) => {
                  const files = Array.from(event.clipboardData.files || [])
                  if (files.length) void addFiles(files)
                }}
                placeholder={screenshot ? '继续输入你的指令，结合当前界面内容咨询' : '帮我回顾一下上周项目评审的关键结论'}
                rows={2}
                disabled={busy}
                onCompositionStart={manualInputImeGuard.onCompositionStart}
                onCompositionEnd={manualInputImeGuard.onCompositionEnd}
                onBlur={manualInputImeGuard.onBlur}
                onKeyDown={(event) => {
                  if (
                    event.key === 'Enter'
                    && !event.shiftKey
                    && !manualInputImeGuard.isImeEvent(event)
                  ) {
                    event.preventDefault()
                    event.currentTarget.form?.requestSubmit()
                  }
                }}
              />
              {attachments.length > 0 && (
                <div className="system-floating-assist__attachments">
                  {attachments.map(item => (
                    <span className="system-floating-assist__attachment" key={item.id}>
                      <BreadToolIcon name="attach" size={12} framed={false} />
                      <span>{item.name}</span>
                      <small>{formatAttachmentSize(item.size)}</small>
                      <button
                        type="button"
                        onClick={() => setAttachments(prev => prev.filter(existing => existing.id !== item.id))}
                        disabled={busy}
                        aria-label={`移除 ${item.name}`}
                      >
                        <X size={12} />
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {attachmentError && <div className="system-floating-assist__attachment-error">{attachmentError}</div>}
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/*,.pdf,.txt,.md,.doc,.docx"
              style={{ display: 'none' }}
              onChange={(event) => {
                if (event.target.files) void addFiles(event.target.files)
                event.currentTarget.value = ''
              }}
            />
            <button
              className="system-floating-assist__manual-attach"
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy}
              aria-label="上传附件"
              title="上传附件"
            >
              <BreadToolIcon name="attach" size={16} framed={false} />
            </button>
            <button
              className="system-floating-assist__manual-submit"
              type="submit"
              disabled={busy || !manualInstruction.trim()}
              aria-label="发送手工咨询"
              title="发送手工咨询"
            >
              {busy ? <Loader2 size={15} className="system-floating-assist__spin" /> : <BreadToolIcon name="send" size={16} framed={false} />}
            </button>
          </form>

          {visibleReferences.length > 0 && (
            <div className="system-floating-assist__refs">
              <div className="system-floating-assist__refs-title">参考资料（{visibleReferences.length}）</div>
              {displayedReferences.map((item, index) => {
                const referenceType = referenceTypeMeta(item)
                return (
                  <button
                    className="system-floating-assist__ref"
                    type="button"
                    onClick={() => openReference(item)}
                    key={`${item.doc_key || item.capture_id}-${index}`}
                  >
                    <span className={`system-floating-assist__ref-type system-floating-assist__ref-type--${referenceType.kind}`}>
                      {referenceType.label}
                    </span>
                    <span className="system-floating-assist__ref-index">R{index + 1}</span>
                    <strong>{referenceTitle(item)}</strong>
                  </button>
                )
              })}
              {hiddenReferenceCount > 0 && (
                <button
                  className="system-floating-assist__refs-toggle"
                  type="button"
                  aria-expanded={referencesExpanded}
                  onClick={() => setReferencesExpanded(expanded => !expanded)}
                >
                  {referencesExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                  {referencesExpanded ? '收起' : `展开更多（${hiddenReferenceCount}）`}
                </button>
              )}
            </div>
          )}

        </section>
      )}

      {previewOpen && screenshotSrc && (
        <div className="system-floating-assist__preview" onClick={() => setPreviewOpen(false)}>
          <img src={screenshotSrc} alt="本次截屏预览" />
        </div>
      )}
    </div>
  )
}

const referenceTitle = (item: RagContext) =>
  item.title || item.overview || item.summary || item.win_title || item.app_name || item.text?.slice(0, 48) || '参考资料'

const formatElapsed = (elapsedMs: number) => {
  if (elapsedMs < 1000) return `${Math.max(1, Math.round(elapsedMs))} ms`
  return `${(elapsedMs / 1000).toFixed(elapsedMs < 10_000 ? 1 : 0)} 秒`
}

type FloatingReferenceType = 'knowledge' | 'document' | 'operation' | 'timeline'

const referenceTypeMeta = (item: RagContext): { kind: FloatingReferenceType; label: string } => {
  const sourceType = item.source_type || item.source
  if (sourceType === 'bake_knowledge') return { kind: 'knowledge', label: '知识' }
  if (sourceType === 'document') return { kind: 'document', label: '文档' }
  if (sourceType === 'operation' || sourceType === 'action') return { kind: 'operation', label: '操作' }
  return { kind: 'timeline', label: '时间线' }
}

const MarkdownContent = ({ content }: { content: string }) => {
  const inlineComponents = {
    p: ({ children }: any) => <>{children}</>,
  }

  return (
    <>
      {parseMarkdownBlocks(content).map((block, index) => {
        if (block.type === 'markdown') {
          return <ReactMarkdown key={`markdown-${index}`}>{block.content}</ReactMarkdown>
        }

        return (
          <div className="system-floating-assist__table-wrap" key={`table-${index}`}>
            <table>
              <thead>
                <tr>
                  {block.headers.map((header, cellIndex) => (
                    <th key={cellIndex} style={{ textAlign: block.alignments[cellIndex] || 'left' }}>
                      <ReactMarkdown components={inlineComponents}>{header}</ReactMarkdown>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {block.headers.map((_, cellIndex) => (
                      <td key={cellIndex} style={{ textAlign: block.alignments[cellIndex] || 'left' }}>
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

export default SystemFloatingAssist
