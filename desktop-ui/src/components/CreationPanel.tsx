import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { AtSign, Bot, Check, ChevronDown, ChevronLeft, ChevronRight, CloudOff, CloudUpload, Copy, ExternalLink, Eye, FileCode2, FileText, FolderOpen, Globe2, Image, Library, Lightbulb, Loader2, Maximize2, MessageSquarePlus, Minimize2, PackageCheck, PackagePlus, Paperclip, Pencil, Plus, Search, Send, Sparkles, Square, Store, Trash2, Upload, Wrench, X, Zap } from 'lucide-react'
import { serviceEnvironmentHeaders, useAppStore } from '../store/useAppStore'
import type {
  CreationAgentEvent,
  CreationBrainstormState,
  CreationChatMessage,
  CreationDataReferenceItem,
  CreationMode,
  CreationReferenceItem,
  CreationReferencePreview,
} from '../store/useAppStore'
import { fetchWithLocalhostFallback } from '../hooks/useApi'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { MentionHighlightTextarea } from './MentionHighlightField'
import { getUserDisplayName } from '../utils/accountDisplay'
import { fetchBillingBalance } from '../utils/authApi'
import { createOptionalCloudRequestSignal, optionalCloudIsReachable } from '../utils/optionalCloud'
import { CREATION_MODEL_DEFS, LOCAL_CREATION_MODEL_ID, REMOTE_CREATION_MODEL_ID, canUseRemoteCreationModel, getEffectiveCreationModelId, getModelDisplayName } from '../utils/modelSelection'
import { buildAttachmentMetadata, buildAttachmentPrompt, filesToAttachments, formatAttachmentSize, type UserAttachment } from '../utils/attachments'
import { toLocalApiError, toUserFacingError } from '../utils/userFacingError'
import { consumeGatewayChatStream, fetchGatewayChat, readGatewayChatError } from '../utils/gatewayChatStream'
import {
  buildCreationSkillInstruction,
  categoryPathFor,
  CREATION_SKILL_AGENT_OPTIONS,
  CREATION_SKILL_TOOL_OPTIONS,
  creationSkillCategoryOptions,
  codexSkillPackageFiles,
  deleteLocalCreationSkill,
  fetchCreationSkillCategories,
  fetchCreationSkillMarketDetail,
  importAgentSkillPackage,
  importAgentSkillZip,
  listLocalCreationSkills,
  marketCreationSkillToLocalInput,
  matchCreationSkills,
  publishCreationSkill,
  resolveCreationSkillDependencies,
  resolveExecutionSkills,
  saveLocalCreationSkill,
  searchCreationSkillMarket,
  skillFileText,
  type CreationSkillMarketItem,
  type CreationSkillSource,
  type LocalCreationSkill,
  type MatchedCreationSkill,
} from '../utils/creationSkills'
import { OFFLINE_CREATION_SKILL_CATEGORIES } from '../data/creationSkillCategories'
import ModelSelect from './ModelSelect'
import CreationSkillEditor from './CreationSkillEditor'
import CreationSkillCategoryCombobox from './CreationSkillCategoryCombobox'
import CreationSkillDetail, {
  localSkillDetail,
  marketSkillDetail,
  type CreationSkillDetailData,
} from './CreationSkillDetail'
import CreationToolsPanel from './CreationToolsPanel'
import CreationSelectionToolbar from './creation-selection/CreationSelectionToolbar'
import CreationInlineBrainstormCard from './creation-selection/CreationInlineBrainstormCard'
import {
  inlineEditActionLabel,
  resolveCreationSelection,
  sha256Hex,
  verifyInlineEditResponse,
  type CreationInlineEditAction,
  type CreationInlineEditCapabilities,
  type CreationSelectionSnapshot,
  type InlineEditResponse,
} from './creation-selection/creationInlineEdit'
import { CreationDiagramCode, CreationDiagramPre } from './CreationDiagram'
import { HistoryPagination, HistorySearch } from './HistoryBrowserControls'
import TutorialLink, { TUTORIAL_URLS } from './TutorialLink'
import {
  creationToolResultLimits,
  enabledCreationToolIds,
  loadCreationTools,
  saveCreationTools,
  setCreationToolEnabled,
  setCreationToolInstalled,
  setCreationToolResultLimit,
  type CreationToolId,
} from '../utils/creationTools'
import type { DataSnapshot, DataSource } from '../types'
import './CreationPanel.css'

interface CreationPanelProps {
  className?: string
  active?: boolean
}

type ReferenceItem = CreationReferenceItem
type ReferencePreview = CreationReferencePreview
type BottomTab = 'reference' | 'data' | 'config'
type FullscreenPanel = 'document' | 'reference' | 'data' | null

const CREATION_MODE_OPTIONS = [
  {
    id: 'direct',
    name: '直出模式',
    description: '直接规划并生成完整内容，适合方向明确的需求',
  },
  {
    id: 'brainstorm',
    name: '脑暴模式',
    description: '逐步确认方向与细节，形成创作简报后再生成',
  },
] as const

type ReferenceGroup<T> = {
  id: string
  title: string
  toolName: string
  query: string
  items: T[]
  legacy?: boolean
}
interface CreationHistoryItem {
  id: number
  prompt: string
  timestamp: string
  preview: string
  fullContent: string
  docType: string
  audience: string
  references: CreationReferenceItem[]
  dataReferences: CreationDataReferenceItem[]
  sessionId?: string | null
  rootRequest: string
  conversation: CreationChatMessage[]
  agentEvents: CreationAgentEvent[]
  revisionNo: number
  editOperation: string
  documentPatch: Record<string, unknown> | null
  model?: string | null
  latencyMs?: number | null
  evidence: CreationEvidenceItem[]
  /** 记录来源：creation 手动创作（默认）/ scheduled_task 定时任务执行 */
  sourceKind: 'creation' | 'scheduled_task'
  lifecycleStatus: 'running' | 'completed' | 'failed' | 'cancelled'
  creationMode: CreationMode
  creationBrief: CreationBrainstormState | null
  brainstormRevision: number | null
  progressEpoch: number
}
interface CreationEvidenceItem {
  id: string
  source_url: string
  page_title: string
  captured_at: number
  image_url: string
  validation_status: string
  validation?: Record<string, unknown>
}

interface InlineBrainstormSession {
  sessionId: string
  rootRequest: string
  anchorMessageId: string
  selection: CreationSelectionSnapshot
  state: CreationBrainstormState | null
  loading: boolean
  error: string
  selectedOptionIds: string[]
  customSelected: boolean
  customAnswer: string
  continuationDirectionId: string
  customDirection: string
  applied: boolean
}
interface BrowserPreviewItem {
  id: string
  source_id: number
  title: string
  image_url: string
  status?: string
  browser?: string | null
  interaction_mode?: string | null
  focus_policy?: string | null
  focus_takeover_count?: number
}
interface BrowserLiveJob {
  browser_job_id: string
  url: string
  title: string
  status: string
  stage: string
  updated_at: number
  has_preview: boolean
  preview_revision: number
}
type MarkdownBlock =
  | { type: 'markdown'; content: string; startLine: number; endLine: number; startOffset: number; endOffset: number }
  | { type: 'table'; headers: string[]; alignments: Array<'left' | 'center' | 'right'>; rows: string[][]; startLine: number; endLine: number; startOffset: number; endOffset: number }
interface DocumentChange {
  changeType: 'added' | 'modified' | 'deleted'
  sectionTitle: string
  startLine: number | null
  endLine: number | null
  summary: string
}
interface AgentPhaseResult {
  events: CreationAgentEvent[]
  continuation: Record<string, unknown> | null
  modelMessages: Array<{ role: string; content: string }> | null
  modelRequestId: string | null
  completed: boolean
  pausedForConfirmation: boolean
  document: string
  sessionId: string | null
  runId: string | null
}

const defaultPrompt = '请生成一份“数据治理平台建设方案”，参考历史项目方案、知识库和操作手册，风格正式，包含总体架构、功能设计、实施计划和后续核验清单。'
const HISTORY_PAGE_SIZE = 20
const SKILL_MARKET_PAGE_SIZE = 18
const MAX_CONVERSATION_MESSAGES = 60
const BROWSER_CRAWLER_PREFERENCE_KEY = 'memory-bread_creation_browser_crawler_enabled'
const INLINE_EDIT_NODE_KINDS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote']
const CONTINUE_DOCUMENT_FROM_BRAINSTORM_PROMPT = '请基于最新脑暴结论继续完善当前文档，将新增决定落实到相关章节，并保持其他已确认内容一致。'

const inlineEditUserInstruction = (
  action: CreationInlineEditAction,
  selectedText: string,
  customPrompt = '',
) => {
  const requirement = {
    brainstorm: '按本轮已确认的局部脑暴结论改写所选内容，不超出事实与选区边界',
    polish: '改善所选内容的措辞、语气和连贯性，不新增事实',
    expand: '基于已有上下文扩充所选内容，补充解释、过渡和事实支持',
    elaborate: '细化所选内容，补齐对象、条件、步骤、边界、风险或验收维度',
  }[action]
  const normalizedSelection = selectedText.trim().replace(/\s+/g, ' ')
  const excerpt = normalizedSelection.length > 600
    ? `${normalizedSelection.slice(0, 600)}…`
    : normalizedSelection
  return [
    `${inlineEditActionLabel(action)}要求：${requirement}`,
    excerpt ? `选取内容：${excerpt}` : '',
    customPrompt.trim()
      ? `${action === 'brainstorm' ? '已确认脑暴结论' : '补充要求'}：${customPrompt.trim()}`
      : '',
  ].filter(Boolean).join('\n')
}

const intentOperationForRun = (
  events: CreationAgentEvent[],
  runId?: string | null,
) => {
  const intent = [...events]
    .reverse()
    .find(event => (
      event.type === 'intent.interpreted'
      && (!runId || event.run_id === runId)
    ))
  const operation = String(intent?.data?.operation || '').trim()
  return operation || null
}

const createCreationSessionId = () =>
  `creation-${Date.now()}-${Math.random().toString(16).slice(2)}`

const formatCreationMessageTimestamp = (createdAt: number, now = Date.now()) => {
  const date = new Date(createdAt)
  if (!Number.isFinite(date.getTime())) return null

  const pad = (value: number) => String(value).padStart(2, '0')
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  const full = `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${time}`
  const today = new Date(now)
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1)
  const isSameDate = (left: Date, right: Date) => (
    left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate()
  )

  let label = full
  if (isSameDate(date, today)) {
    label = time
  } else if (isSameDate(date, yesterday)) {
    label = `昨天 ${time}`
  } else if (date.getFullYear() === today.getFullYear()) {
    label = `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
  }

  return { label, full, iso: date.toISOString() }
}

const stripInternalCreationMarkers = (content: string) => content.replace(
  /\\?<!--\s*\/?memorybread:data-risks(?::[a-f0-9]+)?\s*-->/gi,
  '',
)

const sanitizeGeneratedContent = (content: string) =>
  content.replace(/<a\s+(?:id|name)=["'][^"']+["']\s*>\s*<\/a>/gi, '')

const retainConversationContext = (messages: CreationChatMessage[]) => {
  if (messages.length <= MAX_CONVERSATION_MESSAGES) return messages
  return [...messages.slice(0, 4), ...messages.slice(-(MAX_CONVERSATION_MESSAGES - 4))]
}

const isRunTerminalEvent = (event: CreationAgentEvent) => (
  ['run.completed', 'run.failed', 'run.cancelled'].includes(event.type)
)

const terminalEventForLatestRun = (events: CreationAgentEvent[]) => {
  const latestRunId = [...events].reverse().find(event => event.run_id)?.run_id
  return [...events].reverse().find(event => (
    isRunTerminalEvent(event) && (!latestRunId || event.run_id === latestRunId)
  ))
}

type CreationTimelineItem =
  | { kind: 'message'; key: string; message: CreationChatMessage }
  | { kind: 'trace'; key: string; events: CreationAgentEvent[] }

const groupAgentEventsByRun = (events: CreationAgentEvent[]) => {
  const groups: Array<{ runId: string; events: CreationAgentEvent[] }> = []
  const groupByRunId = new Map<string, { runId: string; events: CreationAgentEvent[] }>()

  events.forEach((event) => {
    const runId = event.run_id || 'current'
    const existing = groupByRunId.get(runId)
    if (existing) {
      existing.events.push(event)
      return
    }
    const group = { runId, events: [event] }
    groupByRunId.set(runId, group)
    groups.push(group)
  })

  return groups
}

const closeInterruptedHistoricalRuns = (
  events: CreationAgentEvent[],
  fallbackSessionId: string,
) => {
  const restored = [...events]
  groupAgentEventsByRun(events).forEach(({ runId, events: runEvents }) => {
    if (runEvents.some(isRunTerminalEvent)) return
    const hasUnfinishedLifecycle = runEvents.some(event => (
      ['running', 'waiting'].includes(event.status)
      || ['run.started', 'run.paused', 'phase.started', 'thinking.started'].includes(event.type)
    ))
    if (!hasUnfinishedLifecycle) return

    const now = Date.now()
    const latestGoal = [...runEvents].reverse().find(event => event.goal)?.goal
    restored.push({
      schema_version: 'creation.agent.v1',
      event_id: `history-interrupted-${runId}-${now}`,
      session_id: runEvents[runEvents.length - 1]?.session_id || fallbackSessionId,
      run_id: runId,
      sequence: Math.max(0, ...runEvents.map(event => Number(event.sequence) || 0)) + 1,
      timestamp: now,
      type: 'run.failed',
      status: 'failed',
      actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
      summary: '该历史记录未包含完成事件，已按中断状态展示',
      goal: latestGoal
        ? { ...latestGoal, status: 'failed', outcome: '本轮创作未完成' }
        : undefined,
      environment_patch: {},
      data: { error_code: 'HISTORICAL_RUN_INTERRUPTED', retryable: true },
    })
  })
  return restored
}

const isLegacyMainAgentControlStart = (event: CreationAgentEvent) => (
  event.type === 'agent.started'
  && event.actor?.id === 'creation_main_agent'
  && /^(?:创作主? Agent|创作 Agent) 开始执行$/.test(String(event.summary || '').trim())
)

const collapseAgentLifecycleEvents = (events: CreationAgentEvent[]) => {
  const startTypeForTerminal: Record<string, string> = {
    'agent.completed': 'agent.started',
    'agent.failed': 'agent.started',
    'tool.completed': 'tool.started',
    'tool.failed': 'tool.started',
    'skill.completed': 'skill.started',
    'browser.preview.completed': 'browser.preview.started',
  }
  const visible: CreationAgentEvent[] = []

  events.forEach((event) => {
    // 旧版本曾把 route / plan 内部控制阶段记录为普通 Agent 启动步骤。
    // 这些事件没有独立动作含义；新后端已不再生成，历史轨迹在此兼容清理。
    if (isLegacyMainAgentControlStart(event)) return

    const startType = startTypeForTerminal[event.type]
    if (startType) {
      let startIndex = -1
      for (let index = visible.length - 1; index >= 0; index -= 1) {
        const candidate = visible[index]
        if (
          candidate.type === startType
          && candidate.run_id === event.run_id
          && candidate.actor?.id === event.actor?.id
        ) {
          startIndex = index
          break
        }
      }
      if (startIndex >= 0) visible.splice(startIndex, 1)
    }
    visible.push(event)
  })

  return visible
}

const groupConsecutiveAgentEvents = (events: CreationAgentEvent[]) => {
  const groups: Array<{ key: string; events: CreationAgentEvent[] }> = []

  events.forEach((event) => {
    const previous = groups[groups.length - 1]
    const previousEvent = previous?.events[previous.events.length - 1]
    const sameAgent = (
      event.actor?.kind === 'agent'
      && previousEvent?.actor?.kind === 'agent'
      && event.actor?.id === previousEvent.actor?.id
    )

    if (sameAgent) {
      previous.events.push(event)
      return
    }

    groups.push({
      key: event.event_id || `${event.run_id}-${event.sequence}`,
      events: [event],
    })
  })

  return groups
}

type AgentEventGroup = { key: string; events: CreationAgentEvent[] }

type TraceThinkingSegment = {
  kind: 'thinking'
  key: string
  stage: string
  status: 'running' | 'completed'
  reasoning: string
  durationMs: number | null
  startedAt: number | null
  innerEvents: CreationAgentEvent[]
}

type TraceStepSegment = {
  kind: 'step'
  group: AgentEventGroup
}

type TracePhaseSegment = {
  kind: 'phase'
  key: string
  phaseId: string
  title: string
  phaseKind: string
  status: 'running' | 'completed'
  startedAt: number | null
  durationMs: number | null
  segments: TraceSegment[]
}

type TraceSegment = TraceThinkingSegment | TraceStepSegment | TracePhaseSegment

/** 行标题里属于次级结果的开头标记，拆分后用灰色弱化展示 */
const HEADLINE_SUB_MARKERS = ['，召回', '，获得', '，并把结果写回']

const splitHeadline = (text: string): { main: string; sub: string | null } => {
  let splitIndex = -1
  HEADLINE_SUB_MARKERS.forEach((marker) => {
    const index = text.indexOf(marker)
    if (index >= 0 && (splitIndex === -1 || index < splitIndex)) splitIndex = index
  })
  if (splitIndex < 0) return { main: text, sub: null }
  return { main: text.slice(0, splitIndex), sub: text.slice(splitIndex + 1) }
}

/**
 * 解析思考事件的阶段：优先 data.stage；早期持久化记录未保存 data 时，
 * 从“深度思考（中/完成）：阶段标签”的 summary 反推，保证历史记录仍能展示阶段。
 */
const thinkingStageOfEvent = (event: CreationAgentEvent): string => {
  const raw = String(event.data?.stage || '').trim()
  if (raw) return raw
  const label = String(event.summary || '').split('：').slice(1).join('：').trim()
  if (!label) return ''
  const found = Object.entries(CREATION_THINKING_STAGE_LABELS)
    .find(([, value]) => value === label)
  return found ? found[0] : ''
}

/**
 * 把轨迹切成“深度思考块 + 动作块”：thinking.started 到
 * thinking.completed 之间的事件归入思考块；没有思考事件的历史轨迹
 * 全部落到动作块，保持向后兼容。
 */
const segmentCoreEvents = (collapsed: CreationAgentEvent[]): TraceSegment[] => {
  const segments: TraceSegment[] = []
  let openThinking: TraceThinkingSegment | null = null
  let stepBuffer: CreationAgentEvent[] = []

  const flushSteps = () => {
    if (!stepBuffer.length) return
    groupConsecutiveAgentEvents(stepBuffer).forEach((group) => {
      segments.push({ kind: 'step', group })
    })
    stepBuffer = []
  }

  const closeOpenThinking = () => {
    if (!openThinking) return
    segments.push(openThinking)
    openThinking = null
  }

  collapsed.forEach((event) => {
    if (event.type === 'thinking.started') {
      flushSteps()
      closeOpenThinking()
      openThinking = {
        kind: 'thinking',
        key: event.event_id || `thinking-${event.run_id}-${event.sequence}`,
        stage: thinkingStageOfEvent(event),
        status: 'running',
        reasoning: '',
        durationMs: null,
        startedAt: Number.isFinite(event.timestamp) ? event.timestamp : null,
        innerEvents: [],
      }
      return
    }
    if (event.type === 'thinking.completed') {
      const stage = thinkingStageOfEvent(event)
      const reasoning = String(event.data?.reasoning || '').trim()
      const matchesOpen = openThinking
        && (!openThinking.stage || !stage || openThinking.stage === stage)
      if (matchesOpen && openThinking) {
        openThinking.status = 'completed'
        openThinking.reasoning = reasoning
        openThinking.durationMs = (
          openThinking.startedAt != null && Number.isFinite(event.timestamp)
            ? Math.max(0, event.timestamp - openThinking.startedAt)
            : null
        )
        segments.push(openThinking)
        openThinking = null
        return
      }
      // 恢复场景可能只有 completed 没有 started，补一个独立已完成思考块。
      closeOpenThinking()
      flushSteps()
      segments.push({
        kind: 'thinking',
        key: event.event_id || `thinking-${event.run_id}-${event.sequence}`,
        stage,
        status: 'completed',
        reasoning,
        durationMs: null,
        startedAt: null,
        innerEvents: [],
      })
      return
    }
    if (openThinking) {
      openThinking.innerEvents.push(event)
      return
    }
    stepBuffer.push(event)
  })

  closeOpenThinking()
  flushSteps()
  return segments
}

/**
 * 执行过程三层结构的最外层：phase.started/completed 之间的事件归入阶段块，
 * 阶段内部复用思考/动作分段。没有 phase 事件的旧记录保持平铺，向后兼容。
 */
const segmentAgentTrace = (events: CreationAgentEvent[]): TraceSegment[] => {
  const collapsed = collapseAgentLifecycleEvents(events)
  if (!collapsed.some(event => event.type === 'phase.started')) {
    return segmentCoreEvents(collapsed)
  }

  const segments: TraceSegment[] = []
  let openPhase: TracePhaseSegment | null = null
  let phaseBuffer: CreationAgentEvent[] = []
  let outerBuffer: CreationAgentEvent[] = []

  const flushOuter = () => {
    if (!outerBuffer.length) return
    segmentCoreEvents(outerBuffer).forEach(segment => segments.push(segment))
    outerBuffer = []
  }

  const closeOpenPhase = (push: boolean) => {
    if (!openPhase) return
    openPhase.segments = segmentCoreEvents(phaseBuffer)
    phaseBuffer = []
    if (push) segments.push(openPhase)
    openPhase = null
  }

  collapsed.forEach((event) => {
    if (event.type === 'phase.started') {
      flushOuter()
      closeOpenPhase(true)
      openPhase = {
        kind: 'phase',
        key: event.event_id || `phase-${event.run_id}-${event.sequence}`,
        phaseId: String(event.data?.phase_id || ''),
        title: String(event.data?.phase_title || event.summary || '执行阶段').trim(),
        phaseKind: String(event.data?.phase_kind || 'plan_step'),
        status: 'running',
        startedAt: Number.isFinite(event.timestamp) ? event.timestamp : null,
        durationMs: null,
        segments: [],
      }
      return
    }
    if (event.type === 'phase.completed') {
      const phaseId = String(event.data?.phase_id || '')
      if (openPhase && (!phaseId || !openPhase.phaseId || phaseId === openPhase.phaseId)) {
        openPhase.status = 'completed'
        openPhase.durationMs = (
          openPhase.startedAt != null && Number.isFinite(event.timestamp)
            ? Math.max(0, event.timestamp - openPhase.startedAt)
            : null
        )
        closeOpenPhase(true)
      }
      return
    }
    if (openPhase) {
      phaseBuffer.push(event)
      return
    }
    outerBuffer.push(event)
  })

  flushOuter()
  closeOpenPhase(true)
  return segments
}

const buildCreationTimeline = (
  conversation: CreationChatMessage[],
  events: CreationAgentEvent[],
): CreationTimelineItem[] => {
  const runs = groupAgentEventsByRun(events)
  const runsById = new Map(runs.map(run => [run.runId, run]))
  const claimedRunIds = new Set<string>()
  const timeline: CreationTimelineItem[] = []

  const claimRun = (runId: string | undefined, claimed: typeof runs) => {
    if (!runId || claimedRunIds.has(runId)) return
    const run = runsById.get(runId)
    if (!run) return
    claimedRunIds.add(runId)
    claimed.push(run)
  }

  conversation.forEach((message, messageIndex) => {
    timeline.push({ kind: 'message', key: `message-${message.id}`, message })
    if (message.role !== 'user') return

    const instructionRuns: typeof runs = []
    ;(message.runIds || []).forEach(runId => claimRun(runId, instructionRuns))
    claimRun(message.runId, instructionRuns)

    for (let index = messageIndex + 1; index < conversation.length; index += 1) {
      const nextMessage = conversation[index]
      if (nextMessage.role === 'user') break
      claimRun(nextMessage.runId, instructionRuns)
    }

    if (!instructionRuns.length) {
      const nextUnclaimedRun = runs.find(run => !claimedRunIds.has(run.runId))
      claimRun(nextUnclaimedRun?.runId, instructionRuns)
    }

    if (instructionRuns.length) {
      timeline.push({
        kind: 'trace',
        key: `instruction-trace-${message.id}`,
        events: instructionRuns.flatMap(run => run.events),
      })
    }
  })

  runs.forEach((run, index) => {
    if (claimedRunIds.has(run.runId)) return
    timeline.push({
      kind: 'trace',
      key: `unclaimed-trace-${run.runId}-${index}`,
      events: run.events,
    })
  })

  return timeline
}

const readApiErrorMessage = async (response: Response, fallback: string) => {
  try {
    const text = await response.text()
    if (!text.trim()) return fallback

    try {
      const data = JSON.parse(text)
      if (typeof data === 'string') return data
      if (data?.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
      if (data?.message) return data.message
      if (data?.error?.message) return data.error.message
      if (data?.error?.code) return data.error.code
      if (data?.error) return typeof data.error === 'string' ? data.error : JSON.stringify(data.error)
    } catch {
      return text
    }

    return text
  } catch {
    return fallback
  }
}

const normalizeLatencyMs = (value: unknown): number | null => {
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null
}

const formatInferenceLatency = (latencyMs?: number | null) => {
  if (latencyMs == null) return '未记录'
  if (latencyMs < 1000) return `${latencyMs} ms`
  return `${(latencyMs / 1000).toFixed(latencyMs < 10_000 ? 1 : 0)} 秒`
}

const normalizeOptionalNumber = (value: unknown): number | undefined => {
  if (value == null || value === '') return undefined
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) ? numeric : undefined
}

const normalizeStringList = (value: unknown): string[] | undefined => {
  if (!Array.isArray(value)) return undefined
  return value.map(item => String(item))
}

const normalizeReferenceItems = (value: unknown): CreationReferenceItem[] => {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): CreationReferenceItem[] => {
    if (!item || typeof item !== 'object') return []
    const source = item as Record<string, unknown>
    const id = Number(source.id)
    if (!Number.isFinite(id) || id <= 0) return []
    return [{
      id,
      title: String(source.title || '本地参考资料'),
      doc_type: String(source.doc_type || ''),
      final_weight: Number(source.final_weight || 0),
      relevance_score: Number(source.relevance_score || 0),
      quality_score: Number(source.quality_score || 0),
      completeness_score: Number(source.completeness_score || 0),
      usage_score: Number(source.usage_score || 0),
      format_score: Number(source.format_score || 0),
      freshness_score: Number(source.freshness_score || 0),
      usage_count: Number(source.usage_count || 0),
      reason: String(source.reason || ''),
      retrieval_tier: source.retrieval_tier != null ? String(source.retrieval_tier) : undefined,
      retrieval_paths: normalizeStringList(source.retrieval_paths),
      matched_keywords: normalizeStringList(source.matched_keywords),
      matched_entities: normalizeStringList(source.matched_entities),
      lexical_score: normalizeOptionalNumber(source.lexical_score),
      semantic_score: normalizeOptionalNumber(source.semantic_score),
      entity_score: normalizeOptionalNumber(source.entity_score),
      retrieval_mode: source.retrieval_mode != null ? String(source.retrieval_mode) : undefined,
      primary_target: source.primary_target != null ? String(source.primary_target) : undefined,
      matched_components: normalizeStringList(source.matched_components),
      matched_relations: normalizeStringList(source.matched_relations),
      relation_score: normalizeOptionalNumber(source.relation_score),
      intent_mode: source.intent_mode != null ? String(source.intent_mode) : undefined,
      matched_concepts: normalizeStringList(source.matched_concepts),
      primary_target_score: normalizeOptionalNumber(source.primary_target_score),
      coverage: normalizeOptionalNumber(source.coverage),
      relation_coverage: normalizeOptionalNumber(source.relation_coverage),
      summary: String(source.summary || ''),
      source_url: source.source_url ? String(source.source_url) : undefined,
      source_type: source.source_type != null ? String(source.source_type) : undefined,
      source_id: source.source_id != null ? Number(source.source_id) : undefined,
      skill_step_title: source.skill_step_title ? String(source.skill_step_title) : undefined,
      refresh_status: source.refresh_status ? String(source.refresh_status) : undefined,
      refresh_completeness: source.refresh_completeness ? String(source.refresh_completeness) : undefined,
      refresh_collected_at: source.refresh_collected_at != null ? Number(source.refresh_collected_at) : undefined,
      refresh_truncated: source.refresh_truncated === true,
    }]
  })
}

const mergeReferenceItems = (
  ...referenceGroups: CreationReferenceItem[][]
): CreationReferenceItem[] => {
  const referencesByIdentity = new Map<string, CreationReferenceItem>()
  referenceGroups.forEach(references => {
    references.forEach(reference => {
      const identity = `${reference.source_type || 'document'}:${reference.source_id ?? reference.id}`
      referencesByIdentity.set(identity, reference)
    })
  })
  return [...referencesByIdentity.values()]
}

const referenceGroupId = (event: CreationAgentEvent) => `reference-group-${event.event_id || `${event.run_id}-${event.sequence}`}`
const dataGroupId = (event: CreationAgentEvent) => `data-group-${event.event_id || `${event.run_id}-${event.sequence}`}`

const referenceGroupsFromEvents = (
  events: CreationAgentEvent[],
  fallback: CreationReferenceItem[],
): ReferenceGroup<CreationReferenceItem>[] => {
  const groups = events.flatMap((event): ReferenceGroup<CreationReferenceItem>[] => {
    if (event.type !== 'tool.completed' || event.actor?.id !== 'memory_search') return []
    const items = normalizeReferenceItems(event.environment_patch?.references)
    if (!items.length) return []
    return [{
      id: referenceGroupId(event),
      title: String(event.data?.skill_step_title || '本地资料检索'),
      toolName: displayCreationToolNames(event.actor.name || '记忆搜索 Tool'),
      query: String(event.data?.query || ''),
      items,
    }]
  })
  if (groups.length || !fallback.length) return groups
  return [{
    id: 'reference-group-history-summary',
    title: '历史参考资料',
    toolName: '历史汇总',
    query: '',
    items: fallback,
    legacy: true,
  }]
}

const dataGroupsFromEvents = (
  events: CreationAgentEvent[],
  fallback: CreationDataReferenceItem[],
): ReferenceGroup<CreationDataReferenceItem>[] => {
  const groups = events.flatMap((event): ReferenceGroup<CreationDataReferenceItem>[] => {
    if (!isDataReferenceEvent(event)) return []
    const items = normalizeDataReferences(event.environment_patch?.data_sources)
    if (!items.length) return []
    return [{
      id: dataGroupId(event),
      title: String(event.data?.skill_step_title || (event.actor?.id === 'webpage_scrape' ? '网页即时采集' : '数据检索')),
      toolName: displayCreationToolNames(event.actor.name || '数据检索 Tool'),
      query: String(event.data?.query || ''),
      items,
    }]
  })
  if (groups.length || !fallback.length) return groups
  return [{
    id: 'data-group-history-summary',
    title: '历史参考数据',
    toolName: '历史汇总',
    query: '',
    items: fallback,
    legacy: true,
  }]
}

const normalizeDataReferences = (value: unknown): CreationDataReferenceItem[] => {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): CreationDataReferenceItem[] => {
    if (!item || typeof item !== 'object') return []
    const source = item as Record<string, unknown>
    const sourceId = Number(source.source_id)
    if (!Number.isFinite(sourceId) || sourceId <= 0) return []
    return [{
      source_id: sourceId,
      title: String(source.title || `数据来源 #${sourceId}`),
      source_kind: String(source.source_kind || ''),
      freshness_class: String(source.freshness_class || ''),
      refresh_required: source.refresh_required === true,
      can_use: source.can_use !== false,
      ...(source.evidence_status
        ? { evidence_status: String(source.evidence_status) }
        : {}),
      ...(source.evidence_reason
        ? { evidence_reason: String(source.evidence_reason) }
        : {}),
      ...(source.unavailable_reason
        ? { unavailable_reason: String(source.unavailable_reason) }
        : {}),
    }]
  })
}

interface RejectedDataSource {
  source_id: number
  title: string
  url: string
}

const normalizeRejectedDataSources = (value: unknown): RejectedDataSource[] => {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): RejectedDataSource[] => {
    if (!item || typeof item !== 'object') return []
    const source = item as Record<string, unknown>
    const sourceId = Number(source.source_id)
    if (!Number.isFinite(sourceId) || sourceId <= 0) return []
    return [{
      source_id: sourceId,
      title: String(source.title || `数据来源 #${sourceId}`),
      url: String(source.url || ''),
    }]
  })
}

const isDataReferenceEvent = (event: CreationAgentEvent) => (
  event.type === 'tool.completed'
  && ['data_search', 'webpage_scrape'].includes(event.actor?.id || '')
  && Array.isArray(event.environment_patch?.data_sources)
)

const mergeDataReferences = (
  ...referenceGroups: CreationDataReferenceItem[][]
): CreationDataReferenceItem[] => {
  const referencesBySourceId = new Map<number, CreationDataReferenceItem>()
  referenceGroups.forEach(references => {
    references.forEach(reference => referencesBySourceId.set(reference.source_id, reference))
  })
  return [...referencesBySourceId.values()]
}

const dataReferencesFromEvents = (events: CreationAgentEvent[]) => {
  return mergeDataReferences(...events
    .filter(isDataReferenceEvent)
    .map(event => normalizeDataReferences(event.environment_patch?.data_sources)))
}

interface DataMetricRow {
  dimension: string
  metric: string
  value: string
  note: string
}

interface DataPresentation {
  title: string
  summary: string
  rows: DataMetricRow[]
}

const normalizeDataText = (value: unknown) => {
  if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

const presentDataSnapshot = (snapshot: DataSnapshot): DataPresentation => {
  const structured = snapshot.structured_data ?? {}
  const rows = Array.isArray(structured.metric_rows)
    ? structured.metric_rows.flatMap((value): DataMetricRow[] => {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return []
        const row = value as Record<string, unknown>
        const metric = normalizeDataText(row.metric)
        const metricValue = normalizeDataText(row.value)
        if (!metric || !metricValue) return []
        return [{
          dimension: normalizeDataText(row.dimension),
          metric,
          value: metricValue,
          note: normalizeDataText(row.note),
        }]
      })
    : []
  return {
    title: normalizeDataText(structured.title) || '数据指标概况',
    summary: normalizeDataText(structured.summary)
      || normalizeDataText(snapshot.content_text).slice(0, 500)
      || '这条数据尚未形成可理解的摘要',
    rows,
  }
}

const formatDataTimestamp = (timestamp?: number | null) => (
  timestamp
    ? new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
    : '尚未采集'
)

const hasLegacyDataSearchResults = (events: CreationAgentEvent[]) => events.some(event => (
  event.type === 'tool.completed'
  && event.actor?.id === 'data_search'
  && Number(event.data?.result_count) > 0
))

const mapDataSearchResults = (value: unknown): CreationDataReferenceItem[] => {
  if (!value || typeof value !== 'object') return []
  return normalizeDataReferences((value as { results?: unknown }).results)
}

const normalizeBrainstormState = (value: CreationBrainstormState | null): CreationBrainstormState | null => {
  if (!value) return null
  const answeredCount = Math.max(0, Number(value.answered_count) || 0)
  return {
    ...value,
    depth: Math.max(0, Number(value.depth) || answeredCount),
    can_continue_brainstorm: value.can_continue_brainstorm === true,
    readiness_reason: String(value.readiness_reason || ''),
    continuation_directions: Array.isArray(value.continuation_directions)
      ? value.continuation_directions
      : [],
    open_flags: Array.isArray(value.open_flags) ? value.open_flags : [],
    invalidated_question_ids: Array.isArray(value.invalidated_question_ids)
      ? value.invalidated_question_ids
      : [],
    history: Array.isArray(value.history) ? value.history : [],
    decisions: Array.isArray(value.decisions) ? value.decisions : [],
  }
}

const mapCreationHistory = (histories: any[]): CreationHistoryItem[] => histories.map((h: any) => {
  const fullContent = sanitizeGeneratedContent(h.generated_content)
  const previewContent = stripInternalCreationMarkers(fullContent)
  const rootRequest = String(h.root_request || '')
  let references: CreationReferenceItem[] = []
  try {
    const parsed = typeof h.references_json === 'string' ? JSON.parse(h.references_json || '[]') : h.references_json
    references = normalizeReferenceItems(parsed)
  } catch {
    references = []
  }
  const agentEvents = parseHistoryJson<CreationAgentEvent[]>(h.agent_trace_json, [])
  return {

    id: Number(h.id),
    prompt: rootRequest || h.prompt,
    timestamp: new Date(h.updated_at ?? h.created_at).toLocaleString('zh-CN'),
    preview: previewContent.slice(0, 100) + (previewContent.length > 100 ? '...' : ''),
    fullContent,
    docType: h.doc_type || '',
    audience: h.audience || '',
    references,
    dataReferences: dataReferencesFromEvents(agentEvents),
    sessionId: h.session_id || null,
    rootRequest,
    conversation: parseHistoryJson<CreationChatMessage[]>(h.conversation_json, []),
    agentEvents,

    revisionNo: Math.max(1, Number(h.revision_no) || 1),
    editOperation: String(h.edit_operation || 'create_document'),
    documentPatch: parseHistoryJson<Record<string, unknown> | null>(h.document_patch_json, null),
    model: h.model || null,
    latencyMs: normalizeLatencyMs(h.latency_ms),
    evidence: parseHistoryJson<CreationEvidenceItem[]>(h.evidence_json, []),
    sourceKind: h.source_kind === 'scheduled_task' ? 'scheduled_task' : 'creation',
    lifecycleStatus: ['running', 'failed', 'cancelled'].includes(String(h.lifecycle_status))
      ? h.lifecycle_status
      : 'completed',
    creationMode: h.creation_mode === 'brainstorm' ? 'brainstorm' : 'direct',
    creationBrief: normalizeBrainstormState(
      parseHistoryJson<CreationBrainstormState | null>(h.creation_brief_json, null),
    ),
    brainstormRevision: Number.isFinite(Number(h.brainstorm_revision))
      ? Number(h.brainstorm_revision)
      : null,
    progressEpoch: Math.max(0, Number(h.progress_epoch) || 0),
  }
})

const parseHistoryJson = <T,>(value: unknown, fallback: T): T => {
  try {
    if (value == null || value === '') return fallback
    return (typeof value === 'string' ? JSON.parse(value) : value) as T
  } catch {
    return fallback
  }
}

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
  const lineStarts: number[] = []
  let absoluteOffset = 0
  lines.forEach((line) => {
    lineStarts.push(absoluteOffset)
    absoluteOffset += line.length + 1
  })
  const blocks: MarkdownBlock[] = []
  let markdownBuffer: string[] = []
  let markdownBufferStart = 0
  let index = 0

  const flushMarkdown = () => {
    let first = 0
    let last = markdownBuffer.length
    while (first < last && !markdownBuffer[first].trim()) first += 1
    while (last > first && !markdownBuffer[last - 1].trim()) last -= 1
    const markdown = markdownBuffer.slice(first, last).join('\n')
    if (markdown) {
      const startIndex = markdownBufferStart + first
      const endIndex = markdownBufferStart + last - 1
      blocks.push({
        type: 'markdown',
        content: markdown,
        startLine: startIndex + 1,
        endLine: endIndex + 1,
        startOffset: lineStarts[startIndex],
        endOffset: lineStarts[endIndex] + lines[endIndex].length,
      })
    }
    markdownBuffer = []
  }

  while (index < lines.length) {
    const current = lines[index]
    const next = lines[index + 1]
    if (isPotentialTableRow(current) && next && isTableSeparator(next)) {
      flushMarkdown()
      const tableStart = index
      const headers = splitTableRow(current)
      const alignments = tableAlignments(next)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && isPotentialTableRow(lines[index])) {
        rows.push(splitTableRow(lines[index]))
        index += 1
      }
      blocks.push({
        type: 'table',
        headers,
        alignments,
        rows,
        startLine: tableStart + 1,
        endLine: index,
        startOffset: lineStarts[tableStart],
        endOffset: lineStarts[index - 1] + lines[index - 1].length,
      })
      continue
    }

    if (!markdownBuffer.length) markdownBufferStart = index
    markdownBuffer.push(current)
    index += 1
  }

  flushMarkdown()
  return blocks
}

const normalizeSectionName = (value: string) =>
  value.replace(/[\s：:、，,。.!！?？（）()《》“”"'`#_-]+/g, '').toLowerCase()

const changeTypeLabel = (changeType: DocumentChange['changeType']) => ({
  added: '新增',
  modified: '修改',
  deleted: '删除',
}[changeType])

const documentChangesFromPatch = (
  patch: Record<string, unknown> | undefined,
  content: string,
): DocumentChange[] => {
  if (!patch) return []
  const rawChanges = Array.isArray(patch.changes) ? patch.changes : []
  const changes = rawChanges
    .map((item): DocumentChange | null => {
      if (!item || typeof item !== 'object') return null
      const value = item as Record<string, unknown>
      const rawType = String(value.change_type || value.changeType || '')
      if (!['added', 'modified', 'deleted'].includes(rawType)) return null
      const start = Number(value.start_line ?? value.startLine)
      const end = Number(value.end_line ?? value.endLine)
      return {
        changeType: rawType as DocumentChange['changeType'],
        sectionTitle: String(value.section_title || value.sectionTitle || '标题与导语'),
        startLine: Number.isFinite(start) && start > 0 ? start : null,
        endLine: Number.isFinite(end) && end > 0 ? end : null,
        summary: String(value.summary || ''),
      }
    })
    .filter((item): item is DocumentChange => Boolean(item))
  if (changes.length) return changes

  const targets = Array.isArray(patch.target_sections)
    ? patch.target_sections.map(item => String(item)).filter(Boolean)
    : []
  if (!targets.length) return []
  const operation = String(patch.operation || '')
  const changeType: DocumentChange['changeType'] = operation === 'append_section'
    ? 'added'
    : operation === 'delete_section'
      ? 'deleted'
      : 'modified'
  const lines = content.split('\n')
  return targets.map((target) => {
    const targetName = normalizeSectionName(target)
    const startIndex = lines.findIndex(line => {
      const match = line.match(/^##\s+(.+?)\s*#*\s*$/)
      return Boolean(match && (
        normalizeSectionName(match[1]) === targetName
        || normalizeSectionName(match[1]).includes(targetName)
        || targetName.includes(normalizeSectionName(match[1]))
      ))
    })
    let endIndex = lines.length - 1
    if (startIndex >= 0) {
      const nextHeadingOffset = lines
        .slice(startIndex + 1)
        .findIndex(line => /^##\s+/.test(line))
      if (nextHeadingOffset >= 0) endIndex = startIndex + nextHeadingOffset
    }
    return {
      changeType,
      sectionTitle: target,
      startLine: startIndex >= 0 && changeType !== 'deleted' ? startIndex + 1 : null,
      endLine: startIndex >= 0 && changeType !== 'deleted' ? endIndex + 1 : null,
      summary: `${changeTypeLabel(changeType)}“${target}”中的内容`,
    }
  })
}

const selectionTouchesElement = (selection: Selection | null, element: HTMLElement | null) => {
  if (!selection || !element || selection.isCollapsed || selection.rangeCount === 0) return false
  return Boolean(
    (selection.anchorNode && element.contains(selection.anchorNode))
    || (selection.focusNode && element.contains(selection.focusNode)),
  )
}

const useSelectionStableContent = (
  source: string,
  containerRef: React.RefObject<HTMLElement>,
) => {
  const latestSourceRef = useRef(source)
  const selectionLockedRef = useRef(false)
  const [renderedContent, setRenderedContent] = useState(source)

  useEffect(() => {
    latestSourceRef.current = source
    if (!selectionLockedRef.current) setRenderedContent(source)
  }, [source])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const handleSelectStart = () => {
      selectionLockedRef.current = true
    }
    const handleSelectionChange = () => {
      if (selectionTouchesElement(window.getSelection(), container)) {
        selectionLockedRef.current = true
        return
      }
      if (!selectionLockedRef.current) return
      selectionLockedRef.current = false
      setRenderedContent(latestSourceRef.current)
    }

    container.addEventListener('selectstart', handleSelectStart)
    document.addEventListener('selectionchange', handleSelectionChange)
    return () => {
      container.removeEventListener('selectstart', handleSelectStart)
      document.removeEventListener('selectionchange', handleSelectionChange)
    }
  }, [containerRef])

  const syncRenderedContent = useCallback((nextSource: string) => {
    latestSourceRef.current = nextSource
    selectionLockedRef.current = false
    setRenderedContent(nextSource)
  }, [])

  return { renderedContent, selectionLockedRef, syncRenderedContent }
}

const CreationPanel: React.FC<CreationPanelProps> = ({ className = '', active = true }) => {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)
  const adminApiBaseUrl = useAppStore((s) => s.adminApiBaseUrl)
  const gatewayApiBaseUrl = useAppStore((s) => s.gatewayApiBaseUrl)
  const authToken = useAppStore((s) => s.authToken)
  const currentUser = useAppStore((s) => s.currentUser)
  const localNickname = useAppStore((s) => s.localNickname)
  const cloudBalance = useAppStore((s) => s.cloudBalance)
  const setCloudBalance = useAppStore((s) => s.setCloudBalance)
  const draft = useAppStore((s) => s.creationDraft)
  const setCreationDraft = useAppStore((s) => s.setCreationDraft)
  const setWindowMode = useAppStore((s) => s.setWindowMode)
  const creationModelConfigs = useAppStore((s) => s.creationModelConfigs)
  const creationHistoryOpenTarget = useAppStore((s) => s.creationHistoryOpenTarget)
  const setCreationHistoryOpenTarget = useAppStore((s) => s.setCreationHistoryOpenTarget)
  const setCreationModelConfig = useAppStore((s) => s.setCreationModelConfig)
  const userDisplayName = getUserDisplayName(currentUser, localNickname)

  const {
    prompt,
    docType,
    audience,
    generatedContent,
    inheritFormat,
    enableImageGeneration,
    contentWeight,
    qualityWeight,
    completenessWeight,
    usageWeight,
    formatWeight,
    freshnessWeight,
    referencePreview,
    dataReferences,
    sessionId,
    rootRequest,
    conversation,
    agentEvents,
    creationMode,
    brainstormState,
  } = draft

  const setPrompt = (v: string) => setCreationDraft({ prompt: v })
  const setDocType = (v: string) => setCreationDraft({ docType: v })
  const setAudience = (v: string) => setCreationDraft({ audience: v })
  const setGeneratedContent = (updater: string | ((prev: string) => string)) => {
    if (typeof updater === 'function') {
      setCreationDraft({ generatedContent: sanitizeGeneratedContent(updater(useAppStore.getState().creationDraft.generatedContent)) })
    } else {
      setCreationDraft({ generatedContent: sanitizeGeneratedContent(updater) })
    }
  }
  const setInheritFormat = (v: boolean) => setCreationDraft({ inheritFormat: v })
  const setEnableImageGeneration = (v: boolean) => setCreationDraft({ enableImageGeneration: v })
  const setContentWeight = (v: number) => setCreationDraft({ contentWeight: v })
  const setQualityWeight = (v: number) => setCreationDraft({ qualityWeight: v })
  const setCompletenessWeight = (v: number) => setCreationDraft({ completenessWeight: v })
  const setUsageWeight = (v: number) => setCreationDraft({ usageWeight: v })
  const setFormatWeight = (v: number) => setCreationDraft({ formatWeight: v })
  const setFreshnessWeight = (v: number) => setCreationDraft({ freshnessWeight: v })
  const setReferencePreview = (v: ReferencePreview | null) => setCreationDraft({ referencePreview: v })
  const setDataReferences = (v: CreationDataReferenceItem[]) => setCreationDraft({ dataReferences: v })
  const setSessionId = (v: string | null) => setCreationDraft({ sessionId: v })
  const setRootRequest = (v: string) => setCreationDraft({ rootRequest: v.slice(0, 12000) })
  const setConversation = (v: CreationChatMessage[]) => setCreationDraft({ conversation: retainConversationContext(v) })
  const setAgentEvents = (v: CreationAgentEvent[]) => setCreationDraft({ agentEvents: v.slice(-240) })
  const setCreationMode = (v: CreationMode) => setCreationDraft({ creationMode: v })
  const setBrainstormState = (v: CreationBrainstormState | null) => setCreationDraft({ brainstormState: v })

  const [dataSourcesById, setDataSourcesById] = useState<Record<number, DataSource>>({})
  const [dataReferencesLoading, setDataReferencesLoading] = useState(false)
  const [dataReferencesError, setDataReferencesError] = useState('')
  const [legacyDataReferencesRecovered, setLegacyDataReferencesRecovered] = useState(false)
  const [isGenerating, setIsGenerating] = useState(false)
  const [isBrainstormLoading, setIsBrainstormLoading] = useState(false)
  const [brainstormError, setBrainstormError] = useState<string | null>(null)
  const [brainstormSelectedOptions, setBrainstormSelectedOptions] = useState<string[]>([])
  const [brainstormCustomAnswerSelected, setBrainstormCustomAnswerSelected] = useState(false)
  const [brainstormCustomAnswer, setBrainstormCustomAnswer] = useState('')
  const [brainstormHistoryIndex, setBrainstormHistoryIndex] = useState<number | null>(null)
  const [brainstormContinuationOpen, setBrainstormContinuationOpen] = useState(false)
  const [brainstormContinuationDirectionId, setBrainstormContinuationDirectionId] = useState('')
  const [brainstormCustomDirection, setBrainstormCustomDirection] = useState('')
  const [anchoredBrainstormState, setAnchoredBrainstormState] = useState<CreationBrainstormState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [copySuccess, setCopySuccess] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [topTab, setTopTab] = useState<'creation' | 'history' | 'skills' | 'tools'>('creation')
  const [activeBottomTab, setActiveBottomTab] = useState<BottomTab | null>(null)
  const [highlightedReferenceGroup, setHighlightedReferenceGroup] = useState<string | null>(null)
  const [bottomNavigationRevision, setBottomNavigationRevision] = useState(0)
  const [fullscreenPanel, setFullscreenPanel] = useState<FullscreenPanel>(null)
  const toggleBottomTab = (tab: BottomTab) =>

    setActiveBottomTab(prev => prev === tab ? null : tab)
  const [creationTools, setCreationTools] = useState(loadCreationTools)
  const [creationHistory, setCreationHistory] = useState<CreationHistoryItem[]>([])
  const [activeHistoryId, setActiveHistoryId] = useState<number | null>(null)
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(1)
  const [historySearch, setHistorySearch] = useState('')
  const [debouncedHistorySearch, setDebouncedHistorySearch] = useState('')
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [lastInferenceMeta, setLastInferenceMeta] = useState<{ model: string; latencyMs: number | null } | null>(null)
  const [attachments, setAttachments] = useState<UserAttachment[]>([])
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [composerAddMenuOpen, setComposerAddMenuOpen] = useState(false)
  const [browserExtensionConnected, setBrowserExtensionConnected] = useState(false)
  const [browserLiveJobs, setBrowserLiveJobs] = useState<BrowserLiveJob[]>([])
  const [browserCrawlerEnabled, setBrowserCrawlerEnabled] = useState(
    () => window.localStorage.getItem(BROWSER_CRAWLER_PREFERENCE_KEY) === 'true',
  )
  const [currentDocumentSource, setCurrentDocumentSource] = useState<CreationSkillSource | null>(null)
  const [localSkills, setLocalSkills] = useState<LocalCreationSkill[]>([])
  const [skillsLoading, setSkillsLoading] = useState(false)
  const [skillsLoaded, setSkillsLoaded] = useState(false)
  const [skillsError, setSkillsError] = useState('')
  const [publishingSkillId, setPublishingSkillId] = useState<number | null>(null)
  const [skillLibraryView, setSkillLibraryView] = useState<'mine' | 'market'>('mine')
  const [marketQueryDraft, setMarketQueryDraft] = useState('')
  const [marketQuery, setMarketQuery] = useState('')
  const [marketCategoryIdDraft, setMarketCategoryIdDraft] = useState('')
  const [marketCategoryId, setMarketCategoryId] = useState('')
  const [marketCategories, setMarketCategories] = useState(OFFLINE_CREATION_SKILL_CATEGORIES)
  const [marketOffset, setMarketOffset] = useState(0)
  const [marketSkills, setMarketSkills] = useState<CreationSkillMarketItem[]>([])
  const [marketTotal, setMarketTotal] = useState(0)
  const [marketLoading, setMarketLoading] = useState(false)
  const [marketError, setMarketError] = useState('')
  const [installingMarketSkillId, setInstallingMarketSkillId] = useState<string | null>(null)
  const [skillEditor, setSkillEditor] = useState<{ source?: CreationSkillSource; initialSkill?: LocalCreationSkill } | null>(null)
  const [skillDetail, setSkillDetail] = useState<CreationSkillDetailData | null>(null)
  const [skillDetailMarketItem, setSkillDetailMarketItem] = useState<CreationSkillMarketItem | null>(null)
  const [skillDetailFocusFiles, setSkillDetailFocusFiles] = useState(false)
  const [skillPendingDelete, setSkillPendingDelete] = useState<LocalCreationSkill | null>(null)
  const [deletingSkillId, setDeletingSkillId] = useState<number | null>(null)
  const [uploadingSkillPackage, setUploadingSkillPackage] = useState(false)
  const [skillUploadMenuOpen, setSkillUploadMenuOpen] = useState(false)
  const [currentDocumentSkills, setCurrentDocumentSkills] = useState<LocalCreationSkill[]>([])
  const [inlineCapabilities, setInlineCapabilities] = useState<CreationInlineEditCapabilities | null>(null)
  const [inlineSelection, setInlineSelection] = useState<CreationSelectionSnapshot | null>(null)
  const [inlinePromptOpen, setInlinePromptOpen] = useState(false)
  const [inlineCustomPrompt, setInlineCustomPrompt] = useState('')
  const [inlineRunningAction, setInlineRunningAction] = useState<CreationInlineEditAction | null>(null)
  const [inlineError, setInlineError] = useState('')
  const [inlineBrainstorm, setInlineBrainstorm] = useState<InlineBrainstormSession | null>(null)
  const [inlineUndo, setInlineUndo] = useState<{
    requestId: string
    sessionId: string
    historyId: number
    resultHash: string
  } | null>(null)
  const turnMatchedSkillsRef = useRef<MatchedCreationSkill[] | null>(null)
  const [skillPickerOpen, setSkillPickerOpen] = useState(false)
  const [skillQuery, setSkillQuery] = useState('')
  const [activeSkillPickerIndex, setActiveSkillPickerIndex] = useState(0)
  const [pendingConfirmation, setPendingConfirmation] = useState<{
    question: string
    userMessage: string
    requestId: string
  } | null>(null)
  const [workspaceSplit, setWorkspaceSplit] = useState(60)
  const contentRef = useRef<HTMLDivElement>(null)
  const {
    renderedContent: selectionStableContent,
    selectionLockedRef: documentSelectionLockedRef,
    syncRenderedContent: syncSelectionStableContent,
  } = useSelectionStableContent(generatedContent, contentRef)
  const bottomPanelRef = useRef<HTMLDivElement>(null)
  const referencePanelRef = useRef<HTMLDivElement>(null)
  const dataPanelRef = useRef<HTMLDivElement>(null)
  const pendingBottomNavigationRef = useRef<{
    tab: Extract<BottomTab, 'reference' | 'data'>
    groupId?: string
  } | null>(null)
  const chatTimelineRef = useRef<HTMLDivElement>(null)
  const brainstormContinuationRef = useRef<HTMLDivElement>(null)
  const workspaceRef = useRef<HTMLElement>(null)
  const workspaceResizeCleanupRef = useRef<(() => void) | null>(null)
  const fullscreenTriggerRef = useRef<HTMLButtonElement | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const brainstormAbortRef = useRef<AbortController | null>(null)
  const inlineAbortRef = useRef<AbortController | null>(null)
  const inlineBrainstormAbortRef = useRef<AbortController | null>(null)
  const inlineRequestIdRef = useRef<string | null>(null)
  const inlineViewportAnchorRef = useRef<{
    sourceOffset: number
    scrollTop: number
    viewportOffset: number | null
  } | null>(null)
  const inlineTimelineViewportRef = useRef<{ scrollTop: number } | null>(null)
  const inlineSelectionEpochRef = useRef(0)
  const inlineToolbarInteractionRef = useRef(false)
  const activeHistoryIdRef = useRef<number | null>(null)
  const activeHistoryEpochRef = useRef<number | null>(null)
  const startupRecoveryAttemptedRef = useRef(false)
  const legacyDataRecoveryRef = useRef(0)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const composerAddMenuRef = useRef<HTMLDivElement>(null)
  const browserCrawlerPreferenceInitializedRef = useRef(
    window.localStorage.getItem(BROWSER_CRAWLER_PREFERENCE_KEY) != null,
  )
  const skillPackageInputRef = useRef<HTMLInputElement>(null)
  const skillZipInputRef = useRef<HTMLInputElement>(null)
  const skillUploadMenuRef = useRef<HTMLDivElement>(null)
  const promptInputRef = useRef<HTMLTextAreaElement>(null)
  const promptSkillShellRef = useRef<HTMLDivElement>(null)
  const promptImeGuard = useImeCompositionGuard<HTMLTextAreaElement>()
  const activeUserMessageRef = useRef('')
  const activeUserEntryRef = useRef<CreationChatMessage | null>(null)
  const enabledToolIds = useMemo(
    () => enabledCreationToolIds(creationTools),
    [creationTools],
  )
  const toolResultLimits = useMemo(
    () => creationToolResultLimits(creationTools),
    [creationTools],
  )
  const memorySearchEnabled = enabledToolIds.includes('memory_search')
  const internetSearchEnabled = enabledToolIds.includes('internet_search')

  useEffect(() => {
    const controller = new AbortController()
    const loadInlineCapabilities = async () => {
      if (!activeHistoryId || !generatedContent.trim() || isGenerating) {
        setInlineCapabilities(null)
        setInlineSelection(null)
        return
      }
      const localBaseHash = await sha256Hex(generatedContent)
      if (controller.signal.aborted) return
      const unavailableCapabilities = (reason: string): CreationInlineEditCapabilities => ({
        schema_version: 'creation.inline-edit.v1',
        enabled: false,
        actions: [],
        max_selection_bytes: 12000,
        max_custom_prompt_bytes: 2000,
        supported_node_kinds: INLINE_EDIT_NODE_KINDS,
        history_id: activeHistoryId,
        revision_no: 0,
        base_document_hash: localBaseHash,
        disabled_reason: reason,
      })
      try {
        const response = await fetchWithLocalhostFallback(
          `${apiBaseUrl}/api/creation/inline-edit/capabilities?history_id=${activeHistoryId}`,
          { signal: controller.signal },
        )
        if (response.status === 404) {
          setInlineCapabilities(unavailableCapabilities('选区编辑服务未启动，请重启客户端后再试'))
          return
        }
        if (!response.ok) throw new Error(`inline capabilities ${response.status}`)
        const capabilities = await response.json() as CreationInlineEditCapabilities
        if (
          capabilities.schema_version !== 'creation.inline-edit.v1'
          || capabilities.history_id !== activeHistoryId
          || capabilities.base_document_hash !== localBaseHash
        ) {
          setInlineCapabilities(unavailableCapabilities('文档版本已变化，请等待保存完成后重新划选'))
          return
        }
        setInlineCapabilities(capabilities)
      } catch (capabilitiesError) {
        if (!controller.signal.aborted) {
          console.warn('加载选区编辑能力失败:', capabilitiesError)
          setInlineCapabilities(unavailableCapabilities('选区编辑服务暂不可用，请稍后重试'))
        }
      }
    }
    void loadInlineCapabilities()
    return () => controller.abort()
  }, [activeHistoryId, apiBaseUrl, generatedContent, isGenerating])

  useEffect(() => {
    const updateSelection = () => {
      const activeElement = document.activeElement
      const toolbarOwnsFocus = activeElement instanceof Element
        && Boolean(activeElement.closest('.creation-selection-toolbar'))
      if (inlinePromptOpen || inlineToolbarInteractionRef.current || toolbarOwnsFocus) return
      const requestEpoch = inlineSelectionEpochRef.current + 1
      inlineSelectionEpochRef.current = requestEpoch
      const capabilities = inlineCapabilities
      const container = contentRef.current
      if (
        !capabilities
        || !container
        || isGenerating
        || inlineRunningAction
        || selectionStableContent !== generatedContent
        || capabilities.revision_no == null
        || !capabilities.base_document_hash
      ) {
        setInlineSelection(null)
        return
      }
      void resolveCreationSelection({
        selection: window.getSelection(),
        container,
        originalSource: generatedContent,
        baseRevisionNo: capabilities.revision_no,
        baseDocumentHash: capabilities.base_document_hash,
        maxSelectionBytes: capabilities.max_selection_bytes,
        supportedNodeKinds: capabilities.supported_node_kinds,
      }).then((snapshot) => {
        if (inlineSelectionEpochRef.current !== requestEpoch) return
        setInlineSelection(snapshot)
        if (snapshot) setInlineError('')
      })
    }
    // WKWebView can emit `selectionchange` before the mouse/touch selection has
    // reached its final range. Re-read the selection when the gesture ends so
    // desktop users do not lose the toolbar because an intermediate collapsed
    // range won the async epoch race. `keyup` covers Shift+Arrow selections.
    const updateSettledSelection = (event: Event) => {
      const target = event.target
      if (target instanceof Element && target.closest('.creation-selection-toolbar')) return
      window.requestAnimationFrame(updateSelection)
    }
    document.addEventListener('selectionchange', updateSelection)
    document.addEventListener('pointerup', updateSettledSelection, true)
    document.addEventListener('mouseup', updateSettledSelection, true)
    document.addEventListener('keyup', updateSettledSelection, true)
    contentRef.current?.addEventListener('scroll', updateSelection, { passive: true })
    window.addEventListener('resize', updateSelection)
    if (inlineCapabilities) updateSelection()
    return () => {
      inlineSelectionEpochRef.current += 1
      document.removeEventListener('selectionchange', updateSelection)
      document.removeEventListener('pointerup', updateSettledSelection, true)
      document.removeEventListener('mouseup', updateSettledSelection, true)
      document.removeEventListener('keyup', updateSettledSelection, true)
      contentRef.current?.removeEventListener('scroll', updateSelection)
      window.removeEventListener('resize', updateSelection)
    }
  }, [generatedContent, inlineCapabilities, inlinePromptOpen, inlineRunningAction, isGenerating, selectionStableContent])

  useEffect(() => {
    let cancelled = false
    let timer: number | null = null
    const refreshBrowserExtensionStatus = async () => {
      let nextDelay = 5_000
      try {
        const response = await fetch(`${apiBaseUrl}/api/browser-integration/status`)
        if (!response.ok) throw new Error(`browser status ${response.status}`)
        const status = await response.json() as { connected?: boolean; jobs?: BrowserLiveJob[] }
        if (cancelled) return
        const connected = status.connected === true
        const jobs = Array.isArray(status.jobs) ? status.jobs : []
        setBrowserExtensionConnected(connected)
        setBrowserLiveJobs(jobs)
        if (jobs.some(job => ['queued', 'running'].includes(job.status))) nextDelay = 900
        if (connected && !browserCrawlerPreferenceInitializedRef.current) {
          browserCrawlerPreferenceInitializedRef.current = true
          setBrowserCrawlerEnabled(true)
          window.localStorage.setItem(BROWSER_CRAWLER_PREFERENCE_KEY, 'true')
        }
      } catch {
        if (!cancelled) {
          setBrowserExtensionConnected(false)
          setBrowserLiveJobs([])
        }
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void refreshBrowserExtensionStatus(), nextDelay)
      }
    }
    void refreshBrowserExtensionStatus()
    return () => {
      cancelled = true
      if (timer != null) window.clearTimeout(timer)
    }
  }, [apiBaseUrl])

  useEffect(() => {
    if (!composerAddMenuOpen) return
    const handlePointerDown = (event: MouseEvent) => {
      if (!composerAddMenuRef.current?.contains(event.target as Node)) {
        setComposerAddMenuOpen(false)
      }
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setComposerAddMenuOpen(false)
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [composerAddMenuOpen])

  useEffect(() => {
    if (!skillUploadMenuOpen) return
    const handlePointerDown = (event: MouseEvent) => {
      if (!skillUploadMenuRef.current?.contains(event.target as Node)) {
        setSkillUploadMenuOpen(false)
      }
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSkillUploadMenuOpen(false)
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [skillUploadMenuOpen])

  const updateCreationTools = (
    updater: (current: typeof creationTools) => typeof creationTools,
  ) => {
    setCreationTools(current => saveCreationTools(updater(current)))
  }

  const handleInstallTool = (id: CreationToolId) => {
    updateCreationTools(current => setCreationToolInstalled(current, id, true))
  }

  const handleUninstallTool = (id: CreationToolId) => {
    updateCreationTools(current => setCreationToolInstalled(current, id, false))
  }

  const handleToggleTool = (id: CreationToolId, enabled: boolean) => {
    updateCreationTools(current => setCreationToolEnabled(current, id, enabled))
  }

  const handleToolResultLimitChange = (id: CreationToolId, resultLimit: number) => {
    updateCreationTools(current => setCreationToolResultLimit(current, id, resultLimit))
  }

  const startTimer = () => {
    setElapsedSeconds(0)
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setElapsedSeconds((prev) => prev + 1)
    }, 1000)
  }

  const stopTimer = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  useEffect(() => () => {
    stopTimer()
    workspaceResizeCleanupRef.current?.()
  }, [])

  const workspaceSplitBounds = () => {
    const width = Math.max(1, workspaceRef.current?.getBoundingClientRect().width || 0)
    return {
      min: Math.min(50, (360 / width) * 100),
      max: Math.max(50, 100 - (310 / width) * 100),
    }
  }

  const clampWorkspaceSplit = (value: number) => {
    const bounds = workspaceSplitBounds()
    return Math.min(bounds.max, Math.max(bounds.min, value))
  }

  const updateWorkspaceSplitFromPointer = (clientX: number) => {
    const rect = workspaceRef.current?.getBoundingClientRect()
    if (!rect?.width) return
    setWorkspaceSplit(clampWorkspaceSplit(((clientX - rect.left) / rect.width) * 100))
  }

  const handleWorkspaceResizeStart = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    workspaceResizeCleanupRef.current?.()
    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const handleMove = (pointerEvent: PointerEvent) => {
      updateWorkspaceSplitFromPointer(pointerEvent.clientX)
    }
    const cleanup = () => {
      window.removeEventListener('pointermove', handleMove)
      window.removeEventListener('pointerup', cleanup)
      window.removeEventListener('pointercancel', cleanup)
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
      workspaceResizeCleanupRef.current = null
    }
    workspaceResizeCleanupRef.current = cleanup
    window.addEventListener('pointermove', handleMove)
    window.addEventListener('pointerup', cleanup)
    window.addEventListener('pointercancel', cleanup)
    updateWorkspaceSplitFromPointer(event.clientX)
  }

  const handleWorkspaceResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const bounds = workspaceSplitBounds()
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    if (event.key === 'Home') setWorkspaceSplit(bounds.min)
    else if (event.key === 'End') setWorkspaceSplit(bounds.max)
    else setWorkspaceSplit(value => clampWorkspaceSplit(value + (event.key === 'ArrowLeft' ? -2 : 2)))
  }

  useEffect(() => {
    if (typeof ResizeObserver === 'undefined' || !workspaceRef.current) return
    const observer = new ResizeObserver(() => {
      setWorkspaceSplit(value => clampWorkspaceSplit(value))
    })
    observer.observe(workspaceRef.current)
    return () => observer.disconnect()
  }, [])

  const loadCreationHistory = useCallback(async (signal?: AbortSignal) => {
    setHistoryLoading(true)
    setHistoryError(null)
    const params = new URLSearchParams({
      paged: 'true',
      limit: String(HISTORY_PAGE_SIZE),
      offset: String((historyPage - 1) * HISTORY_PAGE_SIZE),
    })
    if (debouncedHistorySearch) params.set('q', debouncedHistorySearch)

    try {
      const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/history?${params}`, { signal })
      if (!response.ok) throw new Error(`creation history fetch failed: ${response.status}`)
      const data = await response.json()
      if (signal?.aborted) return
      const records = Array.isArray(data) ? data : data.items ?? []
      setCreationHistory(mapCreationHistory(records))
      setHistoryTotal(Number.isFinite(Number(data?.total)) ? Number(data.total) : records.length)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      console.error('加载创作记录失败:', err)
      setCreationHistory([])
      setHistoryTotal(0)
      setHistoryError('创作记录加载失败，请稍后重试。')
    } finally {
      if (!signal?.aborted) setHistoryLoading(false)
    }
  }, [apiBaseUrl, debouncedHistorySearch, historyPage])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setHistoryPage(1)
      setDebouncedHistorySearch(historySearch.trim())
    }, 300)
    return () => window.clearTimeout(timer)
  }, [historySearch])

  useEffect(() => {
    const controller = new AbortController()
    void loadCreationHistory(controller.signal)
    return () => controller.abort()
  }, [loadCreationHistory])

  const openFullscreenPanel = (
    panel: Exclude<FullscreenPanel, null>,
    trigger: HTMLButtonElement,
  ) => {
    fullscreenTriggerRef.current = trigger
    if (panel !== 'document') setActiveBottomTab(panel)
    setFullscreenPanel(panel)
  }

  const closeFullscreenPanel = useCallback(() => {
    setFullscreenPanel(null)
    window.requestAnimationFrame(() => fullscreenTriggerRef.current?.focus())
  }, [])

  useEffect(() => {
    if (!fullscreenPanel) return undefined
    const previousOverflow = document.body.style.overflow
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeFullscreenPanel()
    }
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [closeFullscreenPanel, fullscreenPanel])

  const openBottomTab = (
    tab: Extract<BottomTab, 'reference' | 'data'>,
    groupId?: string,
  ) => {
    pendingBottomNavigationRef.current = { tab, groupId }
    setActiveBottomTab(tab)
    setHighlightedReferenceGroup(groupId || null)
    setBottomNavigationRevision(revision => revision + 1)
  }

  useEffect(() => {
    const pending = pendingBottomNavigationRef.current
    if (!pending || activeBottomTab !== pending.tab) return undefined

    let innerFrame = 0
    const outerFrame = window.requestAnimationFrame(() => {
      innerFrame = window.requestAnimationFrame(() => {
        const panel = pending.tab === 'reference' ? referencePanelRef.current : dataPanelRef.current
        const target = pending.groupId ? document.getElementById(pending.groupId) : null
        if (!panel || (pending.groupId && !target)) return

        pendingBottomNavigationRef.current = null
        bottomPanelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
        panel.scrollTop = 0
        target?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
        if (target) {
          target.focus({ preventScroll: true })
          window.setTimeout(() => {
            setHighlightedReferenceGroup(current => current === pending.groupId ? null : current)
          }, 1800)
        } else {
          bottomPanelRef.current
            ?.querySelector<HTMLButtonElement>(`[data-bottom-tab="${pending.tab}"]`)
            ?.focus({ preventScroll: true })
        }
      })
    })

    return () => {
      window.cancelAnimationFrame(outerFrame)
      if (innerFrame) window.cancelAnimationFrame(innerFrame)
    }
  }, [activeBottomTab, agentEvents, bottomNavigationRevision, dataReferences])

  const handleOpenReferenceSource = useCallback((item: ReferenceItem) => {
    const sourceType = String(item.source_type || 'document')
    const sourceId = Number(item.source_id ?? item.id)
    if (!Number.isFinite(sourceId) || sourceId <= 0) {
      if (item.source_url) window.open(item.source_url, '_blank', 'noopener,noreferrer')
      return
    }
    const internalType = sourceType === 'knowledge'
      ? 'bake_knowledge'
      : sourceType === 'pending_document'
        ? 'capture'
        : sourceType
    if (!['document', 'bake_knowledge', 'operation', 'action', 'capture', 'data'].includes(internalType)) {
      if (item.source_url) window.open(item.source_url, '_blank', 'noopener,noreferrer')
      return
    }
    window.dispatchEvent(new CustomEvent('view-rag-reference', {
      detail: {
        type: internalType,
        artifactId: ['bake_knowledge', 'operation', 'action'].includes(internalType)
          ? sourceId
          : undefined,
        documentId: internalType === 'document' ? sourceId : undefined,
        captureId: internalType === 'capture' ? sourceId : undefined,
        dataSourceId: internalType === 'data' ? sourceId : undefined,
      },
    }))
  }, [])

  useEffect(() => {
    const sourceIds = [...new Set(dataReferences.map(item => item.source_id))]
    if (!sourceIds.length) {
      setDataSourcesById({})
      return
    }

    const controller = new AbortController()
    setDataReferencesLoading(true)
    setDataReferencesError('')
    void Promise.allSettled(sourceIds.map(async sourceId => {
      const response = await fetchWithLocalhostFallback(
        `${apiBaseUrl}/api/data/sources/${sourceId}`,
        { signal: controller.signal },
      )
      if (!response.ok) throw new Error(`data source fetch failed: ${response.status}`)
      return response.json() as Promise<DataSource>
    })).then(results => {
      if (controller.signal.aborted) return
      const loaded: Record<number, DataSource> = {}
      let failedCount = 0
      results.forEach(result => {
        if (result.status === 'fulfilled') loaded[result.value.id] = result.value
        else failedCount += 1
      })
      setDataSourcesById(loaded)
      if (failedCount) {
        setDataReferencesError(
          failedCount === sourceIds.length
            ? '参考数据详情加载失败，请稍后重试。'
            : `${failedCount} 个参考数据来源暂时无法加载。`,
        )
      }
    }).finally(() => {
      if (!controller.signal.aborted) setDataReferencesLoading(false)
    })

    return () => controller.abort()
  }, [apiBaseUrl, dataReferences])

  const recoverLegacyDataReferences = async (item: CreationHistoryItem) => {
    if (item.dataReferences.length || !hasLegacyDataSearchResults(item.agentEvents)) return
    const recoveryId = legacyDataRecoveryRef.current + 1
    legacyDataRecoveryRef.current = recoveryId
    setDataReferencesLoading(true)
    setDataReferencesError('')
    setLegacyDataReferencesRecovered(false)
    try {
      const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/tools/data-search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: item.rootRequest || item.prompt, limit: 10 }),
      })
      if (!response.ok) throw new Error(`data search failed: ${response.status}`)
      const recovered = mapDataSearchResults(await response.json())
      if (legacyDataRecoveryRef.current !== recoveryId) return
      setDataReferences(recovered)
      setLegacyDataReferencesRecovered(recovered.length > 0)
      if (!recovered.length) setDataReferencesError('未能从当前本地数据中恢复这条历史记录的参考数据。')
    } catch (recoverError) {
      if (legacyDataRecoveryRef.current !== recoveryId) return
      console.error('恢复历史参考数据失败:', recoverError)
      setDataReferencesError('历史记录未保存来源编号，当前数据恢复失败。')
    } finally {
      if (legacyDataRecoveryRef.current === recoveryId) setDataReferencesLoading(false)
    }
  }

  const handleRestoreHistory = (item: typeof creationHistory[0]) => {
    inlineAbortRef.current?.abort()
    inlineBrainstormAbortRef.current?.abort()
    setInlineUndo(null)
    setInlineSelection(null)
    setInlineBrainstorm(null)
    setInlinePromptOpen(false)
    setInlineError('')
    activeHistoryIdRef.current = item.id
    setActiveHistoryId(item.id)
    activeHistoryEpochRef.current = item.progressEpoch
    legacyDataRecoveryRef.current += 1
    setPrompt('')
    setCreationMode(item.creationMode)
    setBrainstormState(item.creationBrief)
    setAnchoredBrainstormState(
      item.creationMode === 'brainstorm' && item.fullContent.trim()
        ? item.creationBrief
        : null,
    )
    setGeneratedContent(item.fullContent)
    setSessionId(item.sessionId || `history-${item.id}`)
    const restoredConversation: CreationChatMessage[] = item.conversation.length
      ? item.conversation.filter((message, index, messages) => {
        // 旧版终态保存可能在原始用户消息之后再补一条 server-user-* 副本。
        // 历史恢复时只隐藏这类确定的服务端重复项，避免同一需求在时间线末尾
        // 再出现一次并让脑暴锚点看起来像被移动；用户主动重复发送的内容保留。
        if (message.role !== 'user' || !message.id.startsWith('server-user-')) return true
        const content = message.content.trim()
        if (!content) return true
        return !messages.slice(0, index).some(previous => (
          previous.role === 'user' && previous.content.trim() === content
        ))
      })
      : [{
        id: `history-user-${item.id}`,
        role: 'user',
        content: item.prompt,
        createdAt: Date.now(),
      }]
    setConversation(restoredConversation)
    setRootRequest(
      item.rootRequest
      || restoredConversation.find(message => message.role === 'user')?.content
      || item.prompt,
    )
    // 历史记录不会重连旧的 SSE / 模型请求。旧版若在暂停后失败，
    // 可能只持久化了 running / waiting 事件；恢复时必须按中断收口，
    // 否则这条旧记录的深度思考与呼吸灯会永久闪烁。
    const restoredEvents = item.lifecycleStatus === 'running'
      ? [...item.agentEvents]
      : closeInterruptedHistoricalRuns(
        item.agentEvents,
        item.sessionId || `history-${item.id}`,
      )
    if (
      item.documentPatch
      && !restoredEvents.some(event => event.type === 'document.patch.applied')
    ) {
      restoredEvents.push({
        schema_version: 'creation.agent.v1',
        event_id: `history-patch-${item.id}`,
        session_id: item.sessionId || `history-${item.id}`,
        run_id: `history-run-${item.id}`,
        sequence: restoredEvents.length + 1,
        timestamp: Date.now(),
        type: 'document.patch.applied',
        status: 'completed',
        actor: {
          kind: 'agent',
          id: 'document_writer_agent',
          name: '文档撰写 Agent',
        },
        summary: String(item.documentPatch.summary || '已恢复本轮文档改动'),
        environment_patch: { document_patch: item.documentPatch },
        data: { patch: item.documentPatch },
      })
    }
    setAgentEvents(restoredEvents)
    setDataReferences(item.dataReferences)
    setDataSourcesById({})
    setDataReferencesError('')
    setLegacyDataReferencesRecovered(false)
    void recoverLegacyDataReferences(item)
    setReferencePreview({

      requirement: {
        topic: item.prompt,
        doc_type: item.docType || docType,
        audience: item.audience || audience,
        style: '',
        keywords: [],
      },
      references: item.references || [],
    })
    setCurrentDocumentSource({
      kind: 'creation_history',
      id: String(item.id),
      title: item.prompt,
      content: item.fullContent,
      docType: item.docType || docType,
    })
    if (item.creationMode === 'brainstorm' && item.sessionId) {
      // 历史记录中的 creation_brief 是生成文档时的锚点快照，继续脑暴后的
      // 实时 revision 保存在独立会话表。恢复记录时静默同步实时状态，既能
      // 立即展示后续方向，也避免用户先撞一次版本冲突才看到最新进度。
      void restoreBrainstormSession(item.sessionId, item.rootRequest || item.prompt, true)
    }
    if (contentRef.current) {
      setTimeout(() => contentRef.current?.scrollTo?.({ top: 0, behavior: 'smooth' }), 100)
    }
  }

  // 任务页点击「查看执行过程」后，拉取单条创作记录并恢复会话与执行流水。
  // 注意：清空目标会重新触发本 effect，不能用 cleanup 取消进行中的请求，
  // 改用 ref 防止同一个目标被重复消费（含 StrictMode 双调用）。
  const consumedHistoryTargetRef = useRef<number | null>(null)
  useEffect(() => {
    if (creationHistoryOpenTarget == null) {
      // 目标被清空后重置消费标记，允许之后再次跳转同一条记录。
      consumedHistoryTargetRef.current = null
      return
    }
    if (consumedHistoryTargetRef.current === creationHistoryOpenTarget) return
    consumedHistoryTargetRef.current = creationHistoryOpenTarget
    const targetId = creationHistoryOpenTarget
    setCreationHistoryOpenTarget(null)
    fetch(`${apiBaseUrl}/api/creation/history/${targetId}`)
      .then(async res => {
        if (!res.ok) throw new Error('创作记录不存在')
        return res.json()
      })
      .then(raw => {
        const [item] = mapCreationHistory([raw])
        if (!item) return
        handleRestoreHistory(item)
        setTopTab('creation')
      })
      .catch(() => {
        // 记录已被删除或接口不可用时保持当前页面不变。
      })
  }, [apiBaseUrl, creationHistoryOpenTarget, setCreationHistoryOpenTarget])

  const loadLocalSkills = useCallback(async () => {
    setSkillsLoading(true)
    setSkillsLoaded(false)
    setSkillsError('')
    try {
      setLocalSkills(await listLocalCreationSkills(apiBaseUrl))
    } catch (err) {
      setLocalSkills([])
      setSkillsError(toLocalApiError(err, '技能加载失败'))
    } finally {
      setSkillsLoading(false)
      setSkillsLoaded(true)
    }
  }, [apiBaseUrl])

  useEffect(() => {
    void loadLocalSkills()
  }, [loadLocalSkills])

  const loadMarketSkills = useCallback(async () => {
    setMarketLoading(true)
    setMarketError('')
    try {
      const page = await searchCreationSkillMarket(adminApiBaseUrl, {
        query: marketQuery,
        categoryId: marketCategoryId,
        limit: SKILL_MARKET_PAGE_SIZE,
        offset: marketOffset,
      })
      setMarketSkills(page.items)
      setMarketTotal(page.total)
    } catch (err) {
      setMarketSkills([])
      setMarketTotal(0)
      setMarketError(toUserFacingError(err, '技能市场加载失败'))
    } finally {
      setMarketLoading(false)
    }
  }, [adminApiBaseUrl, marketCategoryId, marketOffset, marketQuery])

  const loadMarketCategories = useCallback(async () => {
    setMarketCategories(await fetchCreationSkillCategories(adminApiBaseUrl))
  }, [adminApiBaseUrl])

  useEffect(() => {
    if (topTab === 'skills' && skillLibraryView === 'market') {
      void loadMarketSkills()
      void loadMarketCategories()
    }
  }, [loadMarketCategories, loadMarketSkills, skillLibraryView, topTab])

  useEffect(() => {
    if (!currentDocumentSource) {
      setCurrentDocumentSkills([])
      return
    }
    let cancelled = false
    listLocalCreationSkills(apiBaseUrl, {
      sourceKind: currentDocumentSource.kind,
      sourceId: currentDocumentSource.id,
    }).then(items => {
      if (!cancelled) setCurrentDocumentSkills(items)
    }).catch(() => {
      if (!cancelled) setCurrentDocumentSkills([])
    })
    return () => { cancelled = true }
  }, [apiBaseUrl, currentDocumentSource])

  const handleSkillSaved = (skill: LocalCreationSkill) => {
    setLocalSkills(prev => [skill, ...prev.filter(item => item.id !== skill.id)])
    if (currentDocumentSource?.kind === skill.sourceKind && currentDocumentSource.id === skill.sourceId) {
      setCurrentDocumentSkills(prev => [skill, ...prev.filter(item => item.id !== skill.id)])
    }
  }

  const handleSkillPackageSelected = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files
    event.target.value = ''
    if (!files?.length) return
    setUploadingSkillPackage(true)
    setSkillsError('')
    try {
      const input = await importAgentSkillPackage(files)
      const saved = await saveLocalCreationSkill(apiBaseUrl, input)
      handleSkillSaved(saved)
      setSkillLibraryView('mine')
      showLocalSkillDetail(saved)
    } catch (err) {
      setSkillsError(toLocalApiError(err, '上传 Skill 源文件失败'))
    } finally {
      setUploadingSkillPackage(false)
    }
  }

  const handleSkillZipSelected = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const archive = event.target.files?.[0]
    event.target.value = ''
    if (!archive) return
    setUploadingSkillPackage(true)
    setSkillsError('')
    try {
      const input = await importAgentSkillZip(archive)
      const saved = await saveLocalCreationSkill(apiBaseUrl, input)
      handleSkillSaved(saved)
      setSkillLibraryView('mine')
      showLocalSkillDetail(saved)
    } catch (err) {
      setSkillsError(toLocalApiError(err, '上传 Skill 源文件 ZIP 失败'))
    } finally {
      setUploadingSkillPackage(false)
    }
  }

  const handleToggleSkillInstalled = async (skill: LocalCreationSkill) => {
    if (skill.status !== 'saved') {
      setSkillsError('请先打开草稿并保存技能，再安装使用。')
      return
    }
    setSkillsError('')
    const { id, createdAt: _createdAt, updatedAt: _updatedAt, ...input } = skill
    try {
      const saved = await saveLocalCreationSkill(apiBaseUrl, { ...input, installed: !skill.installed }, id)
      handleSkillSaved(saved)
    } catch (err) {
      setSkillsError(toLocalApiError(err, skill.installed ? '卸载技能失败' : '安装技能失败'))
    }
  }

  const handlePublishSkill = async (skill: LocalCreationSkill, published: boolean) => {
    if (skill.status !== 'saved') {
      setSkillsError('请先打开草稿并保存技能，再发布到市场。')
      return
    }
    if (published && !skill.categoryId) {
      setSkillsError('技能当前为“私有”类目，请先编辑技能并选择非私有的创作类目，再发布到市场。')
      return
    }
    if (!authToken || !currentUser) {
      setSkillsError('请先登录 MemoryBread 账户，再发布到技能市场。')
      return
    }
    setPublishingSkillId(skill.id)
    setSkillsError('')
    const { id, createdAt: _createdAt, updatedAt: _updatedAt, ...input } = skill
    try {
      const cloud = await publishCreationSkill(adminApiBaseUrl, authToken, input, published)
      const saved = await saveLocalCreationSkill(apiBaseUrl, {
        ...input,
        cloudSkillId: cloud.id,
        published: cloud.published,
      }, id)
      handleSkillSaved(saved)
    } catch (err) {
      setSkillsError(toUserFacingError(err, published ? '发布技能失败' : '取消发布技能失败'))
    } finally {
      setPublishingSkillId(null)
    }
  }

  const handleInstallMarketSkill = async (marketSkill: CreationSkillMarketItem) => {
    setInstallingMarketSkillId(marketSkill.id)
    setMarketError('')
    const existing = localSkills.find(skill => skill.cloudSkillId === marketSkill.id)
    const installing = !existing?.installed
    try {
      const completeMarketSkill = marketSkill.packageFiles.length > 0
        ? marketSkill
        : await fetchCreationSkillMarketDetail(adminApiBaseUrl, marketSkill.id)
      const marketInput = marketCreationSkillToLocalInput(completeMarketSkill)
      let input = marketInput
      if (existing) {
        const { id: _id, createdAt: _createdAt, updatedAt: _updatedAt, ...existingInput } = existing
        input = installing
          ? existing.sourceKind === 'market'
            ? { ...marketInput, clientSkillKey: existing.clientSkillKey }
            : { ...existingInput, installed: true }
          : { ...existingInput, installed: false }
      }
      const saved = await saveLocalCreationSkill(
        apiBaseUrl,
        input,
        existing?.id,
      )
      handleSkillSaved(saved)
      setSkillDetail(current => current?.id === marketSkill.id
        ? { ...current, installed: installing }
        : current)
    } catch (err) {
      setMarketError(toUserFacingError(err, installing ? '安装市场技能失败' : '卸载市场技能失败'))
    } finally {
      setInstallingMarketSkillId(null)
    }
  }

  const showLocalSkillDetail = (skill: LocalCreationSkill, focusFiles = false) => {
    const path = categoryPathFor(OFFLINE_CREATION_SKILL_CATEGORIES, skill.categoryId)
      .map(item => item.name)
    setSkillDetailMarketItem(null)
    setSkillDetail(localSkillDetail(skill, path))
    setSkillDetailFocusFiles(focusFiles)
  }

  const showMarketSkillDetail = async (skill: CreationSkillMarketItem, focusFiles = false) => {
    const installed = localSkills.some(item =>
      item.cloudSkillId === skill.id && item.installed,
    )
    setSkillDetailMarketItem(skill)
    setSkillDetail(marketSkillDetail(skill, installed))
    setSkillDetailFocusFiles(focusFiles)
    if (skill.packageFiles.length > 0) return
    try {
      const detail = await fetchCreationSkillMarketDetail(adminApiBaseUrl, skill.id)
      setSkillDetailMarketItem(detail)
      setSkillDetail(current => current?.id === skill.id ? marketSkillDetail(detail, installed) : current)
      setMarketSkills(current => current.map(item => item.id === detail.id ? detail : item))
    } catch (err) {
      setMarketError(toUserFacingError(err, 'Skill 源文件加载失败'))
    }
  }

  const closeSkillDetail = useCallback(() => {
    setSkillDetail(null)
    setSkillDetailMarketItem(null)
    setSkillDetailFocusFiles(false)
  }, [])

  useEffect(() => {
    if (!skillPendingDelete || deletingSkillId !== null) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSkillPendingDelete(null)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [deletingSkillId, skillPendingDelete])

  const handleDeleteSkill = (skill: LocalCreationSkill) => {
    if (skill.published) {
      setSkillsError('已发布的技能暂不能删除。')
      return
    }
    setSkillPendingDelete(skill)
  }

  const confirmDeleteSkill = async () => {
    const skill = skillPendingDelete
    if (!skill) return
    setDeletingSkillId(skill.id)
    setSkillsError('')
    try {
      await deleteLocalCreationSkill(apiBaseUrl, skill.id)
      setLocalSkills(prev => prev.filter(item => item.id !== skill.id))
      setCurrentDocumentSkills(prev => prev.filter(item => item.id !== skill.id))
      setSkillPendingDelete(null)
    } catch (err) {
      setSkillsError(toLocalApiError(err, '删除技能失败'))
    } finally {
      setDeletingSkillId(null)
    }
  }

  const remoteModelAllowed = canUseRemoteCreationModel(currentUser, cloudBalance)
  const activeCreationModelId = getEffectiveCreationModelId(creationModelConfigs, remoteModelAllowed)
  const useGatewayCreation = activeCreationModelId === REMOTE_CREATION_MODEL_ID
  const installedSkills = useMemo(
    () => localSkills.filter(skill => skill.status === 'saved' && skill.installed),
    [localSkills],
  )
  const installedMarketSkillIds = useMemo(
    () => new Set(
      localSkills
        .filter(skill => skill.installed && skill.cloudSkillId)
        .map(skill => skill.cloudSkillId as string),
    ),
    [localSkills],
  )
  const marketCategoryOptions = useMemo(
    () => creationSkillCategoryOptions(marketCategories),
    [marketCategories],
  )
  // 自动推荐技能已下线：输入过程中不再做任何召回计算，只有用户显式 @ 提及
  // 的 Skill 会进入创作指令。
  const matchedSkills = useMemo(
    () => matchCreationSkills(prompt, installedSkills),
    [installedSkills, prompt],
  )
  // 提交后的执行时解析结果只在当轮生效：Loop 运行中 selected_skills 与技能
  // 指令都以它为准；未解析时退回输入时的显式 @ 选择。
  const effectiveMatchedSkills = () => turnMatchedSkillsRef.current ?? matchedSkills
  const skillPickerItems = useMemo(() => {
    const query = skillQuery.trim().toLowerCase()
    return installedSkills
      .filter(skill => !query || `${skill.title}\n${skill.summary}`.toLowerCase().includes(query))
      .slice(0, 8)
  }, [installedSkills, skillQuery])

  useEffect(() => {
    if (!skillPickerOpen) return
    setActiveSkillPickerIndex(0)
  }, [skillPickerOpen, skillQuery])

  useEffect(() => {
    if (!skillPickerOpen) return
    setActiveSkillPickerIndex(current => Math.min(current, Math.max(0, skillPickerItems.length - 1)))
  }, [skillPickerItems.length, skillPickerOpen])

  useEffect(() => {
    if (!skillPickerOpen) return
    const handlePointerDown = (event: MouseEvent) => {
      if (!promptSkillShellRef.current?.contains(event.target as Node)) {
        setSkillPickerOpen(false)
        setSkillQuery('')
      }
    }
    document.addEventListener('mousedown', handlePointerDown)
    return () => document.removeEventListener('mousedown', handlePointerDown)
  }, [skillPickerOpen])

  useEffect(() => {
    if (!skillPickerOpen || !skillPickerItems.length) return
    document.getElementById(`creation-skill-option-${skillPickerItems[activeSkillPickerIndex]?.id}`)
      ?.scrollIntoView?.({ block: 'nearest' })
  }, [activeSkillPickerIndex, skillPickerItems, skillPickerOpen])
  const messageWithAttachments = (message = prompt) => {
    const attachmentPrompt = buildAttachmentPrompt(attachments)
    return attachmentPrompt ? `${message.trim()}\n\n${attachmentPrompt}` : message.trim()
  }
  const promptWithAttachments = (message = prompt) => {
    const basePrompt = messageWithAttachments(message)
    return `${basePrompt}${buildCreationSkillInstruction(effectiveMatchedSkills())}`
  }

  const handlePromptChange = (value: string, caret: number | null) => {
    setPrompt(value)
    const beforeCaret = value.slice(0, caret ?? value.length)
    // 查询词不允许空白：选中技能后插入的文本带尾随空格，若正则允许空白，
    // 已完成的 @提及会被误判为进行中的查询，导致继续打字时选择器反复弹出。
    const mention = beforeCaret.match(/@([^\s@\n]{0,48})$/)
    setSkillPickerOpen(Boolean(mention))
    setSkillQuery(mention?.[1] || '')
  }

  const selectPromptSkill = (skill: LocalCreationSkill) => {
    const textarea = promptInputRef.current
    const caret = textarea?.selectionStart ?? prompt.length
    const beforeCaret = prompt.slice(0, caret)
    // 与触发正则保持一致：只回溯不含空白的查询段，避免误吞正文里的其它 @。
    const mentionMatch = beforeCaret.match(/@([^\s@\n]{0,48})$/)
    const mentionStart = mentionMatch ? beforeCaret.length - mentionMatch[0].length : beforeCaret.length
    const nextPrompt = `${beforeCaret.slice(0, mentionStart)}@${skill.title} ${prompt.slice(caret)}`
    const nextCaret = mentionStart + skill.title.length + 2
    setPrompt(nextPrompt)
    setSkillPickerOpen(false)
    setSkillQuery('')
    window.requestAnimationFrame(() => {
      textarea?.focus()
      textarea?.setSelectionRange(nextCaret, nextCaret)
    })
  }

  const handleMarketSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const query = marketQueryDraft.trim()
    const categoryId = marketCategoryIdDraft
    setMarketOffset(0)
    if (query === marketQuery && categoryId === marketCategoryId && marketOffset === 0) {
      void loadMarketSkills()
    } else {
      setMarketQuery(query)
      setMarketCategoryId(categoryId)
    }
  }

  const handleSelectModel = (modelId: string) => {
    if (modelId === REMOTE_CREATION_MODEL_ID && !remoteModelAllowed) return
    setCreationModelConfig(modelId, { enabled: true })
  }

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

  const buildPayload = (message = prompt) => {
    const activeModel = creationModelConfigs.find(c => c.id === LOCAL_CREATION_MODEL_ID)
    return {
      user_prompt: `${messageWithAttachments(message)}${buildCreationSkillInstruction(matchedSkills)}`,
      design_templates: [],
      design_ids: [],
      timeline_ids: [],
      capture_ids: [],
      doc_type: docType,
      audience,
      output_format: 'markdown',
      inherit_format: inheritFormat,
      enable_rag: memorySearchEnabled,
      enable_web_search: internetSearchEnabled,
      enabled_tools: enabledToolIds,
      enable_image_generation: enableImageGeneration,
      browser_extension_enabled: browserCrawlerEnabled,
      content_weight: contentWeight / 100,
      quality_weight: qualityWeight / 100,
      completeness_weight: completenessWeight / 100,
      usage_weight: usageWeight / 100,
      format_weight: formatWeight / 100,
      freshness_weight: freshnessWeight / 100,
      max_references: toolResultLimits.memorySearch,
      data_search_limit: toolResultLimits.dataSearch,
      attachments: buildAttachmentMetadata(attachments),
      ...(activeCreationModelId === LOCAL_CREATION_MODEL_ID && activeModel ? {
        creation_model: LOCAL_CREATION_MODEL_ID,
        creation_base_url: activeModel.baseUrl || undefined,
      } : {}),
    }
  }

  const buildGatewayMessages = (references: CreationReferenceItem[], message = prompt) => {
    const systemPrompt = [
      '你是 MemoryBread 的咨询创作助手。',
      '请用专业、结构化的中文输出 Markdown 文档。',
      '不要提及底层供应商或模型实现。',
    ].join('\n')
    const referenceText = references.length
      ? `\n\n本地记忆参考资料：\n${references.map((item, index) => {
        const rawText = item.summary || item.reason || ''
        const text = rawText.length > 900 ? `${rawText.slice(0, 900)}...` : rawText
        return `R#${index + 1} ${item.title || `参考资料 ${index + 1}`}\n类型：${item.doc_type || '未分类'}\n${text}`.trim()
      }).join('\n\n')}`
      : ''
    const options = [
      `文档类型：${docType || '建设方案'}`,
      `目标读者：${audience || '客户'}`,
      `继承历史格式：${inheritFormat ? '是' : '否'}`,
      `启用记忆搜索：${memorySearchEnabled ? '是' : '否'}，参考数量：${references.length}`,
      `启用工具：${enabledToolIds.join(', ')}`,
      `权重：内容 ${contentWeight}%，质量 ${qualityWeight}%，完整性 ${completenessWeight}%，热度 ${usageWeight}%，格式 ${formatWeight}%，时效 ${freshnessWeight}%`,
    ].join('\n')
    return [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: `${options}\n\n创作需求：\n${promptWithAttachments(message)}${referenceText}` },
    ]
  }

  const postGatewayCreation = async (
    references: CreationReferenceItem[],
    message: string,
    signal?: AbortSignal,
  ) => {
    const response = await fetchGatewayChat(`${gatewayApiBaseUrl.replace(/\/+$/, '')}/v1/gateway/chat`, {
      method: 'POST',
      headers: {
        ...serviceEnvironmentHeaders(),
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      signal,
      body: JSON.stringify({
        request_id: `creation-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        user_id: currentUser?.id || null,
        brand_model_id: 'mbcd-plus-v1',
        caller: 'creation',
        messages: buildGatewayMessages(references, message),
        stream: true,
        privacy: { content_logging: false, client_scrubbed: true },
        limits: { max_output_tokens: 8192, max_credit: '100.0000' },
      }),
    })
    if (!response.ok) {
      throw await readGatewayChatError(response, '云端模型服务暂时不可用，请稍后重试')
    }
    return consumeGatewayChatStream(response)
  }

  const postGatewayAgentCall = async (
    messages: Array<{ role: string; content: string }>,
    requestId: string | null,
    signal?: AbortSignal,
  ) => {
    const stableRequestId = String(requestId || '').trim().slice(0, 255)
    const response = await fetchGatewayChat(`${gatewayApiBaseUrl.replace(/\/+$/, '')}/v1/gateway/chat`, {
      method: 'POST',
      headers: {
        ...serviceEnvironmentHeaders(),
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      signal,
      body: JSON.stringify({
        // 沿用 sidecar model.request 的稳定 ID，使暂停、恢复与 Gateway
        // 调用能按同一业务请求追踪，也为结算幂等提供确定键。
        request_id: stableRequestId
          || `creation-agent-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        user_id: currentUser?.id || null,
        brand_model_id: 'mbcd-plus-v1',
        caller: 'creation',
        messages,
        stream: true,
        privacy: { content_logging: false, client_scrubbed: true },
        limits: { max_output_tokens: 8192, max_credit: '100.0000' },
      }),
    })
    if (!response.ok) {
      throw await readGatewayChatError(response, '云端模型服务暂时不可用，请稍后重试')
    }
    const data = await consumeGatewayChatStream(response)
    const content = sanitizeGeneratedContent(data.content)
    if (!content.trim()) throw new Error('品牌模型没有返回 Agent 结果')
    return content
  }

  const inlineEditEvent = (
    requestId: string,
    type: string,
    summary: string,
    data: Record<string, unknown>,
  ): CreationAgentEvent => ({
    schema_version: 'creation.inline-edit.v1',
    event_id: `${type}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    session_id: sessionId || 'current',
    run_id: requestId,
    sequence: 1,
    timestamp: Date.now(),
    type,
    status: 'completed',
    actor: { kind: 'agent', id: 'inline_edit_agent', name: '选区编辑' },
    summary,
    environment_patch: {},
    data,
  })

  const runInlineEdit = async (
    action: CreationInlineEditAction,
    snapshotOverride?: CreationSelectionSnapshot,
    customPromptOverride = '',
  ) => {
    const snapshot = snapshotOverride || inlineSelection
    const capabilities = inlineCapabilities
    const historyId = activeHistoryIdRef.current
    const activeSessionId = sessionId
    const currentDocument = generatedContent
    if (
      !snapshot
      || !capabilities?.enabled
      || !historyId
      || !activeSessionId
      || inlineRunningAction
      || isGenerating
    ) return
    const actionPrompt = action === 'brainstorm' ? customPromptOverride : action === 'polish' ? inlineCustomPrompt : ''
    if (new TextEncoder().encode(actionPrompt).length > capabilities.max_custom_prompt_bytes) {
      setInlineError(`${action === 'brainstorm' ? '脑暴结论' : '自定义润色要求'}过长，请精简后重试`)
      return
    }

    const documentViewport = contentRef.current
    const selectedBlock = documentViewport
      ? [...documentViewport.querySelectorAll<HTMLElement>('[data-md-start][data-md-end]')].find((element) => {
        const start = Number(element.dataset.mdStart)
        const end = Number(element.dataset.mdEnd)
        return Number.isFinite(start) && Number.isFinite(end)
          && start <= snapshot.startOffset && snapshot.startOffset < end
      })
      : null
    const selectedBlockRect = selectedBlock?.getBoundingClientRect()
    const documentViewportRect = documentViewport?.getBoundingClientRect()
    const viewportAnchor = documentViewport
      ? {
        sourceOffset: snapshot.startOffset,
        scrollTop: documentViewport.scrollTop,
        viewportOffset: selectedBlockRect && documentViewportRect
          ? selectedBlockRect.top - documentViewportRect.top
          : null,
      }
      : null

    const controller = new AbortController()
    inlineAbortRef.current = controller
    const requestId = `inline-${Date.now()}-${globalThis.crypto.randomUUID?.() || Math.random().toString(16).slice(2)}`
    inlineRequestIdRef.current = requestId
    setInlineRunningAction(action)
    setInlineError('')
    setInlineUndo(null)
    const instruction = inlineEditUserInstruction(
      action,
      snapshot.selectedText,
      actionPrompt,
    )
    const requestedAt = Date.now()
    inlineTimelineViewportRef.current = { scrollTop: chatTimelineRef.current?.scrollTop ?? 0 }
    setConversation([
      ...useAppStore.getState().creationDraft.conversation,
      {
        id: `inline-user-${requestedAt}`,
        role: 'user',
        content: instruction,
        createdAt: requestedAt,
        runId: requestId,
      },
    ])
    let committedResult: InlineEditResponse | null = null
    const basePayload: Record<string, unknown> = {
      schema_version: 'creation.inline-edit.v1',
      request_id: requestId,
      session_id: activeSessionId,
      history_id: historyId,
      root_request: rootRequest,
      current_document: currentDocument,
      action,
      custom_prompt: actionPrompt,
      model_mode: useGatewayCreation && currentUser?.id ? 'external' : 'local',
      selection: {
        base_revision_no: snapshot.baseRevisionNo,
        base_document_hash: snapshot.baseDocumentHash,
        start_byte: snapshot.startByte,
        end_byte: snapshot.endByte,
        selected_markdown: snapshot.selectedMarkdown,
        selected_markdown_hash: snapshot.selectedMarkdownHash,
        selected_text: snapshot.selectedText,
        start_line: snapshot.startLine,
        end_line: snapshot.endLine,
      },
    }
    try {
      let payload = basePayload
      let result: InlineEditResponse | null = null
      for (let phase = 0; phase < 2; phase += 1) {
        const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/inline-edit/run`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify(payload),
        })
        if (!response.ok) {
          throw new Error(await readApiErrorMessage(response, `选区${inlineEditActionLabel(action)}失败`))
        }
        result = await response.json() as InlineEditResponse
        if (result.status !== 'paused') break
        const messages = result.model_request?.messages
        if (!Array.isArray(messages) || !messages.length) throw new Error('选区编辑缺少模型请求内容')
        const modelResult = await postGatewayAgentCall(
          messages,
          result.model_request?.request_id || requestId,
          controller.signal,
        )
        payload = {
          ...basePayload,
          resume_state: result.resume_state,
          model_result: modelResult,
        }
      }
      if (!result) throw new Error('选区编辑没有返回结果')
      committedResult = result.status === 'committed' ? result : null
      if (result.status === 'cancelled') {
        const completedAt = Date.now()
        setConversation([
          ...useAppStore.getState().creationDraft.conversation,
          {
            id: `inline-assistant-${completedAt}`,
            role: 'assistant',
            content: `本次${inlineEditActionLabel(action)}已取消，文档未修改。`,
            createdAt: completedAt,
            runId: requestId,
          },
        ])
        return
      }
      if (result.status === 'no_change') {
        if (action === 'brainstorm') {
          setInlineBrainstorm(current => current ? { ...current, applied: true } : current)
        }
        setInlineError('所选内容已经符合要求，未产生修改')
        const completedAt = Date.now()
        setConversation([
          ...useAppStore.getState().creationDraft.conversation,
          {
            id: `inline-assistant-${completedAt}`,
            role: 'assistant',
            content: `所选内容已经符合${inlineEditActionLabel(action)}要求，未产生修改。`,
            createdAt: completedAt,
            runId: requestId,
          },
        ])
        return
      }
      if (!await verifyInlineEditResponse(snapshot, currentDocument, result)) {
        throw new Error('修改结果校验失败，原文未在本地应用，请重新划选')
      }
      const nextContent = result.content as string
      const patch = result.patch as Record<string, unknown>
      const now = Date.now()
      const nextConversation: CreationChatMessage[] = [
        ...useAppStore.getState().creationDraft.conversation,
        {
          id: `inline-assistant-${now}`,
          role: 'assistant',
          content: `已完成${inlineEditActionLabel(action)}。`,
          createdAt: now + 1,
          runId: requestId,
        },
      ]
      const state = useAppStore.getState().creationDraft
      const patchEvent = inlineEditEvent(
        requestId,
        'document.patch.applied',
        `已完成${inlineEditActionLabel(action)}`,
        { content: nextContent, patch, operation: patch.operation },
      )
      const completedEvent = {
        ...inlineEditEvent(requestId, 'run.completed', `选区${inlineEditActionLabel(action)}完成`, {
          document: nextContent,
        }),
        sequence: 2,
      }
      // Re-rendering the replaced Markdown removes the browser selection. Keep
      // the operated block at the same visual position instead of letting the
      // general streaming auto-scroll move the document to its end.
      inlineViewportAnchorRef.current = viewportAnchor
      inlineTimelineViewportRef.current = { scrollTop: chatTimelineRef.current?.scrollTop ?? 0 }
      window.getSelection()?.removeAllRanges()
      syncSelectionStableContent(nextContent)
      setGeneratedContent(nextContent)
      setConversation(nextConversation)
      setAgentEvents([...state.agentEvents, patchEvent, completedEvent])
      if (action === 'brainstorm') {
        setInlineBrainstorm(current => current ? { ...current, applied: true } : current)
      }
      setInlineCapabilities(prev => prev ? {
        ...prev,
        revision_no: result?.revision_no ?? prev.revision_no,
        base_document_hash: String(patch.result_hash || ''),
      } : prev)
      setInlineUndo({
        requestId,
        sessionId: activeSessionId,
        historyId,
        resultHash: String(patch.result_hash || ''),
      })
      setInlineSelection(null)
      setInlinePromptOpen(false)
      setInlineCustomPrompt('')
      if (historyPage === 1) void loadCreationHistory()
    } catch (inlineEditError) {
      if (!controller.signal.aborted) {
        // 外部模型调用可能在 Core 已把请求持久化为 paused 后失败。主动收口
        // 该运行，避免残留的活动请求把同一创作会话后续所有选区操作挡住。
        if (!committedResult) {
          try {
            await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/inline-edit/cancel`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ request_id: requestId, session_id: activeSessionId }),
            })
          } catch (cancelError) {
            console.warn('清理失败的选区编辑运行失败:', cancelError)
          }
        }
        // Core 在返回 committed 前已经原子更新了历史记录。若 WebView 在
        // 响应校验或本地状态同步时失败，不能让界面继续停在旧版本并表现成
        // “没有反应”；以同一 history/session 的持久化结果做一次只读恢复。
        let restored = false
        if (committedResult?.revision_no != null && committedResult.patch) {
          try {
            const response = await fetchWithLocalhostFallback(
              `${apiBaseUrl}/api/creation/history/${historyId}`,
              { signal: controller.signal },
            )
            if (response.ok) {
              const [item] = mapCreationHistory([await response.json()])
              const expectedHash = String(committedResult.patch.result_hash || '')
              const persistedHash = item ? await sha256Hex(item.fullContent) : ''
              if (
                item
                && item.id === historyId
                && item.sessionId === activeSessionId
                && item.revisionNo === committedResult.revision_no
                && Boolean(expectedHash)
                && persistedHash === expectedHash
              ) {
                inlineViewportAnchorRef.current = viewportAnchor
                inlineTimelineViewportRef.current = { scrollTop: chatTimelineRef.current?.scrollTop ?? 0 }
                handleRestoreHistory(item)
                setInlineUndo({
                  requestId,
                  sessionId: activeSessionId,
                  historyId,
                  resultHash: expectedHash,
                })
                restored = true
                if (historyPage === 1) void loadCreationHistory()
              }
            }
          } catch (recoveryError) {
            if (!controller.signal.aborted) console.warn('恢复已提交的选区编辑失败:', recoveryError)
          }
        }
        if (!restored) {
          const failureMessage = toUserFacingError(inlineEditError, `选区${inlineEditActionLabel(action)}失败`)
          setInlineError(failureMessage)
          const completedAt = Date.now()
          setConversation([
            ...useAppStore.getState().creationDraft.conversation,
            {
              id: `inline-assistant-${completedAt}`,
              role: 'assistant',
              content: `${failureMessage}，文档未修改。`,
              createdAt: completedAt,
              runId: requestId,
            },
          ])
        }
      }
    } finally {
      if (inlineAbortRef.current === controller) inlineAbortRef.current = null
      if (inlineRequestIdRef.current === requestId) inlineRequestIdRef.current = null
      setInlineRunningAction(null)
    }
  }

  const cancelInlineEdit = async () => {
    const controller = inlineAbortRef.current
    const activeSessionId = sessionId
    if (!controller || !activeSessionId) return
    const runningRequestId = inlineRequestIdRef.current
    if (runningRequestId) {
      try {
        const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/inline-edit/cancel`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ request_id: runningRequestId, session_id: activeSessionId }),
        })
        if (!response.ok) setInlineError(await readApiErrorMessage(response, '修改正在提交，请稍后同步文档'))
      } catch (cancelError) {
        setInlineError(toUserFacingError(cancelError, '中止选区编辑失败'))
      }
    }
    controller.abort()
  }

  const undoInlineEdit = async () => {
    const undo = inlineUndo
    if (!undo || inlineRunningAction || isGenerating) return
    setInlineError('')
    try {
      const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/inline-edit/undo`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: undo.requestId,
          session_id: undo.sessionId,
          history_id: undo.historyId,
          expected_result_hash: undo.resultHash,
        }),
      })
      if (!response.ok) throw new Error(await readApiErrorMessage(response, '撤销失败'))
      const result = await response.json() as InlineEditResponse
      if (!result.content || !result.patch) throw new Error('撤销结果无效')
      const state = useAppStore.getState().creationDraft
      setGeneratedContent(result.content)
      setConversation([...state.conversation, {
        id: `inline-undo-${Date.now()}`,
        role: 'user',
        content: '撤销本次选区修改',
        createdAt: Date.now(),
        runId: undo.requestId,
      }])
      setAgentEvents([...state.agentEvents, inlineEditEvent(
        undo.requestId,
        'document.patch.applied',
        '已撤销本次选区修改',
        { content: result.content, patch: result.patch },
      )])
      setInlineCapabilities(prev => prev ? {
        ...prev,
        revision_no: result.revision_no ?? prev.revision_no,
        base_document_hash: String((result.patch as Record<string, unknown>).result_hash || ''),
      } : prev)
      setInlineUndo(null)
      setInlineSelection(null)
      if (historyPage === 1) void loadCreationHistory()
    } catch (undoError) {
      setInlineError(toUserFacingError(undoError, '撤销失败'))
    }
  }

  const postLocalCreation = async (message: string, signal?: AbortSignal) => {
    const response = await fetch(`${apiBaseUrl}/api/creation/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify(buildPayload(message)),
    })

    if (!response.ok) {
      const message = await readApiErrorMessage(response, `生成失败: ${response.status}`)
      throw new Error(message.startsWith('生成失败') ? message : `生成失败: ${message}`)
    }

    const reader = response.body?.getReader()
    const decoder = new TextDecoder()
    if (!reader) throw new Error('无法读取响应流')

    let buffer = ''
    let finalContent = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const jsonStr = line.slice(6)
        let event: any
        try {
          event = JSON.parse(jsonStr)
        } catch {
          finalContent += jsonStr
          setGeneratedContent(prev => prev + jsonStr)
          continue
        }

        if (typeof event === 'string') {
          finalContent += event
          setGeneratedContent(prev => prev + event)
          continue
        }
        if (event?.error) {
          throw new Error(`生成失败: ${event.error}`)
        }
        if (event?.done) {
          continue
        }
        const content = typeof event?.content === 'string' ? event.content : ''
        if (content) {
          finalContent += content
          setGeneratedContent(prev => prev + content)
        }
      }
    }

    if (!finalContent.trim()) {
      throw new Error('生成结束但没有返回内容，请检查本地运行环境和创作模型状态')
    }
    return finalContent
  }

  const selectedSkillPayload = () => {
    const turnSkills = effectiveMatchedSkills()
    const primarySkillIds = new Set(turnSkills.map(({ skill }) => skill.id))
    return resolveCreationSkillDependencies(
      turnSkills.map(({ skill }) => skill),
      localSkills,
    ).map(skill => {
      const skillMarkdown = codexSkillPackageFiles(skill)
        .find(file => file.path === 'SKILL.md')
      return {
        id: skill.clientSkillKey || String(skill.id),
        title: skill.title,
        summary: skill.summary,
        // 依赖 Skill 只向引用它的步骤提供能力与规则，不能展开自己的整套工作流。
        workflowRole: primarySkillIds.has(skill.id) ? 'primary' : 'support',
        skillDescription: skill.skillDescription,
        executionSteps: skill.executionSteps,
        skillInstructions: skillMarkdown ? skillFileText(skillMarkdown) || '' : '',
        strictStructure: true,
        titleDesignStyle: skill.commonTitles,
        writingDesign: skill.textStyle,
        imageGeneration: skill.diagramStyle,
        voiceStyle: skill.writingGuidelines,
        fieldExamples: {
          titleDesignStyle: skill.fieldExamples.commonTitles,
          writingDesign: skill.fieldExamples.textStyle,
          imageGeneration: skill.fieldExamples.diagramStyle,
          voiceStyle: skill.fieldExamples.writingGuidelines,
        },
        // 完整示例只供 Skill 编辑与预览，运行时不发送，避免示例章节污染成稿。
        exampleDocumentAvailable: Boolean(skill.exampleDocument.trim()),
      }
    })
  }

  const selectedBrainstormSkillPayload = () => selectedSkillPayload().map(skill => ({
    id: skill.id,
    title: skill.title,
    summary: skill.summary,
    workflowRole: skill.workflowRole,
    skillDescription: skill.skillDescription,
    executionSteps: skill.executionSteps,
    writingDesign: skill.writingDesign,
    voiceStyle: skill.voiceStyle,
  }))

  const buildAgentPayload = (
    message: string,
    chat: CreationChatMessage[],
    extras: Record<string, unknown> = {},
  ) => {
    const payload = buildPayload(message)
    const liveDraft = useAppStore.getState().creationDraft
    delete payload.creation_model
    delete payload.creation_base_url
    return {
      ...payload,
      user_prompt: messageWithAttachments(message),
      session_id: liveDraft.sessionId,
      root_request: liveDraft.rootRequest
        || liveDraft.conversation.find(item => item.role === 'user')?.content
        || messageWithAttachments(message),
      current_document: liveDraft.generatedContent,
      // “用户中止”需要留在可见对话里，但不是下一轮交给模型的创作指令。
      conversation: chat
        .filter(item => !['user_abort', 'session_end'].includes(item.kind || ''))
        .map(item => ({ role: item.role, content: item.content })),
      selected_skills: selectedSkillPayload(),
      model_mode: useGatewayCreation && currentUser?.id ? 'external' : 'local',
      creation_mode: liveDraft.creationMode,
      creation_brief: liveDraft.brainstormState,
      ...extras,
    }
  }

  const toStoredAgentEvent = (event: CreationAgentEvent): CreationAgentEvent => {
    const base: CreationAgentEvent = {
      ...event,
      actor: {
        ...event.actor,
        name: event.actor?.id === 'creation_main_agent'
          ? '创作 Agent'
          : String(event.actor?.name || '创作 Agent').replace(/创作主 Agent/g, '创作 Agent'),
      },
      summary: String(event.summary || '').replace(/创作主 Agent/g, '创作 Agent'),
      goal: event.goal ? { ...event.goal, objective: '' } : undefined,
      environment_patch: {},
      data: {},
    }
    if (event.type === 'intent.interpreted') {
      return {
        ...base,
        data: {
          operation: event.data?.operation,
          target_sections: event.data?.target_sections,
          preserve_untouched: event.data?.preserve_untouched,
          reasoning_summary: event.data?.reasoning_summary,
        },
      }
    }
    if (event.type === 'model.request') {
      return {
        ...base,
        data: { request_id: event.data?.request_id },
      }
    }
    if (event.type === 'thinking.started' || event.type === 'thinking.completed') {
      // 深度思考的阶段与推理摘要是历史记录里展示思考内容的依据，必须保留
      return {
        ...base,
        data: { stage: event.data?.stage, reasoning: event.data?.reasoning },
      }
    }
    if (event.type === 'phase.started' || event.type === 'phase.completed') {
      // 阶段信息是历史记录里三层执行结构的依据，必须保留
      return {
        ...base,
        data: {
          phase_id: event.data?.phase_id,
          phase_title: event.data?.phase_title,
          phase_kind: event.data?.phase_kind,
        },
      }
    }
    if (event.type === 'run.paused') {
      return {
        ...base,
        data: { reason: event.data?.reason },
      }
    }
    if (event.type === 'run.failed') {
      return {
        ...base,
        summary: '创作 Agent 执行失败（详细错误未写入轨迹）',
        // 只保留品牌中立的稳定错误码与重试语义，既不持久化供应商详情，
        // 也避免下一次排障只能看到笼统失败文案。
        data: {
          error_code: event.data?.error_code,
          retryable: event.data?.retryable,
        },
      }
    }
    if (event.type === 'agent.failed') {
      // 节点级容错的失败留痕：错误码与原因是历史轨迹里展示失败详情的依据
      return {
        ...base,
        data: { error_code: event.data?.error_code, error_reason: event.data?.error_reason },
      }
    }
    if (event.type === 'document.replaced') {
      return {
        ...base,
        data: {
          operation: event.data?.operation || 'rewrite_document',
          assembly_audit: event.data?.assembly_audit,
        },
      }
    }
    if (event.type === 'document.patch.applied') {
      return {
        ...base,
        environment_patch: { document_patch: event.data?.patch },
        data: { patch: event.data?.patch },
      }
    }
    if (event.type === 'document.evidence.applied') {
      return {
        ...base,
        data: { evidence: event.data?.evidence },
      }
    }
    if (event.type === 'run.completed') {
      return {
        ...base,
        data: { evidence: event.data?.evidence },
      }
    }
    if (event.type === 'browser.preview.started' || event.type === 'browser.preview.completed') {
      const previews = Array.isArray(event.data?.previews)
        ? event.data.previews
          .filter(item => item && typeof item === 'object')
          .slice(0, 5)
          .map((item: any) => ({
            id: item.id,
            source_id: item.source_id,
            title: item.title,
            image_url: item.image_url,
            status: item.status,
            browser: item.browser,
            interaction_mode: item.interaction_mode,
            focus_policy: item.focus_policy,
            focus_takeover_count: Number(item.focus_takeover_count || 0),
          }))
        : []
      return { ...base, data: { previews } }
    }
    if (event.type === 'tool.completed' || event.type === 'tool.failed') {
      const dataSources = isDataReferenceEvent(event)
        ? normalizeDataReferences(event.environment_patch?.data_sources)
        : []
      const rejectedSources = event.actor?.id === 'webpage_scrape'
        ? normalizeRejectedDataSources(
          event.data?.rejected_sources || event.environment_patch?.rejected_sources,
        )
        : []
      // memory_search 额外保留查询与召回明细，便于事后追溯"某条知识为何没进成稿"
      const isMemorySearch = event.actor?.id === 'memory_search'
      const memoryReferences = isMemorySearch
        ? normalizeReferenceItems(event.environment_patch?.references)
        : []
      const retrievalPlan = isMemorySearch
        ? event.data?.retrieval_plan ?? event.environment_patch?.retrieval_plan
        : undefined
      const entityContext = isMemorySearch
        ? event.data?.entity_context ?? event.environment_patch?.entity_context
        : undefined
      return {
        ...base,
        environment_patch: {
          ...(dataSources.length > 0 ? { data_sources: dataSources } : {}),
          ...(rejectedSources.length > 0 ? { rejected_sources: rejectedSources } : {}),
          ...(memoryReferences.length > 0 ? { references: memoryReferences } : {}),
        },
        data: {
          result_count: event.data?.result_count,
          refresh_required_count: event.data?.refresh_required_count,
          diagram_type: event.data?.diagram_type,
          error_code: event.data?.error_code,
          ...(rejectedSources.length > 0 ? { rejected_sources: rejectedSources } : {}),
          ...(isMemorySearch ? {
            result_limit: event.data?.result_limit,
            source_counts: event.data?.source_counts,
            reference_ids: event.data?.reference_ids,
            query: event.data?.query,
            keywords: event.data?.keywords,
            retrieval_plan: retrievalPlan,
            entity_context: entityContext,
            skill_step_id: event.data?.skill_step_id,
            skill_step_title: event.data?.skill_step_title,
          } : {}),
        },
      }
    }
    if (event.type === 'harness.decision') {
      return {
        ...base,
        data: {
          trigger: event.data?.trigger,
          trigger_status: event.data?.trigger_status,
          reason_code: event.data?.reason_code,
          result_count: event.data?.result_count,
          refreshable_count: event.data?.refreshable_count,
          analyzable_count: event.data?.analyzable_count,
          quality_cycle: event.data?.quality_cycle,
          issue_count: event.data?.issue_count,
          issue_codes: event.data?.issue_codes,
          scheduled: event.data?.scheduled,
          activated_skills: event.data?.activated_skills,
          error_code: event.data?.error_code,
        },
      }
    }
    if (event.type === 'skill.completed') {
      return {
        ...base,
        environment_patch: {
          skill: event.environment_patch?.skill,
          skill_step: event.environment_patch?.skill_step,
          quality_skill_activation: event.environment_patch?.quality_skill_activation,
        },
        data: event.data ? {
          quality_cycle: event.data?.quality_cycle,
          issue_codes: event.data?.issue_codes,
          capabilities: event.data?.capabilities,
        } : undefined,
      }
    }
    if (event.actor?.id === 'quality_review_agent') {
      return {
        ...base,
        environment_patch: { quality_review: event.environment_patch?.quality_review },
      }
    }
    return base
  }

  const applyAgentEvent = (event: CreationAgentEvent, phase: AgentPhaseResult) => {
    phase.sessionId = event.session_id || phase.sessionId
    phase.runId = event.run_id || phase.runId
    if (event.run_id) {
      let currentConversation = useAppStore.getState().creationDraft.conversation
      const activeUserEntry = activeUserEntryRef.current
      let userMessageIndex = activeUserEntry
        ? currentConversation.findIndex(item => item.id === activeUserEntry.id)
        : -1
      if (userMessageIndex < 0 && activeUserEntry) {
        currentConversation = [...currentConversation, activeUserEntry]
        userMessageIndex = currentConversation.length - 1
        setConversation(currentConversation)
      }
      if (userMessageIndex < 0) {
        userMessageIndex = currentConversation.length - 1
        while (userMessageIndex >= 0 && currentConversation[userMessageIndex].role !== 'user') {
          userMessageIndex -= 1
        }
      }
      if (userMessageIndex >= 0) {
        const userMessage = currentConversation[userMessageIndex]
        const runIds = [...new Set([
          ...(userMessage.runIds || []),
          ...(userMessage.runId ? [userMessage.runId] : []),
          event.run_id,
        ])]
        if (runIds.length !== userMessage.runIds?.length) {
          const nextConversation = [...currentConversation]
          nextConversation[userMessageIndex] = { ...userMessage, runIds }
          setConversation(nextConversation)
        }
      }
    }
    if (
      event.type === 'agent.started'
      // 润色类 Agent 只是局部重写相关细节，完成后以 document.patch.applied 局部生效；
      // 不清空已展示文档，避免用户误以为整篇在重新生成。
      && event.actor?.id === 'document_writer_agent'
    ) {
      phase.document = ''
    }
    if (!['document.delta', 'document.patch.delta', 'document.preview'].includes(event.type)) {
      const current = useAppStore.getState().creationDraft.agentEvents
      setAgentEvents([...current, event])
    }
    if (['document.delta', 'document.patch.delta'].includes(event.type)) {
      const content = String(event.data?.content || '')
      if (content) {
        phase.document += content
        setGeneratedContent(phase.document)
      }
    }
    if (event.type === 'document.preview') {
      phase.document = sanitizeGeneratedContent(String(event.data?.content || ''))
      if (phase.document) setGeneratedContent(phase.document)
    }
    if (event.type === 'document.replaced') {
      phase.document = sanitizeGeneratedContent(String(event.data?.content || ''))
      if (phase.document) setGeneratedContent(phase.document)
    }
    if (event.type === 'document.patch.applied') {
      phase.document = sanitizeGeneratedContent(String(event.data?.content || ''))
      if (phase.document) setGeneratedContent(phase.document)
    }
    if (event.type === 'document.evidence.applied') {
      phase.document = sanitizeGeneratedContent(String(event.data?.content || ''))
      if (phase.document) setGeneratedContent(phase.document)
    }
    if (event.type === 'intent.interpreted') {
      const restoredRoot = String(event.data?.root_request || '').trim()
      if (restoredRoot) setRootRequest(restoredRoot)
    }
    if (isDataReferenceEvent(event)) {
      setLegacyDataReferencesRecovered(false)
      const currentReferences = useAppStore.getState().creationDraft.dataReferences
      setDataReferences(mergeDataReferences(
        currentReferences,
        normalizeDataReferences(event.environment_patch?.data_sources),
      ))
    }
    if (event.type === 'model.request') {
      const messages = event.data?.messages
      phase.modelMessages = Array.isArray(messages)
        ? messages
          .filter((item: any) => item && typeof item.content === 'string')
          .map((item: any) => ({ role: String(item.role || 'user'), content: item.content }))
        : null
      phase.modelRequestId = String(event.data?.request_id || '').trim() || null
    }
    if (event.type === 'run.paused') {
      const continuation = event.data?.continuation
      phase.continuation = continuation && typeof continuation === 'object'
        ? continuation as Record<string, unknown>
        : null
      phase.pausedForConfirmation = event.data?.reason === 'user_confirmation'
    }
    if (event.type === 'confirmation.required') {
      setPendingConfirmation({
        question: String(event.data?.question || event.summary),
        userMessage: activeUserMessageRef.current,
        requestId: String(event.data?.request_id || event.event_id),
      })
    }
    if (event.type === 'tool.completed' && event.actor?.id === 'memory_search') {
      const items = event.environment_patch?.references
      if (Array.isArray(items)) {
        const batchReferences = normalizeReferenceItems(items)
        const currentReferences = useAppStore.getState().creationDraft.referencePreview?.references || []
        setReferencePreview({
          requirement: {
            topic: messageWithAttachments(activeUserMessageRef.current),
            doc_type: docType,
            audience,
            style: '',
            keywords: [],
          },
          references: mergeReferenceItems(currentReferences, batchReferences),
        })
      }
    }
    if (event.type === 'run.completed') {
      const completedDocument = sanitizeGeneratedContent(String(event.data?.document || ''))
      if (completedDocument) {
        phase.document = completedDocument
        setGeneratedContent(completedDocument)
      }
      phase.completed = true
    }
    if (event.type === 'run.failed') {
      // phase.document 随预览/写回事件实时更新，失败时即已组装出的部分成果；
      // 附在异常上抛出，供外层 catch 在中断后保全落库。
      const failure = new Error(event.summary || '创作 Agent 执行失败') as Error & {
        partialDocument?: string
        errorCode?: string
        retryable?: boolean
      }
      failure.partialDocument = phase.document
      failure.errorCode = String(event.data?.error_code || '')
      failure.retryable = Boolean(event.data?.retryable)
      throw failure
    }
  }

  const readAgentPhase = async (response: Response): Promise<AgentPhaseResult> => {
    if (!response.ok) {
      throw new Error(await readApiErrorMessage(response, `创作 Agent 启动失败: ${response.status}`))
    }
    const reader = response.body?.getReader()
    if (!reader) throw new Error('无法读取创作 Agent 事件流')
    const decoder = new TextDecoder()
    let buffer = ''
    const phase: AgentPhaseResult = {
      events: [],
      continuation: null,
      modelMessages: null,
      modelRequestId: null,
      completed: false,
      pausedForConfirmation: false,
      document: '',
      sessionId: null,
      runId: null,
    }
    const processLine = (line: string) => {
      if (!line.startsWith('data: ')) return
      const event = JSON.parse(line.slice(6)) as CreationAgentEvent
      phase.events.push(event)
      applyAgentEvent(event, phase)
    }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      lines.forEach(processLine)
    }
    if (buffer.trim()) processLine(buffer.trim())
    return phase
  }

  const postAgentPhase = async (
    payload: Record<string, unknown>,
    signal?: AbortSignal,
  ) => {
    const response = await fetch(`${apiBaseUrl}/api/creation/agent/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify(payload),
    })
    if (response.status === 404) {
      const error = new Error('当前本地服务尚未提供创作 Agent 接口')
      ;(error as Error & { code?: string }).code = 'CREATION_AGENT_NOT_AVAILABLE'
      throw error
    }
    return readAgentPhase(response)
  }

  const postReferencePreview = async (message: string, signal?: AbortSignal) => {
    const payload = buildPayload(message)
    const response = await fetch(`${apiBaseUrl}/api/creation/references`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify(payload),
    })

    if (response.ok) return response
    if (response.status !== 404) return response

    return fetch('http://127.0.0.1:8001/creation/references', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify(payload),
    })
  }

  const addFiles = async (files: Iterable<File>, mentionImages = false) => {
    setAttachmentError(null)
    try {
      const next = await filesToAttachments(files, attachments.length)
      setAttachments(prev => [...prev, ...next])
      if (mentionImages) {
        const imageMentions = next
          .filter(item => item.type.startsWith('image/'))
          .map(item => `@${item.name}`)
        if (imageMentions.length) {
          const separator = prompt && !/\s$/.test(prompt) ? ' ' : ''
          setPrompt(`${prompt}${separator}${imageMentions.join(' ')} `)
          window.requestAnimationFrame(() => promptInputRef.current?.focus())
        }
      }
    } catch (err) {
      setAttachmentError(toUserFacingError(err, '附件读取失败'))
    }
  }

  const startCreationHistory = async (
    userMessage: string,
    chat: CreationChatMessage[],
    activeSessionId: string,
  ) => {
    try {
      const state = useAppStore.getState().creationDraft
      const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/history/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: userMessage,
          session_id: activeSessionId,
          generated_content: state.generatedContent,
          doc_type: docType || null,
          audience: audience || null,
          root_request: state.rootRequest || userMessage,
          conversation: chat,
          model: useGatewayCreation && currentUser?.id
            ? REMOTE_CREATION_MODEL_ID
            : LOCAL_CREATION_MODEL_ID,
          creation_mode: state.creationMode,
          creation_brief: state.brainstormState,
          brainstorm_revision: state.brainstormState?.revision ?? null,
        }),
      })
      if (!response.ok) return
      const saved = await response.json() as { id?: number; progress_epoch?: number }
      const historyId = Number(saved.id)
      if (!Number.isSafeInteger(historyId)) return
      activeHistoryIdRef.current = historyId
      setActiveHistoryId(historyId)
      const progressEpoch = Number(saved.progress_epoch)
      activeHistoryEpochRef.current = Number.isSafeInteger(progressEpoch)
        ? progressEpoch
        : null
      setCurrentDocumentSource({
        kind: 'creation_history',
        id: String(historyId),
        title: state.rootRequest || userMessage,
        content: state.generatedContent,
        docType,
      })
      if (historyPage === 1) void loadCreationHistory()
      else setHistoryPage(1)
    } catch (startErr) {
      // 历史记录不可用不阻断创作主链路；完成保存仍会再次尝试落库。
      console.warn('建立进行中创作记录失败:', startErr)
    }
  }

  const postBrainstormTurn = async (payload: Record<string, unknown>) => {
    const controller = new AbortController()
    brainstormAbortRef.current?.abort()
    brainstormAbortRef.current = controller
    try {
      const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/brainstorm/turn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        // 每一轮都携带当前执行 Skill。Core 只在旧会话缺少 Skill 上下文时
        // 补写，因此恢复或继续历史脑暴也能获得章节覆盖图。
        body: JSON.stringify({
          selected_skills: selectedBrainstormSkillPayload(),
          ...payload,
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as { code?: string; message?: string }
        const failure = new Error(body.message || '脑暴进度更新失败') as Error & { code?: string }
        failure.code = body.code
        throw failure
      }
      const body = await response.json() as CreationBrainstormState
      const normalized = normalizeBrainstormState(body)
      if (!normalized) throw new Error('脑暴状态为空')
      // 脑暴请求成功说明用户的重试已经恢复。正式生成与脑暴使用两个独立
      // 错误状态，若只清理卡片错误，之前的通用“生成失败”仍会残留在输入框
      // 下方，与新问题同时出现。
      setError(null)
      setBrainstormError(null)
      return normalized
    } finally {
      if (brainstormAbortRef.current === controller) brainstormAbortRef.current = null
    }
  }

  const restoreBrainstormSession = async (
    targetSessionId: string,
    request: string,
    silent = false,
  ): Promise<CreationBrainstormState | null> => {
    setIsBrainstormLoading(true)
    setBrainstormError(null)
    try {
      const next = await postBrainstormTurn({
        session_id: targetSessionId,
        root_request: request,
        action: 'start',
        selected_skills: selectedBrainstormSkillPayload(),
      })
      setBrainstormState(next)
      setBrainstormHistoryIndex(null)
      return next
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return null
      if (!silent) setBrainstormError(toUserFacingError(err, '脑暴进度恢复失败'))
      return null
    } finally {
      setIsBrainstormLoading(false)
    }
  }

  const beginBrainstorm = async (userMessage: string) => {
    const message = userMessage.trim()
    if (!message) return
    setIsBrainstormLoading(true)
    setBrainstormError(null)
    const storedSessionId = useAppStore.getState().creationDraft.sessionId
    const activeSessionId = storedSessionId || createCreationSessionId()
    if (!storedSessionId) setSessionId(activeSessionId)
    const liveConversation = useAppStore.getState().creationDraft.conversation
    const existing = liveConversation.find(item => item.role === 'user' && item.content === message)
    const userEntry: CreationChatMessage = existing || {
      id: `user-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role: 'user',
      content: message,
      createdAt: Date.now(),
    }
    const chat = existing ? liveConversation : [...liveConversation, userEntry]
    if (!existing) setConversation(chat)
    setRootRequest(messageWithAttachments(message))
    try {
      // 脑暴与正式生成必须共享同一套提交后 Skill 路由结果。此前脑暴先启动，
      // 导致非显式 @ 的 Skill 只在成稿阶段才出现，章节结构无法指导前置问题。
      const skillResolution = await resolveExecutionSkills({
        apiBaseUrl,
        prompt: messageWithAttachments(message),
        skills: installedSkills,
      })
      turnMatchedSkillsRef.current = skillResolution.matches
      await startCreationHistory(message, chat, activeSessionId)
      if (useAppStore.getState().creationDraft.conversation.some(item => item.kind === 'session_end')) {
        await persistCreationProgress('cancelled')
        return
      }
      const next = await postBrainstormTurn({
        session_id: activeSessionId,
        root_request: messageWithAttachments(message),
        action: 'start',
        selected_skills: selectedBrainstormSkillPayload(),
      })
      setBrainstormState(next)
      setBrainstormHistoryIndex(null)
      setPrompt('')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const code = (err as Error & { code?: string }).code
      if (code === 'BRAINSTORM_SESSION_NOT_FOUND') {
        setBrainstormState(null)
      }
      setBrainstormError(toUserFacingError(err, '脑暴启动失败，请重试'))
    } finally {
      setIsBrainstormLoading(false)
    }
  }

  const submitBrainstormAnswer = async () => {
    const state = useAppStore.getState().creationDraft.brainstormState
    const historyTurn = brainstormHistoryIndex === null
      ? null
      : state?.history?.[brainstormHistoryIndex]
    const question = historyTurn?.question || state?.current_question
    if (!state || !question) return
    const usesCustomAnswer = question.allow_custom && brainstormCustomAnswerSelected
    setIsBrainstormLoading(true)
    setBrainstormError(null)
    try {
      const next = await postBrainstormTurn({
        session_id: state.session_id,
        root_request: rootRequest,
        action: historyTurn ? 'revise_answer' : 'answer',
        revision: state.revision,
        question_id: question.id,
        answer: {
          selected_option_ids: usesCustomAnswer ? [] : brainstormSelectedOptions,
          custom_text: usesCustomAnswer ? brainstormCustomAnswer.trim() : '',
        },
      })
      setBrainstormState(next)
      setBrainstormHistoryIndex(null)
      setBrainstormSelectedOptions([])
      setBrainstormCustomAnswerSelected(false)
      setBrainstormCustomAnswer('')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const code = (err as Error & { code?: string }).code
      if (code === 'BRAINSTORM_REVISION_CONFLICT' || code === 'BRAINSTORM_QUESTION_STALE') {
        await restoreBrainstormSession(state.session_id, rootRequest)
      } else {
        setBrainstormError(toUserFacingError(err, '答案未保存，请重试'))
      }
    } finally {
      setIsBrainstormLoading(false)
    }
  }

  const finishBrainstorm = async () => {
    const state = useAppStore.getState().creationDraft.brainstormState
    if (!state) return
    setIsBrainstormLoading(true)
    setBrainstormError(null)
    try {
      const next = await postBrainstormTurn({
        session_id: state.session_id,
        root_request: rootRequest,
        action: 'finish',
        revision: state.revision,
        accept_assumptions: true,
      })
      setBrainstormState(next)
      setBrainstormHistoryIndex(null)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      setBrainstormError(toUserFacingError(err, '简报收敛失败，请重试'))
    } finally {
      setIsBrainstormLoading(false)
    }
  }

  const continueBrainstorm = async () => {
    const state = useAppStore.getState().creationDraft.brainstormState
    if (!state || !brainstormContinuationDirectionId) return
    const customDirection = brainstormCustomDirection.trim()
    if (brainstormContinuationDirectionId === '__custom__' && !customDirection) return
    setIsBrainstormLoading(true)
    setBrainstormError(null)
    // 正式生成时的脑暴卡片属于历史过程。继续脑暴只更新末尾的新回合，
    // 不应把已经展示在原位置的卡片改写成新问题。
    setAnchoredBrainstormState(current => current || state)
    try {
      const next = await postBrainstormTurn({
        session_id: state.session_id,
        root_request: rootRequest,
        action: 'continue_brainstorm',
        revision: state.revision,
        continuation_direction_id: brainstormContinuationDirectionId,
        focus_hint: brainstormContinuationDirectionId === '__custom__' ? customDirection : '',
      })
      setBrainstormState(next)
      setBrainstormHistoryIndex(null)
      setBrainstormSelectedOptions([])
      setBrainstormCustomAnswerSelected(false)
      setBrainstormCustomAnswer('')
      setBrainstormContinuationOpen(false)
      setBrainstormContinuationDirectionId('')
      setBrainstormCustomDirection('')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const code = (err as Error & { code?: string }).code
      if (code === 'BRAINSTORM_REVISION_CONFLICT' || code === 'BRAINSTORM_QUESTION_STALE') {
        // 历史记录中的脑暴快照可能落后于服务端实时会话。尤其是上一次继续
        // 请求已在服务端成功、但客户端在响应返回前断开时，再次点击会携带
        // 旧 revision。此时静默恢复即可：若服务端已经进入下一题，最新对话
        // 区域会直接展示该题，不再要求用户手动刷新或重复提交方向。
        const refreshed = await restoreBrainstormSession(state.session_id, rootRequest)
        if (refreshed) {
          setBrainstormContinuationOpen(false)
          setBrainstormContinuationDirectionId('')
          setBrainstormCustomDirection('')
        }
      } else {
        setBrainstormError(toUserFacingError(err, '继续脑暴失败，请重试'))
      }
    } finally {
      setIsBrainstormLoading(false)
    }
  }

  const skipBrainstormQuestion = async () => {
    const state = useAppStore.getState().creationDraft.brainstormState
    const question = state?.current_question
    if (!state || !question) return
    setIsBrainstormLoading(true)
    setBrainstormError(null)
    try {
      const next = await postBrainstormTurn({
        session_id: state.session_id,
        root_request: rootRequest,
        action: 'skip',
        revision: state.revision,
        question_id: question.id,
      })
      setBrainstormState(next)
      setBrainstormHistoryIndex(null)
      setBrainstormSelectedOptions([])
      setBrainstormCustomAnswerSelected(false)
      setBrainstormCustomAnswer('')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      setBrainstormError(toUserFacingError(err, '当前问题跳过失败，请重试'))
    } finally {
      setIsBrainstormLoading(false)
    }
  }

  const inlineBrainstormRootRequest = (snapshot: CreationSelectionSnapshot) => [
    '请针对创作文档的局部选区进行脑暴。先帮助用户比较可行方向，通过选项逐步收敛；不要直接输出完整文档。',
    rootRequest.trim() ? `整篇文档目标：${rootRequest.trim()}` : '',
    `所选内容：\n${snapshot.selectedMarkdown}`,
    '最终目标：形成一组可安全写回当前选区的明确结论，保持选区外内容不变，不编造事实。',
  ].filter(Boolean).join('\n\n')

  const updateInlineBrainstormState = (next: CreationBrainstormState) => {
    const recommended = next.current_question?.options.find(option => option.recommended)
    const continuation = next.continuation_directions.find(direction => direction.recommended)
      || next.continuation_directions[0]
    setInlineBrainstorm(current => current ? {
      ...current,
      state: next,
      loading: false,
      error: '',
      selectedOptionIds: recommended ? [recommended.id] : [],
      customSelected: false,
      customAnswer: '',
      continuationDirectionId: continuation?.id || '',
      customDirection: '',
    } : current)
  }

  const postInlineBrainstormTurn = async (
    active: InlineBrainstormSession,
    payload: Record<string, unknown>,
  ) => {
    const controller = new AbortController()
    inlineBrainstormAbortRef.current?.abort()
    inlineBrainstormAbortRef.current = controller
    setInlineBrainstorm(current => current ? { ...current, loading: true, error: '' } : current)
    try {
      const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/brainstorm/turn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          selected_skills: selectedBrainstormSkillPayload(),
          session_id: active.sessionId,
          root_request: active.rootRequest,
          ...payload,
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as { message?: string }
        throw new Error(body.message || '局部脑暴更新失败')
      }
      const normalized = normalizeBrainstormState(await response.json() as CreationBrainstormState)
      if (!normalized) throw new Error('局部脑暴状态为空')
      updateInlineBrainstormState(normalized)
      return normalized
    } catch (brainstormError) {
      if (!(brainstormError instanceof DOMException && brainstormError.name === 'AbortError')) {
        setInlineBrainstorm(current => current ? {
          ...current,
          loading: false,
          error: toUserFacingError(brainstormError, '局部脑暴失败，请重试'),
        } : current)
      }
      return null
    } finally {
      if (inlineBrainstormAbortRef.current === controller) inlineBrainstormAbortRef.current = null
    }
  }

  const beginInlineBrainstorm = async () => {
    const snapshot = inlineSelection
    if (!snapshot || !inlineCapabilities?.enabled || inlineRunningAction || isGenerating) return
    const now = Date.now()
    const messageId = `inline-brainstorm-user-${now}`
    const brainstormSessionId = `inline-brainstorm-${sessionId || 'current'}-${globalThis.crypto.randomUUID?.() || Math.random().toString(16).slice(2)}`
    const request = inlineBrainstormRootRequest(snapshot)
    const active: InlineBrainstormSession = {
      sessionId: brainstormSessionId,
      rootRequest: request,
      anchorMessageId: messageId,
      selection: snapshot,
      state: null,
      loading: true,
      error: '',
      selectedOptionIds: [],
      customSelected: false,
      customAnswer: '',
      continuationDirectionId: '',
      customDirection: '',
      applied: false,
    }
    const excerpt = snapshot.selectedText.trim().replace(/\s+/g, ' ')
    const displayExcerpt = excerpt.length > 600 ? `${excerpt.slice(0, 600)}…` : excerpt
    setConversation([
      ...useAppStore.getState().creationDraft.conversation,
      {
        id: messageId,
        role: 'user',
        content: `脑暴要求：围绕所选内容探索可替换或补强的方向，先给出选项，确认后再修改文档\n选取内容：${displayExcerpt}`,
        createdAt: now,
        runId: brainstormSessionId,
      },
    ])
    setInlineBrainstorm(active)
    setInlineSelection(null)
    setInlinePromptOpen(false)
    setInlineCustomPrompt('')
    setInlineError('')
    window.getSelection()?.removeAllRanges()
    await postInlineBrainstormTurn(active, { action: 'start' })
  }

  const submitInlineBrainstormAnswer = async () => {
    const active = inlineBrainstorm
    const question = active?.state?.current_question
    if (!active || !question) return
    await postInlineBrainstormTurn(active, {
      action: 'answer',
      revision: active.state!.revision,
      question_id: question.id,
      answer: {
        selected_option_ids: active.customSelected ? [] : active.selectedOptionIds,
        custom_text: active.customSelected ? active.customAnswer.trim() : '',
      },
    })
  }

  const skipInlineBrainstormQuestion = async () => {
    const active = inlineBrainstorm
    const question = active?.state?.current_question
    if (!active || !question) return
    await postInlineBrainstormTurn(active, {
      action: 'skip',
      revision: active.state!.revision,
      question_id: question.id,
    })
  }

  const continueInlineBrainstorm = async () => {
    const active = inlineBrainstorm
    if (!active?.state || !active.continuationDirectionId) return
    await postInlineBrainstormTurn(active, {
      action: 'continue_brainstorm',
      revision: active.state.revision,
      continuation_direction_id: active.continuationDirectionId,
      focus_hint: active.continuationDirectionId === '__custom__' ? active.customDirection.trim() : '',
    })
  }

  const applyInlineBrainstorm = async () => {
    const active = inlineBrainstorm
    if (!active?.state || active.state.phase !== 'ready' || active.applied) return
    const conclusions = active.state.decisions
      .map(decision => `${decision.dimension}：${decision.summary}`)
      .join('\n')
    if (!conclusions.trim()) {
      setInlineBrainstorm(current => current ? { ...current, error: '还没有可应用的脑暴结论' } : current)
      return
    }
    if (new TextEncoder().encode(conclusions).length > (inlineCapabilities?.max_custom_prompt_bytes || 0)) {
      setInlineBrainstorm(current => current ? {
        ...current,
        error: '本轮脑暴结论过长，请继续收敛为更少、更明确的方向后再应用',
      } : current)
      return
    }
    await runInlineEdit('brainstorm', active.selection, conclusions)
  }

  const retryInlineBrainstorm = async () => {
    const active = inlineBrainstorm
    if (!active) return
    if (!active.state) {
      await postInlineBrainstormTurn(active, { action: 'start' })
    } else if (active.state.current_question) {
      await submitInlineBrainstormAnswer()
    } else if (active.state.phase === 'ready') {
      await continueInlineBrainstorm()
    }
  }

  const reopenBrainstormDecision = (questionId: string) => {
    const state = useAppStore.getState().creationDraft.brainstormState
    if (!state) return
    setBrainstormError(null)
    const index = (state.history || []).findIndex(item => item.question.id === questionId)
    if (index >= 0) setBrainstormHistoryIndex(index)
  }

  const persistTerminalProgressFallback = async (
    historyId: number | null,
    lifecycleStatus: 'completed' | 'failed' | 'cancelled',
    latencyMs?: number | null,
  ) => {
    const state = useAppStore.getState().creationDraft
    const references = state.referencePreview?.references || []
    const latestRunId = [...state.agentEvents].reverse().find(item => item.run_id)?.run_id
    const latestGoal = [...state.agentEvents].reverse().find(item => item.goal)?.goal || null
    const latestDocumentEvent = [...state.agentEvents].reverse().find(item => (
      ['document.patch.applied', 'document.replaced'].includes(item.type)
      && (!latestRunId || item.run_id === latestRunId)
    ))
    const documentPatch = latestDocumentEvent?.type === 'document.patch.applied'
      ? latestDocumentEvent.data?.patch
      : null
    const prompt = activeUserMessageRef.current || state.rootRequest || '继续创作'
    const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/history`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt,
        generated_content: sanitizeGeneratedContent(state.generatedContent),
        doc_type: docType || null,
        audience: audience || null,
        reference_count: references.length,
        references,
        model: useGatewayCreation && currentUser?.id
          ? REMOTE_CREATION_MODEL_ID
          : LOCAL_CREATION_MODEL_ID,
        latency_ms: latencyMs ?? null,
        session_id: state.sessionId,
        history_id: historyId,
        root_request: state.rootRequest || prompt,
        conversation: state.conversation,
        agent_trace: state.agentEvents.map(toStoredAgentEvent),
        goal: latestGoal,
        edit_operation: intentOperationForRun(state.agentEvents, latestRunId)
          || (state.generatedContent.trim() ? 'rewrite_document' : 'create_document'),
        document_patch: documentPatch || null,
        evidence: [],
        lifecycle_status: lifecycleStatus,
        creation_mode: state.creationMode,
        creation_brief: state.brainstormState,
        brainstorm_revision: state.brainstormState?.revision ?? null,
      }),
    })
    if (!response.ok) {
      throw new Error(`创作终态兜底保存失败: ${response.status}`)
    }
    const saved = await response.json() as { id?: number }
    const savedId = Number(saved.id)
    if (Number.isSafeInteger(savedId)) {
      activeHistoryIdRef.current = savedId
      setActiveHistoryId(savedId)
    }
  }

  const persistCreationProgress = async (
    lifecycleStatus: 'running' | 'completed' | 'failed' | 'cancelled',
    latencyMs?: number | null,
  ) => {
    const historyId = activeHistoryIdRef.current
    if (!historyId) {
      if (lifecycleStatus !== 'running') {
        try {
          await persistTerminalProgressFallback(null, lifecycleStatus, latencyMs)
          if (historyPage === 1) void loadCreationHistory()
        } catch (fallbackErr) {
          console.warn('创作终态兜底保存失败:', fallbackErr)
        }
      }
      return
    }
    const state = useAppStore.getState().creationDraft
    try {
      const response = await fetchWithLocalhostFallback(
        `${apiBaseUrl}/api/creation/history/${historyId}/progress`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            lifecycle_status: lifecycleStatus,
            progress_epoch: activeHistoryEpochRef.current,
            generated_content: state.generatedContent,
            conversation: state.conversation,
            agent_trace: state.agentEvents.map(toStoredAgentEvent),
            latency_ms: latencyMs ?? null,
          }),
        },
      )
      if (!response.ok) {
        throw new Error(`创作进度保存失败: ${response.status}`)
      }
      if (historyPage === 1) void loadCreationHistory()
    } catch (progressErr) {
      console.warn('保存创作进度失败:', progressErr)
      if (lifecycleStatus !== 'running') {
        try {
          await persistTerminalProgressFallback(historyId, lifecycleStatus, latencyMs)
          if (historyPage === 1) void loadCreationHistory()
        } catch (fallbackErr) {
          console.warn('创作终态兜底保存失败:', fallbackErr)
        }
      }
    }
  }

  const persistCreationResult = async (
    userMessage: string,
    content: string,
    chat: CreationChatMessage[],
    usedModelId: string,
    latencyMs: number | null,
    lifecycleStatus: 'completed' | 'failed' = 'completed',
  ) => {
    const state = useAppStore.getState().creationDraft
    const references = state.referencePreview?.references || []
    const latestCompletedRunId = [...state.agentEvents]
      .reverse()
      .find(item => item.type === 'run.completed' && item.status === 'completed')
      ?.run_id
    const latestDocumentEvent = [...state.agentEvents]
      .reverse()
      .find(item => (
        ['document.patch.applied', 'document.replaced'].includes(item.type)
        && (!latestCompletedRunId || item.run_id === latestCompletedRunId)
      ))
    const intentOperation = intentOperationForRun(state.agentEvents, latestCompletedRunId)
    const isInitialCreation = intentOperation === 'create_document'
    const documentPatch = latestDocumentEvent?.type === 'document.patch.applied'
      ? latestDocumentEvent.data?.patch
      : null
    const editOperation = String(
      intentOperation
      || (documentPatch as Record<string, unknown> | undefined)?.operation
      || latestDocumentEvent?.data?.operation
      || (currentDocumentSource ? 'rewrite_document' : 'create_document'),
    )
    const sourceHistoryId = currentDocumentSource?.kind === 'creation_history'
      ? Number(currentDocumentSource.id)
      : Number.NaN
    const evidence = ([...state.agentEvents]
      .reverse()
      .find(item => (
        item.type === 'run.completed'
        && item.status === 'completed'
        && (!latestCompletedRunId || item.run_id === latestCompletedRunId)
      ))
      ?.data?.evidence || []) as CreationEvidenceItem[]
    try {
      const saveResponse = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/history`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: userMessage,
          generated_content: sanitizeGeneratedContent(content),
          doc_type: docType || null,
          audience: audience || null,
          reference_count: references.length,
          references,
          model: usedModelId,
          latency_ms: latencyMs,
          session_id: state.sessionId,
          history_id: Number.isSafeInteger(sourceHistoryId) ? sourceHistoryId : null,
          root_request: state.rootRequest || userMessage,
          conversation: chat,
          agent_trace: state.agentEvents.map(toStoredAgentEvent),
          goal: [...state.agentEvents].reverse().find(item => item.goal)?.goal || null,
          edit_operation: editOperation,
          document_patch: documentPatch || null,
          evidence: Array.isArray(evidence) ? evidence : [],
          lifecycle_status: lifecycleStatus,
          creation_mode: state.creationMode,
          creation_brief: state.brainstormState,
          brainstorm_revision: state.brainstormState?.revision ?? null,
        }),
      })
      if (saveResponse.ok) {
        const saved = await saveResponse.json()
        activeHistoryIdRef.current = Number(saved.id) || activeHistoryIdRef.current
        setActiveHistoryId(activeHistoryIdRef.current)
        const savedTitle = state.rootRequest || userMessage
        setCurrentDocumentSource({
          kind: 'creation_history',
          id: String(saved.id),
          title: savedTitle,
          content: sanitizeGeneratedContent(content),
          docType,
        })
      } else {
        await persistCreationProgress(lifecycleStatus, latencyMs)
      }
      if (historyPage === 1) void loadCreationHistory()
      else setHistoryPage(1)
    } catch (saveErr) {
      console.error('保存创作记录失败:', saveErr)
      await persistCreationProgress(lifecycleStatus, latencyMs)
    }
  }

  const appendAssistantCompletion = (
    chat: CreationChatMessage[],
    runId: string | null,
  ) => {
    const activeUserEntry = activeUserEntryRef.current
    const ensuredChat = activeUserEntry && !chat.some(item => item.id === activeUserEntry.id)
      ? [...chat, activeUserEntry]
      : chat
    const latestMutation = [...useAppStore.getState().creationDraft.agentEvents]
      .reverse()
      .find(item => (
        ['document.patch.applied', 'document.replaced'].includes(item.type)
        && (!runId || item.run_id === runId)
      ))
    const intentOperation = intentOperationForRun(
      useAppStore.getState().creationDraft.agentEvents,
      runId,
    )
    const isInitialCreation = intentOperation === 'create_document'
    const patch = !isInitialCreation && latestMutation?.type === 'document.patch.applied'
      ? latestMutation.data?.patch as Record<string, unknown> | undefined
      : undefined
    const targets = Array.isArray(patch?.target_sections)
      ? patch.target_sections.map(item => String(item)).filter(Boolean)
      : []
    const patchSummary = patch
      ? String(patch.summary || `文档已完成${targets.length ? `“${targets.join('、')}”相关` : ''}修订`)
        .replace(/[。.!！]+$/, '')
      : ''
    const assistant: CreationChatMessage = {
      id: `assistant-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role: 'assistant',
      content: isInitialCreation
        ? '首版文档已生成。你可以继续提出修改要求，我会基于当前版本继续优化。'
        : patch
          ? `${patchSummary}。你可以继续修改。`
          : '文档已更新。你可以继续提出修改要求，我会基于当前版本继续优化。',
      createdAt: Date.now(),
      runId: runId || undefined,
    }
    const next = [...ensuredChat, assistant]
    setConversation(next)
    return next
  }

  const runLegacyGeneration = async (
    userMessage: string,
    chat: CreationChatMessage[],
    controller: AbortController,
  ) => {
    let referencesForHistory: CreationReferenceItem[] = []
    if (memorySearchEnabled) {
      try {
        const refResponse = await postReferencePreview(userMessage, controller.signal)
        if (refResponse.ok) {
          const refData = await refResponse.json()
          setReferencePreview(refData)
          referencesForHistory = Array.isArray(refData?.references) ? refData.references : []
        }
      } catch (refErr) {
        console.warn('参考资料同步加载失败，继续生成:', refErr)
      }
    }
    const startedAt = Date.now()
    let usedModelId = activeCreationModelId
    let content = ''
    if (useGatewayCreation && currentUser?.id) {
      const data = await postGatewayCreation(referencesForHistory, userMessage, controller.signal)
      content = sanitizeGeneratedContent(String(data.content || ''))
    } else {
      usedModelId = LOCAL_CREATION_MODEL_ID
      content = await postLocalCreation(userMessage, controller.signal)
    }
    if (!content.trim()) throw new Error('生成结束但没有返回内容')
    setGeneratedContent(content)
    const latencyMs = Date.now() - startedAt
    setLastInferenceMeta({ model: usedModelId, latencyMs })
    const finalChat = appendAssistantCompletion(chat, null)
    await persistCreationResult(userMessage, content, finalChat, usedModelId, latencyMs)
  }

  const runAgentTurn = async ({
    userMessage,
    confirmed = false,
    appendUser = true,
  }: {
    userMessage: string
    confirmed?: boolean
    appendUser?: boolean
  }) => {
    if (inlineRunningAction) return
    const message = userMessage.trim()
    if (!message) return
    setInlineUndo(null)
    setInlineSelection(null)
    const storedSessionId = useAppStore.getState().creationDraft.sessionId
    const activeSessionId = storedSessionId || createCreationSessionId()
    if (!storedSessionId) setSessionId(activeSessionId)
    activeUserMessageRef.current = message
    const liveConversation = useAppStore.getState().creationDraft.conversation
    const existingUserEntry = !appendUser
      ? liveConversation.find(item => item.id === activeUserEntryRef.current?.id)
        || [...liveConversation].reverse().find(item => (
          item.role === 'user' && item.content.trim() === message
        ))
      : undefined
    const userEntry: CreationChatMessage = existingUserEntry || {
      id: `user-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role: 'user',
      content: message,
      createdAt: Date.now(),
    }
    activeUserEntryRef.current = userEntry
    const chat = appendUser
      ? [...liveConversation, userEntry]
      : existingUserEntry
        ? liveConversation
        : [...liveConversation, userEntry]
    if (appendUser || !existingUserEntry) setConversation(chat)
    const liveRootRequest = useAppStore.getState().creationDraft.rootRequest
    if (!liveRootRequest.trim()) {
      setRootRequest(
        chat.find(item => item.role === 'user')?.content
        || messageWithAttachments(message),
      )
    }
    setPendingConfirmation(null)
    setIsGenerating(true)
    setError(null)
    setLastInferenceMeta(null)
    const controller = new AbortController()
    abortRef.current = controller
    startTimer()
    const startedAt = Date.now()
    await startCreationHistory(message, chat, activeSessionId)
    if (controller.signal.aborted) {
      await persistCreationProgress('cancelled', Date.now() - startedAt)
      if (abortRef.current === controller) abortRef.current = null
      setIsGenerating(false)
      stopTimer()
      return
    }

    try {
      // 执行时自动解析技能：显式 @ 优先；否则由 sidecar 模型路由依据 Skill 自描述
      // 决策，模型不可用或降级时退回词级证据 + 意图门控的确定性匹配，避免明明
      // 命中的技能在执行前被静默丢掉。解析也必须纳入统一失败收口，
      // 否则解析异常会跳过 finally，使页面一直保留生成态。
      const skillResolution = await resolveExecutionSkills({
        apiBaseUrl,
        prompt: messageWithAttachments(message),
        skills: installedSkills,
        signal: controller.signal,
      })
      turnMatchedSkillsRef.current = skillResolution.matches

      let payload = buildAgentPayload(message, chat, {
        session_id: activeSessionId,
        confirmed,
      })
      let finalRunId: string | null = null
      while (true) {
        const phase = await postAgentPhase(payload, controller.signal)
        if (phase.sessionId) setSessionId(phase.sessionId)
        finalRunId = phase.runId || finalRunId
        await persistCreationProgress('running', Date.now() - startedAt)
        if (phase.pausedForConfirmation) return
        if (phase.modelMessages) {
          if (!phase.continuation) throw new Error('创作 Agent 缺少恢复状态')
          const modelResult = await postGatewayAgentCall(
            phase.modelMessages,
            phase.modelRequestId,
            controller.signal,
          )
          payload = buildAgentPayload(message, chat, {
            session_id: phase.sessionId,
            run_id: phase.runId,
            resume_state: phase.continuation,
            model_result: modelResult,
            confirmed: true,
          })
          continue
        }
        if (!phase.completed) throw new Error('创作 Agent 未完成，也没有返回可恢复动作')
        break
      }

      const finalContent = useAppStore.getState().creationDraft.generatedContent
      if (!finalContent.trim()) throw new Error('创作 Agent 完成但没有生成文档')
      const usedModelId = useGatewayCreation && currentUser?.id
        ? REMOTE_CREATION_MODEL_ID
        : LOCAL_CREATION_MODEL_ID
      const latencyMs = Date.now() - startedAt
      setLastInferenceMeta({ model: usedModelId, latencyMs })
      const currentConversation = useAppStore.getState().creationDraft.conversation
      const finalChat = appendAssistantCompletion(currentConversation, finalRunId)
      await persistCreationResult(message, finalContent, finalChat, usedModelId, latencyMs)
      setPrompt('')
      setAttachments([])
    } catch (err) {
      const code = (err as Error & { code?: string })?.code
      if (code === 'CREATION_AGENT_NOT_AVAILABLE') {
        await runLegacyGeneration(message, chat, controller)
        setPrompt('')
        return
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        // 点击中止时已经把行为写入对话与执行轨迹，这里只结束异步流程。
        return
      }
      // SSE 之后还可能在网关模型调用、恢复请求等环节失败。这些异常
      // 不会由 sidecar 再发 run.failed，因此前端要为当前用户消息关联的 run
      // 补齐幂等终态，避免阶段、深度思考和呼吸灯永久停在运行中。
      const failedState = useAppStore.getState().creationDraft
      const failedUserEntry = failedState.conversation.find(item => item.id === userEntry.id)
      const failedRunIds = new Set([
        ...(failedUserEntry?.runIds || []),
        ...(failedUserEntry?.runId ? [failedUserEntry.runId] : []),
      ])
      const failedRunId = [...failedState.agentEvents]
        .reverse()
        .find(item => failedRunIds.has(item.run_id))
        ?.run_id
      const failedRunEvents = failedRunId
        ? failedState.agentEvents.filter(item => item.run_id === failedRunId)
        : []
      if (failedRunId && !failedRunEvents.some(isRunTerminalEvent)) {
        const now = Date.now()
        const latestGoal = [...failedRunEvents].reverse().find(item => item.goal)?.goal
        const failureCode = String(
          (err as Error & { code?: string; errorCode?: string }).errorCode
          || (err as Error & { code?: string }).code
          || 'CLIENT_EXECUTION_FAILED',
        )
        setAgentEvents([...failedState.agentEvents, {
          schema_version: 'creation.agent.v1',
          event_id: `client-failed-${now}`,
          session_id: failedState.sessionId || activeSessionId,
          run_id: failedRunId,
          sequence: Math.max(0, ...failedRunEvents.map(event => Number(event.sequence) || 0)) + 1,
          timestamp: now,
          type: 'run.failed',
          status: 'failed',
          actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
          summary: '生成失败，本轮创作已停止',
          goal: latestGoal
            ? { ...latestGoal, status: 'failed', outcome: '生成失败，本轮创作已停止' }
            : undefined,
          environment_patch: {},
          data: {
            error_code: failureCode,
            retryable: Boolean((err as Error & { retryable?: boolean }).retryable),
          },
        }])
      }
      await persistCreationProgress('failed', Date.now() - startedAt)
      if (!controller.signal.aborted) controller.abort()
      setPendingConfirmation(null)
      // 保全部分成果：中断时若已组装出文档（如模型连接在后续步骤被掐断），
      // 先把已生成部分落库，避免中断前完成的章节随失败一起丢失。
      const partialDocument = sanitizeGeneratedContent(
        String((err as Error & { partialDocument?: string })?.partialDocument || ''),
      )
      if (partialDocument.trim()) {
        try {
          const usedModelId = useGatewayCreation && currentUser?.id
            ? REMOTE_CREATION_MODEL_ID
            : LOCAL_CREATION_MODEL_ID
          const latencyMs = Date.now() - startedAt
          const currentConversation = useAppStore.getState().creationDraft.conversation
          await persistCreationResult(
            message,
            partialDocument,
            currentConversation,
            usedModelId,
            latencyMs,
            'failed',
          )
          setError('创作中断，已保存已生成部分，可重试继续')
          return
        } catch (persistErr) {
          console.warn('创作中断后保存部分成果失败:', persistErr)
        }
      }
      setError(toUserFacingError(err, '生成失败，请稍后重试'))
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setIsGenerating(false)
      stopTimer()
    }
  }

  const handleGenerate = async () => {
    if (inlineRunningAction) return
    if (useAppStore.getState().creationDraft.conversation.some(item => item.kind === 'session_end')) return
    if (creationMode === 'brainstorm') {
      if (!brainstormState) {
        await beginBrainstorm(prompt)
        return
      }
      if (brainstormState.phase === 'ready') {
        const liveDraft = useAppStore.getState().creationDraft
        const hasGeneratedDocument = Boolean(liveDraft.generatedContent.trim())
        setAnchoredBrainstormState(current => current || brainstormState)
        await runAgentTurn({
          userMessage: hasGeneratedDocument
            ? CONTINUE_DOCUMENT_FROM_BRAINSTORM_PROMPT
            : rootRequest || prompt,
          appendUser: hasGeneratedDocument,
        })
      }
      return
    }
    const userMessage = prompt
    if (generatedContent.trim()) setPrompt('')
    await runAgentTurn({ userMessage })
  }

  // 创作 Loop 运行在桌面页面进程内。应用重启后，数据库里的 running 记录仍在，
  // 但旧 SSE / 模型请求已经不存在；启动时自动认领最近一条手动创作记录，沿用
  // 同一会话、对话和已生成文档重新拉起。先把旧 run 按中断收口，避免恢复后旧的
  // 阶段和新 run 同时显示为“进行中”。定时任务由独立 executor 恢复，不在这里抢占。
  useEffect(() => {
    if (
      startupRecoveryAttemptedRef.current
      || historyLoading
      || !skillsLoaded
      || skillsLoading
      || isGenerating
    ) return

    startupRecoveryAttemptedRef.current = true
    const interrupted = creationHistory.find(item => (
      item.sourceKind === 'creation'
      && item.creationMode === 'direct'
      && item.lifecycleStatus === 'running'
      && Boolean(item.sessionId)
      && !terminalEventForLatestRun(item.agentEvents)
    ))
    if (!interrupted) return

    const recoveryMessage = [...interrupted.conversation]
      .reverse()
      .find(item => item.role === 'user' && item.kind !== 'user_abort')
      ?.content
      .trim() || interrupted.prompt.trim()
    if (!recoveryMessage) return

    handleRestoreHistory({
      ...interrupted,
      agentEvents: closeInterruptedHistoricalRuns(
        interrupted.agentEvents,
        interrupted.sessionId || `history-${interrupted.id}`,
      ),
    })
    setTopTab('creation')
    void runAgentTurn({ userMessage: recoveryMessage, appendUser: false })
  }, [creationHistory, historyLoading, isGenerating, skillsLoaded, skillsLoading])

  const handleConfirmContinue = async () => {
    if (
      !pendingConfirmation
      || useAppStore.getState().creationDraft.conversation.some(item => item.kind === 'session_end')
    ) return
    const message = pendingConfirmation.userMessage
    setPendingConfirmation(null)
    await runAgentTurn({ userMessage: message, confirmed: true, appendUser: false })
  }

  const handleStopGenerate = () => {
    const controller = abortRef.current
    if (!controller || controller.signal.aborted) return

    const state = useAppStore.getState().creationDraft
    const now = Date.now()
    const activeUserEntry = activeUserEntryRef.current
    const activeUserMessage = activeUserEntry
      ? state.conversation.find(item => item.id === activeUserEntry.id)
      : [...state.conversation].reverse().find(item => (
        item.role === 'user' && item.kind !== 'user_abort'
      ))
    const activeRunIds = new Set([
      ...(activeUserMessage?.runIds || []),
      ...(activeUserMessage?.runId ? [activeUserMessage.runId] : []),
    ])
    const latestRunEvent = [...state.agentEvents]
      .reverse()
      .find(item => activeRunIds.has(item.run_id))
    const runId = latestRunEvent?.run_id
      || [...activeRunIds].reverse()[0]
      || `cancelled-${now}`
    const runEvents = state.agentEvents.filter(item => item.run_id === runId)

    if (!runEvents.some(event => event.type === 'run.cancelled')) {
      const latestGoal = [...runEvents].reverse().find(event => event.goal)?.goal
      setAgentEvents([...state.agentEvents, {
        schema_version: 'creation.agent.v1',
        event_id: `user-cancelled-${now}`,
        session_id: state.sessionId || 'current',
        run_id: runId,
        sequence: Math.max(0, ...runEvents.map(event => Number(event.sequence) || 0)) + 1,
        timestamp: now,
        type: 'run.cancelled',
        status: 'cancelled',
        actor: { kind: 'user', id: 'current_user', name: userDisplayName },
        summary: '用户已中止，本轮创作结束',
        goal: latestGoal ? { ...latestGoal, status: 'cancelled' } : undefined,
        environment_patch: {},
        data: { reason: 'user_requested' },
      }])
    }

    const conversationWithRun = state.conversation.map(item => (
      activeUserMessage && item.id === activeUserMessage.id
        ? {
          ...item,
          runIds: [...new Set([...(item.runIds || []), ...(item.runId ? [item.runId] : []), runId])],
        }
        : item
    ))
    if (!conversationWithRun.some(item => item.kind === 'user_abort' && item.runId === runId)) {
      setConversation([...conversationWithRun, {
        id: `user-abort-${now}`,
        role: 'user',
        kind: 'user_abort',
        content: '中止了本次创作',
        createdAt: now,
        runId,
      }])
    }

    controller.abort()
    void persistCreationProgress('cancelled', elapsedSeconds * 1000)
    setIsGenerating(false)
    stopTimer()
    setError(null)
  }

  const hasActiveSession = Boolean(
    prompt.trim()
    || generatedContent.trim()
    || referencePreview
    || dataReferences.length
    || sessionId
    || conversation.length
    || agentEvents.length
    || attachments.length
    || pendingConfirmation
    || brainstormState,
  )
  const sessionTerminated = conversation.some(item => item.kind === 'session_end')

  const handleTerminateSession = () => {
    if (!hasActiveSession || sessionTerminated) return

    if (abortRef.current && !abortRef.current.signal.aborted) abortRef.current.abort()
    brainstormAbortRef.current?.abort()
    inlineBrainstormAbortRef.current?.abort()
    setInlineBrainstorm(current => current ? {
      ...current,
      loading: false,
      error: '当前会话已终止，局部脑暴未再继续',
    } : current)

    const state = useAppStore.getState().creationDraft
    const now = Date.now()
    const latestRunId = [...state.agentEvents].reverse().find(event => event.run_id)?.run_id
    const latestRunEvents = latestRunId
      ? state.agentEvents.filter(event => event.run_id === latestRunId)
      : []
    const nextEvents = [...state.agentEvents]
    if (latestRunId && !latestRunEvents.some(isRunTerminalEvent)) {
      const latestGoal = [...latestRunEvents].reverse().find(event => event.goal)?.goal
      nextEvents.push({
        schema_version: 'creation.agent.v1',
        event_id: `session-terminated-${now}`,
        session_id: state.sessionId || 'current',
        run_id: latestRunId,
        sequence: Math.max(0, ...latestRunEvents.map(event => Number(event.sequence) || 0)) + 1,
        timestamp: now,
        type: 'run.cancelled',
        status: 'cancelled',
        actor: { kind: 'user', id: 'current_user', name: userDisplayName },
        summary: '用户已终止当前会话',
        goal: latestGoal ? { ...latestGoal, status: 'cancelled' } : undefined,
        environment_patch: {},
        data: { reason: 'session_terminated' },
      })
      setAgentEvents(nextEvents)
    }

    setConversation([...state.conversation, {
      id: `session-end-${now}`,
      role: 'user',
      kind: 'session_end',
      content: '终止了当前会话',
      createdAt: now,
      runId: latestRunId,
    }])

    const activeBrainstorm = state.brainstormState
    if (activeBrainstorm) {
      setBrainstormState({
        ...activeBrainstorm,
        phase: 'abandoned',
        current_question: null,
        can_continue_brainstorm: false,
        continuation_directions: [],
        readiness_reason: '当前会话已由用户终止',
      })
      if (activeBrainstorm.phase !== 'abandoned') {
        void postBrainstormTurn({
          session_id: activeBrainstorm.session_id,
          root_request: rootRequest,
          action: 'abandon',
          revision: activeBrainstorm.revision,
        }).catch(() => {
          // 本地终止立即生效；服务端状态同步失败不应重新打开会话。
        })
      }
    }

    setPendingConfirmation(null)
    setIsBrainstormLoading(false)
    setBrainstormHistoryIndex(null)
    setBrainstormSelectedOptions([])
    setBrainstormCustomAnswerSelected(false)
    setBrainstormCustomAnswer('')
    setBrainstormContinuationOpen(false)
    setBrainstormError(null)
    setIsGenerating(false)
    stopTimer()
    setError(null)
    void persistCreationProgress('cancelled', elapsedSeconds * 1000)
  }

  const handleNewConversation = () => {
    if ((!sessionTerminated && (isGenerating || isBrainstormLoading || pendingConfirmation)) || !hasActiveSession) return

    brainstormAbortRef.current?.abort()
    inlineAbortRef.current?.abort()
    inlineBrainstormAbortRef.current?.abort()
    setInlineUndo(null)
    setInlineSelection(null)
    setInlineBrainstorm(null)
    setInlinePromptOpen(false)
    setInlineCustomPrompt('')
    setInlineError('')
    activeUserMessageRef.current = ''
    activeUserEntryRef.current = null
    legacyDataRecoveryRef.current += 1
    setDataSourcesById({})
    setDataReferencesLoading(false)
    setDataReferencesError('')
    setLegacyDataReferencesRecovered(false)
    setCreationDraft({
      prompt: '',
      generatedContent: '',
      referencePreview: null,
      dataReferences: [],
      sessionId: null,
      rootRequest: '',
      conversation: [],
      agentEvents: [],
      creationMode: 'direct',
      brainstormState: null,
    })
    setBrainstormSelectedOptions([])
    setBrainstormCustomAnswerSelected(false)
    setBrainstormCustomAnswer('')
    setBrainstormHistoryIndex(null)
    setBrainstormContinuationOpen(false)
    setBrainstormContinuationDirectionId('')
    setBrainstormCustomDirection('')
    setAnchoredBrainstormState(null)
    setBrainstormError(null)
    setAttachments([])
    setAttachmentError(null)
    setCurrentDocumentSource(null)
    setCurrentDocumentSkills([])
    turnMatchedSkillsRef.current = null
    setPendingConfirmation(null)
    setSkillPickerOpen(false)
    setSkillQuery('')
    setError(null)
    setCopySuccess(false)
    setLastInferenceMeta(null)
    setElapsedSeconds(0)
    setActiveBottomTab(null)
    activeUserMessageRef.current = ''
    activeHistoryIdRef.current = null
    setActiveHistoryId(null)
    activeHistoryEpochRef.current = null
    if (contentRef.current) contentRef.current.scrollTop = 0
    window.requestAnimationFrame(() => promptInputRef.current?.focus())
  }

  const handleCopy = async () => {
    await navigator.clipboard.writeText(stripInternalCreationMarkers(generatedContent))
    setCopySuccess(true)
    setTimeout(() => setCopySuccess(false), 2000)
  }

  useEffect(() => {
    const container = contentRef.current
    if (documentSelectionLockedRef.current || !container) return

    const inlineAnchor = inlineViewportAnchorRef.current
    if (inlineAnchor) {
      const blocks = [...container.querySelectorAll<HTMLElement>('[data-md-start][data-md-end]')]
      const target = blocks.find((element) => {
        const start = Number(element.dataset.mdStart)
        const end = Number(element.dataset.mdEnd)
        return Number.isFinite(start) && Number.isFinite(end)
          && start <= inlineAnchor.sourceOffset && inlineAnchor.sourceOffset < end
      })
      if (target && inlineAnchor.viewportOffset != null) {
        const targetOffset = target.getBoundingClientRect().top - container.getBoundingClientRect().top
        container.scrollTop += targetOffset - inlineAnchor.viewportOffset
      } else {
        container.scrollTop = inlineAnchor.scrollTop
      }
      inlineViewportAnchorRef.current = null
      return
    }

    container.scrollTop = container.scrollHeight
  }, [selectionStableContent, documentSelectionLockedRef])

  useEffect(() => {
    const timeline = chatTimelineRef.current
    if (!timeline) return
    const inlineViewport = inlineTimelineViewportRef.current
    if (inlineViewport) {
      timeline.scrollTop = inlineViewport.scrollTop
      inlineTimelineViewportRef.current = null
      return
    }
    timeline.scrollTop = timeline.scrollHeight
  }, [agentEvents, brainstormError, brainstormState, conversation, pendingConfirmation])

  useEffect(() => {
    if (!brainstormContinuationOpen) return undefined

    // 方向卡片由折叠态变为完整选项列表后，高度会在当前滚动位置下方增长。
    // 等连续两帧完成布局再定位到卡片顶部，避免用户只看到标题，而选项被
    // 底部输入区挡在视口之外。
    let innerFrame = 0
    const outerFrame = window.requestAnimationFrame(() => {
      innerFrame = window.requestAnimationFrame(() => {
        brainstormContinuationRef.current?.scrollIntoView?.({
          behavior: 'smooth',
          block: 'start',
        })
      })
    })

    return () => {
      window.cancelAnimationFrame(outerFrame)
      if (innerFrame) window.cancelAnimationFrame(innerFrame)
    }
  }, [brainstormContinuationOpen])

  const totalWeight = contentWeight + qualityWeight + completenessWeight + usageWeight + formatWeight + freshnessWeight
  const generationProgress = isGenerating
    ? Math.min(95, Math.max(5, Math.round((elapsedSeconds / 90) * 100)))
    : generatedContent
      ? 100
      : 0
  const latestAgentRunId = [...agentEvents].reverse().find(item => item.run_id)?.run_id
  const latestRunEvents = latestAgentRunId
    ? agentEvents.filter(item => item.run_id === latestAgentRunId)
    : []
  const latestRunHasLifecycle = latestRunEvents.some(item => item.type.startsWith('run.'))
  const latestRunCompleted = latestRunEvents.some(item => (
    item.type === 'run.completed' && item.status === 'completed'
  ))
  const latestRunIsInitialCreation = (
    intentOperationForRun(latestRunEvents, latestAgentRunId) === 'create_document'
  )
  const canDisplayLatestMutation = !latestRunIsInitialCreation && (
    latestRunCompleted || (!isGenerating && !latestRunHasLifecycle)
  )
  // 首次用户指令对应的整个 create_document run 都属于首版创作；其中的
  // 润色和局部 patch 只是内部生成过程，只有后续用户修订才展示“本轮改动”。
  const latestDocumentMutation = canDisplayLatestMutation
    ? [...agentEvents]
      .reverse()
      .find(item => (
        ['document.patch.applied', 'document.replaced'].includes(item.type)
        && (!latestAgentRunId || item.run_id === latestAgentRunId)
      ))
    : undefined
  const latestDocumentPatch = latestDocumentMutation?.type === 'document.patch.applied'
    ? latestDocumentMutation.data?.patch as Record<string, unknown> | undefined
    : undefined
  const latestPatchTargets = Array.isArray(latestDocumentPatch?.target_sections)
    ? latestDocumentPatch.target_sections.map(item => String(item)).filter(Boolean)
    : []
  const latestPatchChanges = useMemo(
    () => documentChangesFromPatch(latestDocumentPatch, selectionStableContent),
    [latestDocumentPatch, selectionStableContent],
  )
  const latestPatchChangeCount = Math.max(
    latestPatchChanges.length,
    Number(latestDocumentPatch?.change_count) || 0,
  )
  // Brainstorm drafts created by older clients may already have a persisted brief
  // but no conversation snapshot. Keep the root request visible as the first user
  // turn so the question card never replaces the instruction it is clarifying.
  const conversationForTimeline = useMemo(() => {
    if (
      !brainstormState
      || !rootRequest.trim()
      || conversation.some(message => message.role === 'user')
    ) return conversation
    return [{
      id: `brainstorm-root-request-${sessionId || brainstormState.session_id}`,
      role: 'user' as const,
      content: rootRequest,
      createdAt: 0,
    }, ...conversation]
  }, [brainstormState, conversation, rootRequest, sessionId])
  const latestUserInstruction = [...conversationForTimeline]
    .reverse()
    .find(message => message.role === 'user')
    ?.content
  const creationTimeline = buildCreationTimeline(conversationForTimeline, agentEvents)
  const currentAgentTraceKey = [...creationTimeline]
    .reverse()
    .find(item => (
      item.kind === 'trace'
      && item.events.some(event => event.status === 'running')
      && !terminalEventForLatestRun(item.events)
    ))
    ?.key
  const referenceGroups = referenceGroupsFromEvents(
    agentEvents,
    referencePreview?.references || [],
  )
  const dataReferenceGroups = dataGroupsFromEvents(agentEvents, dataReferences)
  const brainstormHistory = brainstormState?.history || []
  const brainstormHistoryTurn = brainstormHistoryIndex === null
    ? null
    : brainstormHistory[brainstormHistoryIndex] || null
  const brainstormQuestion = brainstormHistoryTurn?.question || brainstormState?.current_question || null
  const brainstormWhyNow = brainstormQuestion?.why_now || ''
  const shouldShowBrainstormWhyNow = Boolean(brainstormWhyNow)
    && !/\b(?:next_question_goal|dimension_id|required_coverage|force_continue|focus_hint)\b/i.test(brainstormWhyNow)
  const brainstormPageIndex = brainstormHistoryIndex ?? brainstormHistory.length
  const brainstormPageCount = brainstormHistory.length + (brainstormState?.current_question ? 1 : 0)
  const brainstormIsReviewingHistory = Boolean(brainstormHistoryTurn)
  const brainstormHistoryAnswerForEditing = useMemo(() => {
    if (!brainstormHistoryTurn) {
      return { usesCustomAnswer: false, selectedOptionIds: [] as string[], customText: '' }
    }
    const usesCustomAnswer = brainstormHistoryTurn.question.allow_custom
      && Boolean(brainstormHistoryTurn.answer.custom_text.trim())
    if (!usesCustomAnswer) {
      return {
        usesCustomAnswer: false,
        selectedOptionIds: brainstormHistoryTurn.answer.selected_option_ids,
        customText: brainstormHistoryTurn.answer.custom_text,
      }
    }
    const selectedLabels = brainstormHistoryTurn.answer.selected_option_ids.map(id => (
      brainstormHistoryTurn.question.options.find(option => option.id === id)?.label || id
    ))
    return {
      usesCustomAnswer: true,
      selectedOptionIds: [] as string[],
      customText: [...selectedLabels, brainstormHistoryTurn.answer.custom_text.trim()]
        .filter(Boolean)
        .join('；'),
    }
  }, [
    brainstormHistoryTurn?.answer.custom_text,
    brainstormHistoryTurn?.answer.selected_option_ids,
    brainstormHistoryTurn?.question.allow_custom,
    brainstormHistoryTurn?.question.id,
    brainstormHistoryTurn?.question.options,
  ])
  const brainstormUsesCustomAnswer = Boolean(
    brainstormQuestion?.allow_custom && brainstormCustomAnswerSelected,
  )
  const brainstormSubmittedOptionIds = brainstormUsesCustomAnswer ? [] : brainstormSelectedOptions
  const brainstormSubmittedCustomAnswer = brainstormUsesCustomAnswer ? brainstormCustomAnswer.trim() : ''
  const brainstormHistoryAnswerChanged = Boolean(brainstormHistoryTurn && (
    brainstormSubmittedCustomAnswer !== brainstormHistoryAnswerForEditing.customText.trim()
    || brainstormSubmittedOptionIds.length !== brainstormHistoryAnswerForEditing.selectedOptionIds.length
    || brainstormSubmittedOptionIds.some(id => !brainstormHistoryAnswerForEditing.selectedOptionIds.includes(id))
  ))
  const brainstormAnswerValid = Boolean(brainstormQuestion && (
    brainstormUsesCustomAnswer
      ? brainstormCustomAnswer.trim()
      : brainstormSelectedOptions.length
  ))

  useEffect(() => {
    if (brainstormHistoryTurn) {
      setBrainstormSelectedOptions(brainstormHistoryAnswerForEditing.selectedOptionIds)
      setBrainstormCustomAnswerSelected(brainstormHistoryAnswerForEditing.usesCustomAnswer)
      setBrainstormCustomAnswer(brainstormHistoryAnswerForEditing.customText)
      return
    }
    setBrainstormSelectedOptions([])
    setBrainstormCustomAnswerSelected(false)
    setBrainstormCustomAnswer('')
  }, [
    brainstormHistoryAnswerForEditing,
    brainstormHistoryTurn,
    brainstormState?.current_question?.id,
  ])

  useEffect(() => {
    if (brainstormState?.phase === 'ready') return
    setBrainstormContinuationOpen(false)
    setBrainstormContinuationDirectionId('')
    setBrainstormCustomDirection('')
  }, [brainstormState?.phase, brainstormState?.revision])

  const handleReferenceClick = useCallback((refId: string) => {
    const normalizedId = Number(refId)
    if (!Number.isFinite(normalizedId) || normalizedId <= 0) return
    const reference = referencePreview?.references.find(item => item.id === normalizedId)
    if (reference) handleOpenReferenceSource(reference)
  }, [handleOpenReferenceSource, referencePreview])

  const headingId = useCallback((node: any) => (
    node.children.map((c: any) => c.value || '').join('').toLowerCase().replace(/\s+/g, '-')
  ), [])

  const markdownComponents = useMemo(() => ({
    h1: ({ node, children, ...props }: any) => <h1 id={headingId(node)} style={{ fontSize: 26, lineHeight: 1.25, margin: '0 0 18px' }} {...props}>{children}</h1>,
    h2: ({ node, children, ...props }: any) => <h2 id={headingId(node)} style={{ fontSize: 20, lineHeight: 1.35, margin: '24px 0 12px' }} {...props}>{children}</h2>,
    h3: ({ node, children, ...props }: any) => <h3 id={headingId(node)} style={{ color: 'var(--mb-text-primary)', fontSize: 16, fontWeight: 650, lineHeight: 1.45, margin: '20px 0 9px' }} {...props}>{children}</h3>,
    p: ({ node, ...props }: any) => <p style={{ margin: '9px 0', lineHeight: 1.75 }} {...props} />,
    li: ({ node, ...props }: any) => <li style={{ margin: '6px 0', lineHeight: 1.65 }} {...props} />,
    pre: CreationDiagramPre,
    code: CreationDiagramCode,
    strong: ({ node, ...props }: any) => (
      <strong
        style={{
          color: 'var(--mb-brand-text)',
          fontWeight: 650,
        }}
        {...props}
      />
    ),
    a: ({ node, href, children, ...props }: any) => {
      if (href?.startsWith('#ref-')) {
        const refId = href.substring(5)
        return (
          <a
            {...props}
            href={href}
            onClick={(e) => {
              e.preventDefault()
              handleReferenceClick(refId)
            }}
            style={{
              color: '#a45d22',
              textDecoration: 'underline',
              cursor: 'pointer',
              fontWeight: 500,
            }}
          >
            {children}
            <sup style={{ fontSize: '0.75em', marginLeft: 2 }}>📚</sup>
          </a>
        )
      }
      if (href?.startsWith('#')) {
        return (
          <a
            {...props}
            href={href}
            onClick={(e) => {
              e.preventDefault()
              document.getElementById(decodeURIComponent(href.substring(1)))?.scrollIntoView({ behavior: 'smooth', block: 'start' })
            }}
            style={{ color: '#a45d22', textDecoration: 'underline', cursor: 'pointer' }}
          >{children}</a>
        )
      }
      return <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: '#a45d22', textDecoration: 'underline' }} {...props}>{children}</a>
    },
    img: ({ node, src, alt, ...props }: any) => {
      const resolvedSrc = typeof src === 'string' && src.startsWith('/api/creation/evidence/')
        ? `${apiBaseUrl}${src}`
        : src
      return (
        <img
          {...props}
          src={resolvedSrc}
          alt={alt || '创作证据截图'}
          className="creation-evidence-image"
          loading="lazy"
        />
      )
    },
  }), [apiBaseUrl, handleReferenceClick, headingId])

  const hasBrainstormTimelineTurn = creationMode === 'brainstorm'
    && Boolean(brainstormState || brainstormError || isBrainstormLoading)
  const brainstormGenerationStarted = creationMode === 'brainstorm' && agentEvents.length > 0
  const brainstormCardState = brainstormGenerationStarted && anchoredBrainstormState
    ? anchoredBrainstormState
    : brainstormState
  // 正式生成后的继续脑暴属于新的对话回合，不能塞回已经锚定的旧脑暴卡片。
  // 服务端在答案提交后会把 current_question 移入 history；若这里只渲染当前
  // 问题，刚完成的问答会在 phase 回到 ready 时瞬间消失。
  const brainstormContinuationTurns = brainstormGenerationStarted && anchoredBrainstormState
    ? brainstormHistory.slice(anchoredBrainstormState.history?.length || 0)
    : []

  useEffect(() => {
    if (
      !brainstormGenerationStarted
      || brainstormState?.phase !== 'ready'
      || !brainstormState.can_continue_brainstorm
      || brainstormContinuationTurns.length === 0
    ) return

    // 继续脑暴的一题完成后，服务端可能再次收敛到 ready。直接展开下一批
    // 推荐方向，让用户明确看到既可以继续探索，也可以把新增结论写回文档。
    const recommended = brainstormState.continuation_directions.find(direction => direction.recommended)
      || brainstormState.continuation_directions[0]
    setBrainstormContinuationDirectionId(recommended?.id || '')
    setBrainstormContinuationOpen(true)
  }, [
    brainstormGenerationStarted,
    brainstormState?.can_continue_brainstorm,
    brainstormState?.revision,
    brainstormContinuationTurns.length,
  ])
  const normalizedBrainstormRootRequest = rootRequest.trim()
  const brainstormAnchorMessageId = hasBrainstormTimelineTurn
    ? conversationForTimeline.find((message) => {
      if (message.role !== 'user') return false
      const content = message.content.trim()
      if (!content || !normalizedBrainstormRootRequest) return false
      return content === normalizedBrainstormRootRequest
        || normalizedBrainstormRootRequest.startsWith(`${content}\n`)
    })?.id || conversationForTimeline.find(message => message.role === 'user')?.id
    : undefined
  const brainstormAnchorIndex = brainstormAnchorMessageId
    ? creationTimeline.findIndex(item => (
      item.kind === 'message' && item.message.id === brainstormAnchorMessageId
    ))
    : -1
  const timelineBeforeBrainstorm = brainstormAnchorIndex >= 0
    ? creationTimeline.slice(0, brainstormAnchorIndex + 1)
    : creationTimeline
  const timelineAfterBrainstorm = brainstormAnchorIndex >= 0
    ? creationTimeline.slice(brainstormAnchorIndex + 1)
    : []
  const renderCreationTimelineItem = (item: CreationTimelineItem) => {
    if (item.kind === 'trace') {
      return (
        <AgentExecutionTrace
          key={item.key}
          events={item.events}
          onOpenReferences={openBottomTab}
          browserLiveJob={browserCrawlerEnabled && item.key === currentAgentTraceKey
            ? browserLiveJobs[0]
            : undefined}
          apiBaseUrl={apiBaseUrl}
        />
      )
    }

    const timestamp = formatCreationMessageTimestamp(item.message.createdAt)
    const messageArticle = (
      <article
        className={`creation-chat-message is-${item.message.role}${['user_abort', 'session_end'].includes(item.message.kind || '') ? ' is-abort' : ''}`}
        aria-label={item.message.kind === 'session_end'
          ? '会话终止消息'
          : item.message.kind === 'user_abort'
            ? '用户中止消息'
            : item.message.role === 'user' ? '用户消息' : 'Agent 消息'}
      >
        <div className="creation-chat-message__meta">
          <span>{item.message.role === 'user' ? userDisplayName : '创作 Agent'}</span>
          {['user_abort', 'session_end'].includes(item.message.kind || '') && (
            <span className="creation-chat-message__end-badge">
              {item.message.kind === 'session_end' ? '会话已终止' : '已结束'}
            </span>
          )}
          {timestamp && (
            <time
              dateTime={timestamp.iso}
              title={`发送于 ${timestamp.full}`}
              aria-label={`发送时间：${timestamp.full}`}
            >
              {timestamp.label}
            </time>
          )}
        </div>
        <p>{item.message.content}</p>
      </article>
    )
    if (item.message.id !== inlineBrainstorm?.anchorMessageId) return React.cloneElement(messageArticle, { key: item.key })
    return (
      <React.Fragment key={item.key}>
        {messageArticle}
        <CreationInlineBrainstormCard
          state={inlineBrainstorm.state}
          loading={inlineBrainstorm.loading}
          applying={inlineRunningAction === 'brainstorm'}
          applied={inlineBrainstorm.applied}
          error={inlineBrainstorm.error}
          selectedOptionIds={inlineBrainstorm.selectedOptionIds}
          customSelected={inlineBrainstorm.customSelected}
          customAnswer={inlineBrainstorm.customAnswer}
          continuationDirectionId={inlineBrainstorm.continuationDirectionId}
          customDirection={inlineBrainstorm.customDirection}
          onOptionToggle={(optionId, singleChoice) => setInlineBrainstorm(current => current ? {
            ...current,
            selectedOptionIds: singleChoice
              ? [optionId]
              : current.selectedOptionIds.includes(optionId)
                ? current.selectedOptionIds.filter(id => id !== optionId)
                : [...current.selectedOptionIds, optionId],
            customSelected: false,
            customAnswer: '',
          } : current)}
          onCustomSelectedChange={selected => setInlineBrainstorm(current => current ? {
            ...current,
            customSelected: selected,
            selectedOptionIds: selected ? [] : current.selectedOptionIds,
          } : current)}
          onCustomAnswerChange={value => setInlineBrainstorm(current => current ? { ...current, customAnswer: value } : current)}
          onSubmitAnswer={() => void submitInlineBrainstormAnswer()}
          onSkip={() => void skipInlineBrainstormQuestion()}
          onContinuationDirectionChange={directionId => setInlineBrainstorm(current => current ? {
            ...current,
            continuationDirectionId: directionId,
          } : current)}
          onCustomDirectionChange={value => setInlineBrainstorm(current => current ? { ...current, customDirection: value } : current)}
          onContinue={() => void continueInlineBrainstorm()}
          onApply={() => void applyInlineBrainstorm()}
          onRetry={() => void retryInlineBrainstorm()}
        />
      </React.Fragment>
    )
  }

  return (
    <div
      className={`creation-panel ${className}`.trim()}
      data-active={active ? 'true' : 'false'}
      aria-hidden={!active}
      style={{ height: '100vh', display: active ? 'flex' : 'none', flexDirection: 'column', background: 'var(--mb-bg-page)', color: 'var(--mb-text-primary)' }}
    >

      {/* 顶部 Tab 栏 */}
      <div className="creation-top-tabs" style={{ display: 'flex', borderBottom: '1px solid var(--mb-border-strong)', background: 'var(--mb-bg-card)', padding: '0 22px', flexShrink: 0 }}>
        {(['creation', 'history', 'skills', 'tools'] as const).map((tab) => (
          <React.Fragment key={tab}>
            <button
              onClick={() => setTopTab(tab)}
              style={{
                padding: '12px 16px',
                border: 'none',
                borderBottom: topTab === tab ? '2px solid #a45d22' : '2px solid transparent',
                background: 'none',
                color: topTab === tab ? '#a45d22' : '#667085',
                fontWeight: topTab === tab ? 650 : 400,
                fontSize: 14,
                cursor: 'pointer',
                marginBottom: -1,
              }}
            >
              {tab === 'creation'
                ? '创作'
                : tab === 'history'
                  ? `创作记录${historyTotal ? ` (${historyTotal})` : ''}`
                  : tab === 'skills'
                    ? `技能${localSkills.length ? ` (${localSkills.length})` : ''}`
                    : `工具 (${enabledToolIds.length})`}
            </button>
            {tab === 'creation' && <TutorialLink url={TUTORIAL_URLS.creation} />}
          </React.Fragment>
        ))}
      </div>

      {topTab === 'history' ? (
        <div className="creation-history-page">
          <HistorySearch
            value={historySearch}
            onChange={setHistorySearch}
            placeholder="搜索主题、正文、文档类型或受众"
            ariaLabel="搜索创作记录"
            total={historyTotal}
            loading={historyLoading}
          />
          <div className="history-browser__list-scroll">
            {historyLoading && creationHistory.length === 0 ? (
              <div className="history-browser__state">正在加载创作记录…</div>
            ) : historyError ? (
              <div className="history-browser__state history-browser__state--error" role="alert">
                <span>{historyError}</span>
                <button type="button" onClick={() => void loadCreationHistory()}>重新加载</button>
              </div>
            ) : creationHistory.length > 0 ? (
              <div className="creation-history__list">
                {creationHistory.map((item) => (
                  <article className="creation-history__entry" key={item.id}>
                    <button
                      type="button"
                      className="creation-history__item"
                      onClick={() => {
                        const isLiveRun = item.lifecycleStatus === 'running'
                          && activeHistoryIdRef.current === item.id
                        if (!isLiveRun) handleRestoreHistory(item)
                        setTopTab('creation')
                      }}
                    >
                      <span className="creation-history__title">
                        {item.prompt}
                        {item.lifecycleStatus === 'running' && (
                          <span className="creation-history__lifecycle is-running">
                            <Loader2 className="spin" size={12} /> 进行中
                          </span>
                        )}
                        {item.lifecycleStatus === 'failed' && (
                          <span className="creation-history__lifecycle is-failed">失败</span>
                        )}
                        {item.lifecycleStatus === 'cancelled' && (
                          <span className="creation-history__lifecycle is-cancelled">已中止</span>
                        )}
                      </span>
                      <span className="creation-history__meta">
                        {item.sourceKind === 'scheduled_task' && (
                          <span style={{
                            fontSize: 11, padding: '0 6px', borderRadius: 4, marginRight: 6,
                            background: 'rgba(88,86,214,0.1)', color: '#5856D6',
                          }}>定时任务</span>
                        )}
                        完整会话 · {item.timestamp} · 模型：{getModelDisplayName(item.model)} · 推理耗时：{formatInferenceLatency(item.latencyMs)}
                      </span>
                      <span className="creation-history__preview">
                        {item.preview || (item.lifecycleStatus === 'running'
                          ? '创作正在后台持续进行，点击查看实时进度。'
                          : '本次创作暂无正文内容。')}
                      </span>
                    </button>
                  </article>
                ))}
              </div>
            ) : (
              <div className="history-browser__state">
                {debouncedHistorySearch ? '没有找到匹配的创作记录。' : '暂无创作记录。'}
              </div>
            )}
          </div>
          <HistoryPagination
            page={historyPage}
            pageSize={HISTORY_PAGE_SIZE}
            total={historyTotal}
            loading={historyLoading}
            onPageChange={setHistoryPage}
          />
        </div>
      ) : topTab === 'skills' ? (
        <div className="creation-skill-library">
          <header>
            <div>
              <h2>{skillLibraryView === 'mine' ? '我的技能' : '技能市场'}</h2>
            </div>
            <div className="creation-skill-library__header-actions">
              <div className="creation-skill-library__switcher" role="tablist" aria-label="技能来源">
                <button
                  type="button"
                  role="tab"
                  aria-selected={skillLibraryView === 'mine'}
                  className={skillLibraryView === 'mine' ? 'is-active' : ''}
                  onClick={() => setSkillLibraryView('mine')}
                >
                  <Library size={14} /> 我的技能
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={skillLibraryView === 'market'}
                  className={skillLibraryView === 'market' ? 'is-active' : ''}
                  onClick={() => setSkillLibraryView('market')}
                >
                  <Store size={14} /> 技能市场
                </button>
              </div>
              {skillLibraryView === 'mine' && (
                <>
                  <button
                    type="button"
                    onClick={() => setSkillEditor({})}
                    title="手工录入一份新技能"
                  >
                    <Plus size={14} /> 新建技能
                  </button>
                  <input
                    ref={skillPackageInputRef}
                    className="creation-skill-package-input"
                    type="file"
                    multiple
                    aria-label="选择 Skill 源文件夹"
                    onChange={handleSkillPackageSelected}
                    {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
                  />
                  <input
                    ref={skillZipInputRef}
                    className="creation-skill-package-input"
                    type="file"
                    accept=".zip,application/zip"
                    aria-label="选择 Skill 源文件 ZIP"
                    onChange={handleSkillZipSelected}
                  />
                  <div className="creation-skill-upload" ref={skillUploadMenuRef}>
                    <button
                      type="button"
                      onClick={() => setSkillUploadMenuOpen(open => !open)}
                      disabled={uploadingSkillPackage}
                      aria-haspopup="menu"
                      aria-expanded={skillUploadMenuOpen}
                      title="上传包含根目录 SKILL.md 的文件夹或 ZIP"
                    >
                      {uploadingSkillPackage
                        ? <Loader2 className="spin" size={14} />
                        : <Upload size={14} />}
                      {uploadingSkillPackage ? '正在上传…' : '上传'}
                      {!uploadingSkillPackage && <ChevronDown size={13} />}
                    </button>
                    {skillUploadMenuOpen && (
                      <div className="creation-skill-upload__menu" role="menu" aria-label="上传 Skill 源文件">
                        <button type="button" role="menuitem" onClick={() => {
                          setSkillUploadMenuOpen(false)
                          skillPackageInputRef.current?.click()
                        }}>
                          <Library size={14} />
                          <span><strong>选择文件夹</strong><small>包含根目录 SKILL.md</small></span>
                        </button>
                        <button type="button" role="menuitem" onClick={() => {
                          setSkillUploadMenuOpen(false)
                          skillZipInputRef.current?.click()
                        }}>
                          <PackagePlus size={14} />
                          <span><strong>选择 ZIP 文件</strong><small>Codex / Claude Code 兼容</small></span>
                        </button>
                      </div>
                    )}
                  </div>
                </>
              )}
              <button
                type="button"
                onClick={() => void (skillLibraryView === 'mine'
                  ? loadLocalSkills()
                  : Promise.all([loadMarketSkills(), loadMarketCategories()]))}
                disabled={skillLibraryView === 'mine' ? skillsLoading : marketLoading}
              >
                刷新
              </button>
            </div>
          </header>

          {skillLibraryView === 'market' && (
            <form className="creation-skill-market-search" onSubmit={handleMarketSearch} role="search">
              <label className="creation-skill-market-search__field">
                <span>搜索市场技能</span>
                <span className="creation-skill-market-search__query">
                  <Search size={16} />
                  <input
                    value={marketQueryDraft}
                    onChange={event => setMarketQueryDraft(event.target.value)}
                    placeholder="搜索标题或适用场景"
                  />
                </span>
              </label>
              <label className="creation-skill-market-search__field">
                <span>技能类目</span>
                <CreationSkillCategoryCombobox
                  value={marketCategoryIdDraft}
                  options={marketCategoryOptions}
                  onChange={setMarketCategoryIdDraft}
                />
              </label>
              <button type="submit" disabled={marketLoading}>搜索</button>
            </form>
          )}

          {skillLibraryView === 'mine' ? (
            <>
              {skillsError && <div className="creation-skill-library__feedback is-error" role="alert">{skillsError}</div>}
              {skillsLoading && localSkills.length === 0 ? (
                <div className="history-browser__state"><Loader2 className="spin" size={17} /> 正在加载技能…</div>
              ) : localSkills.length === 0 ? (
                <div className="creation-skill-library__empty"><Library size={32} /><strong>还没有技能</strong><span>可以手工新建、上传 Skill 源文件，或去市场安装一份。</span><div className="creation-skill-library__empty-actions"><button type="button" onClick={() => setSkillEditor({})}>新建技能</button><button type="button" onClick={() => setSkillLibraryView('market')}>浏览技能市场</button></div></div>
              ) : (
                <div className="creation-skill-library__grid">
                  {localSkills.map(skill => {
                    const fromMarket = skill.sourceKind === 'market'
                    const imported = skill.sourceKind === 'imported'
                    const sourceStatus = fromMarket
                      ? '来自市场'
                      : imported
                        ? '手工上传'
                        : skill.published
                          ? '已发布'
                          : skill.status === 'draft'
                            ? '草稿'
                            : null
                    return (
                      <article key={skill.id}>
                        <div className="creation-skill-library__title-row">
                          <button
                            type="button"
                            className="creation-skill-library__title"
                            onClick={() => showLocalSkillDetail(skill)}
                          >
                            {skill.title}
                          </button>
                          <div className="creation-skill-library__title-status">
                            {sourceStatus && <span className="creation-skill-library__status">{sourceStatus}</span>}
                            <span className={skill.installed ? 'is-installed' : ''}>{skill.installed ? '已安装' : '未安装'}</span>
                          </div>
                        </div>
                        <p>{skill.summary}</p>
                        <div className="creation-skill-library__meta">
                          {imported
                            ? `${skill.packageFiles?.length || 0} 个文件 · Codex / Claude Code 兼容`
                            : `${skill.executionSteps.length} 个步骤 · ${skill.commonTitles.length} 个标题规则`}
                        </div>
                        <footer className={fromMarket ? 'is-compact' : ''}>
                          <button
                            type="button"
                            className={skill.installed ? 'is-installed' : ''}
                            onClick={() => void handleToggleSkillInstalled(skill)}
                            disabled={skill.status === 'draft'}
                            title={skill.status === 'draft' ? '保存技能后才能安装' : undefined}
                          >
                            {skill.installed ? <PackageCheck size={14} /> : <PackagePlus size={14} />}
                            {skill.installed ? '卸载' : '安装'}
                          </button>
                          <button type="button" onClick={() => showLocalSkillDetail(skill)}>
                            <Eye size={14} /> 查看详情
                          </button>
                          <button
                            type="button"
                            onClick={() => showLocalSkillDetail(skill, true)}
                            title="查看并下载 Skill 源文件"
                          >
                            <FileCode2 size={14} /> 源文件
                          </button>
                          {!fromMarket && !imported && (
                            <>
                              <button
                                type="button"
                                className={skill.published ? 'is-unpublish' : ''}
                                onClick={() => void handlePublishSkill(skill, !skill.published)}
                                disabled={skill.status === 'draft' || publishingSkillId === skill.id}
                                title={skill.status === 'draft'
                                  ? '保存技能后才能发布'
                                  : skill.published
                                    ? '从技能市场取消发布'
                                    : '发布到技能市场'}
                              >
                                {publishingSkillId === skill.id
                                  ? <Loader2 className="spin" size={14} />
                                  : skill.published
                                    ? <CloudOff size={14} />
                                    : <CloudUpload size={14} />}
                                {skill.published ? '取消发布' : '发布'}
                              </button>
                              <button type="button" onClick={() => setSkillEditor({ initialSkill: skill })}><Pencil size={14} /> 编辑</button>
                            </>
                          )}
                          {!fromMarket && (
                            <button type="button" onClick={() => handleDeleteSkill(skill)} disabled={skill.published}>
                              <Trash2 size={14} /> 删除
                            </button>
                          )}
                        </footer>
                      </article>
                    )
                  })}
                </div>
              )}
            </>
          ) : (
            <>
              {marketError && <div className="creation-skill-library__feedback is-error" role="alert">{marketError}</div>}
              {marketLoading && marketSkills.length === 0 ? (
                <div className="history-browser__state"><Loader2 className="spin" size={17} /> 正在搜索市场技能…</div>
              ) : marketSkills.length === 0 ? (
                <div className="creation-skill-library__empty">
                  <Store size={32} />
                  <strong>{marketQuery || marketCategoryId ? '没有找到匹配的技能' : '市场暂时还没有公开技能'}</strong>
                  <span>{marketQuery || marketCategoryId ? '换个关键词或类目再试试。' : '稍后刷新即可看到新发布的技能。'}</span>
                </div>
              ) : (
                <>
                  <div className="creation-skill-market-count">找到 {marketTotal} 个公开技能</div>
                  <div className="creation-skill-library__grid creation-skill-market-grid">
                    {marketSkills.map(skill => {
                      const installed = installedMarketSkillIds.has(skill.id)
                      return (
                        <article key={skill.id}>
                          <div className="creation-skill-library__status-row">
                            <div className={`creation-skill-library__status ${skill.isOfficial ? 'is-official' : ''}`}>
                              {skill.isOfficial ? '官方技能' : '市场技能'}
                            </div>
                            <span className={installed ? 'is-installed' : ''}>{installed ? '已安装' : '可安装'}</span>
                          </div>
                          <button
                            type="button"
                            className="creation-skill-library__title"
                            onClick={() => showMarketSkillDetail(skill)}
                          >
                            {skill.title}
                          </button>
                          <p>{skill.summary}</p>
                          <div className="creation-skill-library__meta">
                            {skill.author.nickname} · {skill.categoryPath.map(item => item.name).join(' / ')}
                          </div>
                          <footer className="is-compact">
                            <button type="button" onClick={() => showMarketSkillDetail(skill)}>
                              <Eye size={14} /> 查看详情
                            </button>
                            <button
                              type="button"
                              onClick={() => showMarketSkillDetail(skill, true)}
                              title="查看并下载 Skill 源文件"
                            >
                              <FileCode2 size={14} /> 源文件
                            </button>
                            <button
                              type="button"
                              className={installed ? 'is-installed' : ''}
                              disabled={installingMarketSkillId === skill.id}
                              onClick={() => void handleInstallMarketSkill(skill)}
                            >
                              {installingMarketSkillId === skill.id
                                ? <Loader2 className="spin" size={14} />
                                : installed
                                  ? <PackageCheck size={14} />
                                  : <PackagePlus size={14} />}
                              {installed ? '卸载' : '安装'}
                            </button>
                          </footer>
                        </article>
                      )
                    })}
                  </div>
                  {marketTotal > SKILL_MARKET_PAGE_SIZE && (
                    <div className="creation-skill-market-pagination">
                      <button
                        type="button"
                        disabled={marketOffset === 0 || marketLoading}
                        onClick={() => setMarketOffset(offset => Math.max(0, offset - SKILL_MARKET_PAGE_SIZE))}
                      >
                        上一页
                      </button>
                      <span>{Math.floor(marketOffset / SKILL_MARKET_PAGE_SIZE) + 1} / {Math.ceil(marketTotal / SKILL_MARKET_PAGE_SIZE)}</span>
                      <button
                        type="button"
                        disabled={marketOffset + SKILL_MARKET_PAGE_SIZE >= marketTotal || marketLoading}
                        onClick={() => setMarketOffset(offset => offset + SKILL_MARKET_PAGE_SIZE)}
                      >
                        下一页
                      </button>
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </div>
      ) : topTab === 'tools' ? (
        <CreationToolsPanel
          tools={creationTools}
          onInstall={handleInstallTool}
          onUninstall={handleUninstallTool}
          onToggle={handleToggleTool}
          onResultLimitChange={handleToolResultLimitChange}
        />
      ) : (
        <main
          ref={workspaceRef}
          className="creation-workspace"
          style={{
            flex: 1,
            minWidth: 0,
            overflow: 'hidden',
            '--creation-left-pane': `${workspaceSplit}%`,
          } as React.CSSProperties}
        >
          <section className="creation-chat-shell" aria-label="创作对话">
            <header className="creation-chat-header">
              <div>
                <Bot size={18} />
                <strong>创作 Agent</strong>
                {sessionTerminated
                  ? <span>会话已终止，已有内容仍可查看</span>
                  : generatedContent && <span>继续对话优化当前文档</span>}
              </div>
              <div className="creation-chat-header__actions">
                {sessionId && <code>{sessionId.slice(-8)}</code>}
                <button
                  type="button"
                  className="creation-terminate-session-button"
                  onClick={handleTerminateSession}
                  disabled={!hasActiveSession || sessionTerminated}
                  aria-label="终止当前会话"
                  title={sessionTerminated ? '当前会话已终止' : '停止当前会话并保留已有内容'}
                >
                  <Square size={13} fill="currentColor" />
                  {sessionTerminated ? '已终止' : '终止会话'}
                </button>
                <button
                  type="button"
                  className="creation-new-session-button"
                  onClick={handleNewConversation}
                  disabled={(!sessionTerminated && (isGenerating || isBrainstormLoading || Boolean(pendingConfirmation))) || !hasActiveSession}
                  aria-label="开启新会话"
                  title={isGenerating || isBrainstormLoading || pendingConfirmation
                    ? '当前创作结束或中止后可开启新会话'
                    : '清空当前内容并开启新会话'}
                >
                  <MessageSquarePlus size={15} />
                  新会话
                </button>
              </div>
            </header>
            {(
              conversationForTimeline.length > 0
              || agentEvents.length > 0
              || Boolean(pendingConfirmation)
              || (creationMode === 'brainstorm' && Boolean(brainstormState || brainstormError || isBrainstormLoading))
            ) && (
              <div
                className="creation-chat-timeline"
                ref={chatTimelineRef}
                aria-live="polite"
              >
                {timelineBeforeBrainstorm.map(renderCreationTimelineItem)}
            {creationMode === 'brainstorm' && isBrainstormLoading && !brainstormState && (
              <article className="creation-brainstorm-turn" aria-label="Agent 正在思考">
                <div className="creation-chat-message__meta">
                  <span>创作 Agent</span>
                  <span className="creation-brainstorm-turn__step">脑暴步骤</span>
                </div>
                <section
                  className="creation-brainstorm-card creation-brainstorm-card--loading"
                  role="status"
                  aria-label="正在准备第一条脑暴问题"
                >
                  <span className="creation-brainstorm-card__loading-icon" aria-hidden="true">
                    <Loader2 size={19} className="spin" />
                  </span>
                  <div className="creation-brainstorm-card__loading-copy">
                    <span className="creation-brainstorm-card__eyebrow">正在准备第一条脑暴问题</span>
                    <strong>正在梳理你的创作目标</strong>
                    <p>创作 Agent 正在理解你的要求，找出最值得先确认的关键方向。</p>
                  </div>
                </section>
              </article>
            )}
            {creationMode === 'brainstorm' && brainstormError && !brainstormState && (
              <div className="creation-brainstorm-card__error" role="alert">{brainstormError}</div>
            )}
            {creationMode === 'brainstorm' && brainstormState && (
              <article className="creation-brainstorm-turn" aria-label="Agent 消息">
                <div className="creation-chat-message__meta">
                  <span>创作 Agent</span>
                  <span className="creation-brainstorm-turn__step">脑暴步骤</span>
                </div>
                <section className="creation-brainstorm-card" aria-live="polite">
                <header>
                  <div>
                    <span className="creation-brainstorm-card__eyebrow">
                      {brainstormCardState?.phase === 'abandoned'
                        ? '会话已终止'
                        : brainstormCardState?.phase === 'ready' && !brainstormIsReviewingHistory
                        ? '创作简报已就绪'
                        : `${brainstormQuestion?.dimension || '需求梳理'} · ${brainstormIsReviewingHistory ? '回看已答问题' : `第 ${(brainstormCardState?.depth || 0) + 1} 轮模型追问`}`}
                    </span>
                    <strong>
                      {brainstormCardState?.phase === 'abandoned'
                        ? '本次脑暴已停止'
                        : brainstormCardState?.phase === 'ready' && !brainstormIsReviewingHistory
                        ? '关键方向已经收敛，可以开始生成'
                        : brainstormQuestion?.prompt}
                    </strong>
                  </div>
                  <div className="creation-brainstorm-card__header-actions">
                    <div className="creation-brainstorm-pagination" aria-label="脑暴问题导航">
                      <button
                        type="button"
                        aria-label="上一题"
                        title="上一题"
                        onClick={() => setBrainstormHistoryIndex(Math.max(0, brainstormPageIndex - 1))}
                        disabled={sessionTerminated || isBrainstormLoading || brainstormPageIndex <= 0}
                      >
                        <ChevronLeft size={14} aria-hidden />
                      </button>
                      <span aria-live="polite">
                        {brainstormIsReviewingHistory || brainstormCardState?.current_question
                          ? `第 ${brainstormPageIndex + 1} / ${Math.max(1, brainstormPageCount)} 题`
                          : `${brainstormHistory.length} 题已完成`}
                      </span>
                      <button
                        type="button"
                        aria-label="下一题"
                        title="下一题"
                        onClick={() => {
                          const nextIndex = brainstormPageIndex + 1
                          setBrainstormHistoryIndex(nextIndex < brainstormHistory.length ? nextIndex : null)
                        }}
                        disabled={sessionTerminated || isBrainstormLoading || brainstormHistoryIndex === null}
                      >
                        <ChevronRight size={14} aria-hidden />
                      </button>
                    </div>
                    {isBrainstormLoading && <Loader2 size={17} className="spin" aria-label="正在整理回答" />}
                  </div>
                </header>
                {(brainstormCardState?.decisions || []).length > 0 && (
                  <div className="creation-brainstorm-decisions" aria-label="已确认决定">
                    {brainstormCardState?.decisions.map(decision => (
                      <div key={decision.question_id}>
                        <span>
                          <small>{decision.source === 'agent_assumption' ? '合理假设' : '已确认'} · {decision.dimension}</small>
                          <strong>{decision.summary}</strong>
                        </span>
                        <button
                          type="button"
                          onClick={() => reopenBrainstormDecision(decision.question_id)}
                          disabled={sessionTerminated || isBrainstormLoading || isGenerating}
                        >
                          修改
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {brainstormCardState?.phase === 'abandoned' ? (
                  <div className="creation-brainstorm-ended" role="status">
                    <Square size={14} fill="currentColor" aria-hidden />
                    <span>脑暴会话已终止，已确认的决定和简报仍保留在当前记录中。</span>
                  </div>
                ) : brainstormCardState?.phase === 'ready' && !brainstormIsReviewingHistory ? (
                  <div className="creation-brainstorm-ready">
                    <p>
                      已确认 {brainstormCardState.answered_count} 项决定
                      {brainstormCardState.open_flags.length
                        ? `，其余 ${brainstormCardState.open_flags.length} 项将作为开放假设保留。`
                        : '，没有会改变整体方向的开放项。'}
                    </p>
                    {brainstormCardState.readiness_reason && (
                      <small>{brainstormCardState.readiness_reason}</small>
                    )}
                    {brainstormContinuationOpen && brainstormState.can_continue_brainstorm && !brainstormGenerationStarted && (
                      <div
                        ref={brainstormContinuationRef}
                        className="creation-brainstorm-continuation"
                      >
                        <div className="creation-brainstorm-continuation__heading">
                          <strong>选择继续脑暴的方向</strong>
                          <small>以下方向由模型根据当前简报推荐，不影响已经确认的决定。</small>
                        </div>
                        <div
                          className="creation-brainstorm-options creation-brainstorm-options--continuation"
                          role="radiogroup"
                          aria-label="继续脑暴方向"
                        >
                          {brainstormState.continuation_directions.map(direction => {
                            const selected = brainstormContinuationDirectionId === direction.id
                            return (
                              <button
                                key={direction.id}
                                type="button"
                                role="radio"
                                aria-checked={selected}
                                className={selected ? 'is-selected' : ''}
                                onClick={() => setBrainstormContinuationDirectionId(direction.id)}
                                disabled={isBrainstormLoading || isGenerating}
                              >
                                <span className="creation-brainstorm-options__mark" aria-hidden>
                                  {selected ? <Check size={13} /> : null}
                                </span>
                                <span>
                                  <strong>
                                    {direction.label}
                                    {direction.recommended && <small>推荐</small>}
                                  </strong>
                                  <small>{direction.description}</small>
                                </span>
                              </button>
                            )
                          })}
                          <button
                            type="button"
                            role="radio"
                            aria-checked={brainstormContinuationDirectionId === '__custom__'}
                            className={brainstormContinuationDirectionId === '__custom__' ? 'is-selected' : ''}
                            onClick={() => setBrainstormContinuationDirectionId('__custom__')}
                            disabled={isBrainstormLoading || isGenerating}
                          >
                            <span className="creation-brainstorm-options__mark" aria-hidden>
                              {brainstormContinuationDirectionId === '__custom__' ? <Check size={13} /> : null}
                            </span>
                            <span>
                              <strong>自定义脑暴方向</strong>
                              <small>输入你希望继续探索、挑战或补强的内容。</small>
                            </span>
                          </button>
                        </div>
                        {brainstormContinuationDirectionId === '__custom__' && (
                          <label className="creation-brainstorm-custom">
                            <span>脑暴方向</span>
                            <textarea
                              value={brainstormCustomDirection}
                              onChange={event => setBrainstormCustomDirection(event.target.value)}
                              placeholder="例如：从真实用户迁移成本的角度继续脑暴"
                              disabled={isBrainstormLoading || isGenerating}
                              maxLength={500}
                              rows={2}
                            />
                          </label>
                        )}
                        <div className="creation-brainstorm-continuation__actions">
                          <button
                            type="button"
                            className="is-secondary"
                            onClick={() => {
                              setBrainstormContinuationOpen(false)
                              setBrainstormContinuationDirectionId('')
                              setBrainstormCustomDirection('')
                            }}
                            disabled={isBrainstormLoading || isGenerating}
                          >
                            取消
                          </button>
                          <button
                            type="button"
                            onClick={() => void continueBrainstorm()}
                            disabled={isBrainstormLoading || isGenerating || !brainstormContinuationDirectionId || (
                              brainstormContinuationDirectionId === '__custom__' && !brainstormCustomDirection.trim()
                            )}
                          >
                            <Lightbulb size={15} /> 按此方向继续
                          </button>
                        </div>
                      </div>
                    )}
                    <div className="creation-brainstorm-ready__actions">
                      {brainstormState.can_continue_brainstorm && !brainstormContinuationOpen && !brainstormGenerationStarted && (
                        <button
                          type="button"
                          className="is-secondary"
                          onClick={() => {
                            const recommended = brainstormState.continuation_directions.find(direction => direction.recommended)
                              || brainstormState.continuation_directions[0]
                            setBrainstormContinuationDirectionId(recommended?.id || '')
                            setBrainstormContinuationOpen(true)
                          }}
                          disabled={isGenerating || isBrainstormLoading}
                        >
                          <Lightbulb size={15} /> 继续脑暴
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => void handleGenerate()}
                        disabled={isGenerating || isBrainstormLoading || brainstormGenerationStarted}
                      >
                        <Sparkles size={15} /> 按此生成
                      </button>
                    </div>
                  </div>
                ) : brainstormQuestion ? (
                  <>
                    {shouldShowBrainstormWhyNow && (
                      <p className="creation-brainstorm-card__why">{brainstormWhyNow}</p>
                    )}
                    {(brainstormQuestion.options.length > 0 || brainstormQuestion.allow_custom) && (
                      <div
                        className="creation-brainstorm-options"
                        role={brainstormQuestion.type === 'multi_choice' ? 'group' : 'radiogroup'}
                        aria-label="答案选项"
                      >
                        {brainstormQuestion.options.map(option => {
                          const selected = brainstormSelectedOptions.includes(option.id)
                          return (
                            <button
                              key={option.id}
                              type="button"
                              role={brainstormQuestion.type === 'multi_choice' ? 'checkbox' : 'radio'}
                              aria-checked={selected}
                              className={selected ? 'is-selected' : ''}
                              onClick={() => {
                                setBrainstormCustomAnswerSelected(false)
                                if (brainstormQuestion.type === 'multi_choice') {
                                  setBrainstormSelectedOptions(current => current.includes(option.id)
                                    ? current.filter(id => id !== option.id)
                                    : [...current, option.id])
                                } else {
                                  setBrainstormSelectedOptions([option.id])
                                }
                              }}
                              disabled={isBrainstormLoading}
                            >
                              <span className="creation-brainstorm-options__mark" aria-hidden>
                                {selected ? <Check size={13} /> : null}
                              </span>
                              <span>
                                <strong>
                                  {option.label}
                                  {option.recommended && <small>推荐</small>}
                                </strong>
                                <small>{option.description}</small>
                              </span>
                            </button>
                          )
                        })}
                        {brainstormQuestion.allow_custom && (
                          <button
                            type="button"
                            role={brainstormQuestion.type === 'multi_choice' ? 'checkbox' : 'radio'}
                            aria-checked={brainstormCustomAnswerSelected}
                            className={brainstormCustomAnswerSelected ? 'is-selected' : ''}
                            onClick={() => {
                              setBrainstormSelectedOptions([])
                              setBrainstormCustomAnswerSelected(true)
                            }}
                            disabled={isBrainstormLoading}
                          >
                            <span className="creation-brainstorm-options__mark" aria-hidden>
                              {brainstormCustomAnswerSelected ? <Check size={13} /> : null}
                            </span>
                            <span>
                              <strong>自定义答案</strong>
                              <small>选择后输入具体内容，不与以上选项同时选择。</small>
                            </span>
                          </button>
                        )}
                      </div>
                    )}
                    {brainstormUsesCustomAnswer && (
                      <label className="creation-brainstorm-custom">
                        <span>具体内容</span>
                        <textarea
                          value={brainstormCustomAnswer}
                          onChange={event => setBrainstormCustomAnswer(event.target.value)}
                          placeholder={brainstormQuestion.answer_template}
                          disabled={isBrainstormLoading}
                          rows={3}
                        />
                      </label>
                    )}
                    <footer>
                      {brainstormIsReviewingHistory ? (
                        <button
                          type="button"
                          className="is-secondary"
                          onClick={() => setBrainstormHistoryIndex(null)}
                          disabled={isBrainstormLoading}
                        >
                          {brainstormState.current_question ? '返回当前问题' : '返回最新进度'}
                        </button>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="is-secondary"
                            onClick={() => void skipBrainstormQuestion()}
                            disabled={isBrainstormLoading}
                          >
                            跳过此题
                          </button>
                          <button
                            type="button"
                            className="is-secondary"
                            onClick={() => void finishBrainstorm()}
                            disabled={isBrainstormLoading}
                          >
                            基于当前简报生成
                          </button>
                        </>
                      )}
                      <button
                        type="button"
                        onClick={() => void submitBrainstormAnswer()}
                        disabled={!brainstormAnswerValid || isBrainstormLoading || (brainstormIsReviewingHistory && !brainstormHistoryAnswerChanged)}
                      >
                        {brainstormIsReviewingHistory ? '保存修改' : '确认并继续'} <ChevronRight size={15} />
                      </button>
                    </footer>
                  </>
                ) : null}
                {(brainstormCardState?.invalidated_question_ids.length || 0) > 0 && (
                  <div className="creation-brainstorm-card__notice" role="status">
                    上游决定已变化，{brainstormCardState?.invalidated_question_ids.length} 项后续结论需要重新确认。
                  </div>
                )}
                {brainstormError && !brainstormGenerationStarted && (
                  <div className="creation-brainstorm-card__error" role="alert">{brainstormError}</div>
                )}
                </section>
              </article>
            )}
                {timelineAfterBrainstorm.map(renderCreationTimelineItem)}
                {creationMode === 'brainstorm'
                  && brainstormGenerationStarted
                  && brainstormState
                  && (
                    (brainstormState.phase === 'ready' && brainstormState.can_continue_brainstorm)
                    || (brainstormState.phase === 'exploring' && Boolean(brainstormState.current_question))
                    || brainstormContinuationTurns.length > 0
                  ) && (
                  <div
                    className="creation-brainstorm-latest-control"
                    role="group"
                    aria-label="最新脑暴操作"
                  >
                    <div
                      className="creation-brainstorm-latest-action"
                      aria-live="polite"
                    >
                      {brainstormContinuationTurns.length > 0 && (
                        <div
                          className="creation-brainstorm-latest-history"
                          aria-label="已完成的继续脑暴"
                        >
                          {brainstormContinuationTurns.map(turn => {
                            const selectedLabels = turn.answer.selected_option_ids.map(optionId => (
                              turn.question.options.find(option => option.id === optionId)?.label || optionId
                            ))
                            const answerSummary = [...selectedLabels, turn.answer.custom_text.trim()]
                              .filter(Boolean)
                              .join('；')
                            return (
                              <div
                                key={turn.question.id}
                                className="creation-brainstorm-latest-history__turn"
                              >
                                <strong>{turn.question.prompt}</strong>
                                <span><Check size={13} aria-hidden /> {answerSummary || '已跳过此题'}</span>
                              </div>
                            )
                          })}
                        </div>
                      )}
                      {brainstormState.phase === 'exploring' && brainstormState.current_question ? (
                        <div
                          ref={brainstormContinuationRef}
                          className="creation-brainstorm-continuation"
                        >
                          <div className="creation-brainstorm-continuation__heading">
                            <strong>{brainstormState.current_question.prompt}</strong>
                            {brainstormState.current_question.why_now && (
                              <small>{brainstormState.current_question.why_now}</small>
                            )}
                          </div>
                          <div
                            className="creation-brainstorm-options"
                            role={brainstormState.current_question.type === 'multi_choice' ? 'group' : 'radiogroup'}
                            aria-label="答案选项"
                          >
                            {brainstormState.current_question.options.map(option => {
                              const selected = brainstormSelectedOptions.includes(option.id)
                              return (
                                <button
                                  key={option.id}
                                  type="button"
                                  role={brainstormState.current_question?.type === 'multi_choice' ? 'checkbox' : 'radio'}
                                  aria-checked={selected}
                                  className={selected ? 'is-selected' : ''}
                                  onClick={() => {
                                    setBrainstormCustomAnswerSelected(false)
                                    if (brainstormState.current_question?.type === 'multi_choice') {
                                      setBrainstormSelectedOptions(current => current.includes(option.id)
                                        ? current.filter(id => id !== option.id)
                                        : [...current, option.id])
                                    } else {
                                      setBrainstormSelectedOptions([option.id])
                                    }
                                  }}
                                  disabled={isBrainstormLoading}
                                >
                                  <span className="creation-brainstorm-options__mark" aria-hidden>
                                    {selected ? <Check size={13} /> : null}
                                  </span>
                                  <span>
                                    <strong>
                                      {option.label}
                                      {option.recommended && <small>推荐</small>}
                                    </strong>
                                    <small>{option.description}</small>
                                  </span>
                                </button>
                              )
                            })}
                            {brainstormState.current_question.allow_custom && (
                              <button
                                type="button"
                                role={brainstormState.current_question.type === 'multi_choice' ? 'checkbox' : 'radio'}
                                aria-checked={brainstormCustomAnswerSelected}
                                className={brainstormCustomAnswerSelected ? 'is-selected' : ''}
                                onClick={() => {
                                  setBrainstormSelectedOptions([])
                                  setBrainstormCustomAnswerSelected(true)
                                }}
                                disabled={isBrainstormLoading}
                              >
                                <span className="creation-brainstorm-options__mark" aria-hidden>
                                  {brainstormCustomAnswerSelected ? <Check size={13} /> : null}
                                </span>
                                <span>
                                  <strong>自定义答案</strong>
                                  <small>输入你希望继续深入的具体内容。</small>
                                </span>
                              </button>
                            )}
                          </div>
                          {brainstormCustomAnswerSelected && (
                            <label className="creation-brainstorm-custom">
                              <span>具体内容</span>
                              <textarea
                                value={brainstormCustomAnswer}
                                onChange={event => setBrainstormCustomAnswer(event.target.value)}
                                placeholder={brainstormState.current_question.answer_template}
                                disabled={isBrainstormLoading}
                                rows={3}
                              />
                            </label>
                          )}
                          <div className="creation-brainstorm-continuation__actions">
                            <button
                              type="button"
                              className="is-secondary"
                              onClick={() => void skipBrainstormQuestion()}
                              disabled={isBrainstormLoading}
                            >
                              跳过此题
                            </button>
                            <button
                              type="button"
                              onClick={() => void submitBrainstormAnswer()}
                              disabled={!brainstormAnswerValid || isBrainstormLoading}
                            >
                              {isBrainstormLoading
                                ? <Loader2 size={15} className="spin" />
                                : <ChevronRight size={15} />}
                              确认并继续
                            </button>
                          </div>
                        </div>
                      ) : brainstormContinuationOpen ? (
                        <div
                          ref={brainstormContinuationRef}
                          className="creation-brainstorm-continuation"
                        >
                          <div className="creation-brainstorm-continuation__heading">
                            <strong>选择继续脑暴的方向</strong>
                            <small>以下方向由模型根据当前简报推荐，不影响已经确认的决定。</small>
                          </div>
                          <div
                            className="creation-brainstorm-options creation-brainstorm-options--continuation"
                            role="radiogroup"
                            aria-label="继续脑暴方向"
                          >
                            {brainstormState.continuation_directions.map(direction => {
                              const selected = brainstormContinuationDirectionId === direction.id
                              return (
                                <button
                                  key={direction.id}
                                  type="button"
                                  role="radio"
                                  aria-checked={selected}
                                  className={selected ? 'is-selected' : ''}
                                  onClick={() => setBrainstormContinuationDirectionId(direction.id)}
                                  disabled={isBrainstormLoading || isGenerating}
                                >
                                  <span className="creation-brainstorm-options__mark" aria-hidden>
                                    {selected ? <Check size={13} /> : null}
                                  </span>
                                  <span>
                                    <strong>
                                      {direction.label}
                                      {direction.recommended && <small>推荐</small>}
                                    </strong>
                                    <small>{direction.description}</small>
                                  </span>
                                </button>
                              )
                            })}
                            <button
                              type="button"
                              role="radio"
                              aria-checked={brainstormContinuationDirectionId === '__custom__'}
                              className={brainstormContinuationDirectionId === '__custom__' ? 'is-selected' : ''}
                              onClick={() => setBrainstormContinuationDirectionId('__custom__')}
                              disabled={isBrainstormLoading || isGenerating}
                            >
                              <span className="creation-brainstorm-options__mark" aria-hidden>
                                {brainstormContinuationDirectionId === '__custom__' ? <Check size={13} /> : null}
                              </span>
                              <span>
                                <strong>自定义脑暴方向</strong>
                                <small>输入你希望继续探索、挑战或补强的内容。</small>
                              </span>
                            </button>
                          </div>
                          {brainstormContinuationDirectionId === '__custom__' && (
                            <label className="creation-brainstorm-custom">
                              <span>脑暴方向</span>
                              <textarea
                                value={brainstormCustomDirection}
                                onChange={event => setBrainstormCustomDirection(event.target.value)}
                                placeholder="例如：从真实用户迁移成本的角度继续脑暴"
                                disabled={isBrainstormLoading || isGenerating}
                                maxLength={500}
                                rows={2}
                              />
                            </label>
                          )}
                          <div className="creation-brainstorm-continuation__actions">
                            <button
                              type="button"
                              className="is-secondary"
                              onClick={() => {
                                setBrainstormContinuationOpen(false)
                                setBrainstormContinuationDirectionId('')
                                setBrainstormCustomDirection('')
                              }}
                              disabled={isBrainstormLoading || isGenerating}
                            >
                              取消
                            </button>
                            <button
                              type="button"
                              className="is-secondary"
                              onClick={() => void handleGenerate()}
                              disabled={isGenerating || isBrainstormLoading}
                            >
                              {isGenerating
                                ? <Loader2 size={15} className="spin" />
                                : <Sparkles size={15} />}
                              继续生成文档内容
                            </button>
                            <button
                              type="button"
                              onClick={() => void continueBrainstorm()}
                              disabled={isBrainstormLoading || isGenerating || !brainstormContinuationDirectionId || (
                                brainstormContinuationDirectionId === '__custom__' && !brainstormCustomDirection.trim()
                              )}
                            >
                              {isBrainstormLoading
                                ? <Loader2 size={15} className="spin" />
                                : <Lightbulb size={15} />}
                              按此方向继续
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="creation-brainstorm-ready__actions">
                          {brainstormState.can_continue_brainstorm && (
                            <button
                              type="button"
                              className="is-secondary"
                              onClick={() => {
                                const recommended = brainstormState.continuation_directions.find(direction => direction.recommended)
                                  || brainstormState.continuation_directions[0]
                                setBrainstormContinuationDirectionId(recommended?.id || '')
                                setBrainstormContinuationOpen(true)
                              }}
                              disabled={isGenerating || isBrainstormLoading}
                            >
                              <Lightbulb size={15} /> 继续脑暴
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => void handleGenerate()}
                            disabled={isGenerating || isBrainstormLoading}
                          >
                            {isGenerating
                              ? <Loader2 size={15} className="spin" />
                              : <Sparkles size={15} />}
                            继续生成文档内容
                          </button>
                        </div>
                      )}
                      {brainstormError && (
                        <div className="creation-brainstorm-card__error" role="alert">{brainstormError}</div>
                      )}
                    </div>
                  </div>
                )}
                {pendingConfirmation && (
                  <div className="creation-confirmation" role="group" aria-label="Agent 请求确认">
                    <div>
                      <Bot size={17} />
                      <span>
                        <strong>需要你确认</strong>
                        {pendingConfirmation.question}
                      </span>
                    </div>
                    <div>
                      <button type="button" onClick={() => void handleConfirmContinue()} disabled={isGenerating}>
                        <Check size={15} /> 按当前信息继续
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setPendingConfirmation(null)
                          window.requestAnimationFrame(() => promptInputRef.current?.focus())
                        }}
                      >
                        补充要求
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
            <div className="creation-prompt-skill-shell" ref={promptSkillShellRef}>
              <MentionHighlightTextarea
                ref={promptInputRef}
                value={prompt}
                mentionLabels={[
                  ...installedSkills.map(skill => skill.title),
                  ...attachments.filter(item => item.type.startsWith('image/')).map(item => item.name),
                ]}
                onChange={(event) => handlePromptChange(event.target.value, event.target.selectionStart)}
                onCompositionStart={promptImeGuard.onCompositionStart}
                onCompositionEnd={promptImeGuard.onCompositionEnd}
                onBlur={promptImeGuard.onBlur}
                onKeyDown={(event) => {
                  if (event.key === 'Escape' && skillPickerOpen) {
                    event.preventDefault()
                    setSkillPickerOpen(false)
                    setSkillQuery('')
                    return
                  }
                  if (skillPickerOpen && skillPickerItems.length && !promptImeGuard.isImeEvent(event)) {
                    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                      event.preventDefault()
                      const offset = event.key === 'ArrowDown' ? 1 : -1
                      setActiveSkillPickerIndex(current => (
                        (current + offset + skillPickerItems.length) % skillPickerItems.length
                      ))
                      return
                    }
                    if (event.key === 'Home' || event.key === 'End') {
                      event.preventDefault()
                      setActiveSkillPickerIndex(event.key === 'Home' ? 0 : skillPickerItems.length - 1)
                      return
                    }
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      selectPromptSkill(skillPickerItems[activeSkillPickerIndex])
                      return
                    }
                  }
                  if (
                    event.key === 'Enter'
                    && !event.shiftKey
                    && !skillPickerOpen
                    && !promptImeGuard.isImeEvent(event)
                  ) {
                    event.preventDefault()
                    if (prompt.trim() && !isGenerating && !isBrainstormLoading) void handleGenerate()
                  }
                }}
                onPaste={(event) => {
                  const files = Array.from(event.clipboardData.files || [])
                  if (files.some(file => file.type.startsWith('image/'))) {
                    event.preventDefault()
                    void addFiles(files, true)
                  }
                }}
                placeholder={generatedContent
                  ? '继续告诉 Agent 如何修改当前文档。Enter 发送，Shift+Enter 换行；输入 @ 可选择技能。'
                  : `${defaultPrompt}\n输入 @ 可选择已安装的技能。`}
                style={{ ...inputStyle, minHeight: conversation.length ? 82 : 112, resize: 'vertical', lineHeight: 1.6 }}
                disabled={isGenerating || isBrainstormLoading || Boolean(brainstormState) || sessionTerminated}
                aria-expanded={skillPickerOpen}
                aria-controls="creation-skill-picker"
                aria-activedescendant={skillPickerOpen && skillPickerItems.length
                  ? `creation-skill-option-${skillPickerItems[activeSkillPickerIndex]?.id}`
                  : undefined}
              />
              {skillPickerOpen && (
                <div className="creation-skill-picker" id="creation-skill-picker" role="listbox" aria-label="选择技能">
                  <header><AtSign size={15} /><span>选择已安装的技能</span><small>{skillPickerItems.length} 项</small></header>
                  {skillPickerItems.length ? skillPickerItems.map((skill, index) => (
                    <button
                      type="button"
                      role="option"
                      id={`creation-skill-option-${skill.id}`}
                      aria-selected={index === activeSkillPickerIndex}
                      key={skill.id}
                      onMouseDown={event => event.preventDefault()}
                      onMouseEnter={() => setActiveSkillPickerIndex(index)}
                      onClick={() => selectPromptSkill(skill)}
                    >
                      <strong>{skill.title}</strong>
                      <span>{skill.summary}</span>
                    </button>
                  )) : (
                    <div className="creation-skill-picker__empty">
                      {installedSkills.length ? '没有匹配的已安装技能。' : '还没有已安装的技能，请先到「技能」页面安装。'}
                    </div>
                  )}
                </div>
              )}
            </div>
            {matchedSkills.length > 0 && (
              <div className="creation-matched-skills" aria-label="本次使用的技能">
                <span>本次将使用</span>
                {matchedSkills.map(match => (
                  <span className="creation-matched-skill" key={match.skill.id}>
                    <button
                      type="button"
                      className="creation-matched-skill__detail"
                      onClick={() => showLocalSkillDetail(match.skill)}
                      aria-label={`查看技能：${match.skill.title}`}
                    >
                      <Sparkles size={13} />
                      <span>{match.skill.title}</span>
                      <small>@ 已选择</small>
                    </button>
                  </span>
                ))}
              </div>
            )}
            {attachments.length > 0 && (
              <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {attachments.map(item => (
                  <span key={item.id} style={attachmentPillStyle}>
                    <Paperclip size={13} />
                    <span style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.name}</span>
                    <small style={{ color: 'var(--mb-text-secondary)' }}>{formatAttachmentSize(item.size)}</small>
                    <button
                      type="button"
                      onClick={() => setAttachments(prev => prev.filter(existing => existing.id !== item.id))}
                      disabled={isGenerating}
                      style={attachmentRemoveStyle}
                      aria-label={`移除 ${item.name}`}
                    >
                      <X size={13} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {attachmentError && <div style={{ marginTop: 8, color: '#b42318', fontSize: 12 }}>{attachmentError}</div>}
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
            <input
              ref={folderInputRef}
              type="file"
              multiple
              style={{ display: 'none' }}
              aria-label="选择附件文件夹"
              onChange={(event) => {
                if (event.target.files) void addFiles(event.target.files)
                event.currentTarget.value = ''
              }}
              {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            />
            <div className="creation-composer-actions">
              <div className="creation-model-row">
                <div className="creation-composer-add" ref={composerAddMenuRef}>
                  <button
                    type="button"
                    className="creation-composer-add__trigger"
                    aria-label="添加"
                    aria-haspopup="menu"
                    aria-expanded={composerAddMenuOpen}
                    onClick={() => setComposerAddMenuOpen(open => !open)}
                    disabled={isGenerating}
                  >
                    <Plus size={20} strokeWidth={1.5} />
                  </button>
                  {composerAddMenuOpen && (
                    <div className="creation-composer-add__menu" role="menu" aria-label="添加内容和插件">
                      <span className="creation-composer-add__heading">添加</span>
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setComposerAddMenuOpen(false)
                          fileInputRef.current?.click()
                        }}
                      >
                        <Paperclip size={17} />
                        <span><strong>文件</strong><small>图片、PDF 或文档</small></span>
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setComposerAddMenuOpen(false)
                          folderInputRef.current?.click()
                        }}
                      >
                        <FolderOpen size={17} />
                        <span><strong>文件夹</strong><small>最多读取 6 个文件</small></span>
                      </button>
                      <span className="creation-composer-add__heading">插件</span>
                      <button
                        type="button"
                        role="menuitemcheckbox"
                        aria-checked={browserExtensionConnected && browserCrawlerEnabled}
                        disabled={!browserExtensionConnected}
                        onClick={() => {
                          if (!browserExtensionConnected) return
                          const enabled = !browserCrawlerEnabled
                          browserCrawlerPreferenceInitializedRef.current = true
                          setBrowserCrawlerEnabled(enabled)
                          window.localStorage.setItem(BROWSER_CRAWLER_PREFERENCE_KEY, String(enabled))
                        }}
                      >
                        <Globe2 size={17} />
                        <span>
                          <strong>浏览器爬虫</strong>
                        </span>
                        <i className={browserExtensionConnected && browserCrawlerEnabled ? 'is-on' : ''} aria-hidden><Check size={12} /></i>
                      </button>
                    </div>
                  )}
                </div>
                <ModelSelect
                  value={activeCreationModelId}
                  options={CREATION_MODEL_DEFS}
                  disabled={isGenerating}
                  remoteAllowed={remoteModelAllowed}
                  onChange={handleSelectModel}
                  title="选择创作生成模型"
                />
                <ModelSelect
                  className="creation-mode-select"
                  value={creationMode}
                  options={CREATION_MODE_OPTIONS}
                  disabled={isGenerating || isBrainstormLoading || Boolean(brainstormState)}
                  remoteAllowed
                  onChange={(mode) => setCreationMode(mode as CreationMode)}
                  renderIcon={(option) => option.id === 'brainstorm'
                    ? <Lightbulb size={16} />
                    : <Zap size={16} />}
                  title="选择创作模式"
                />
                {activeCreationModelId === REMOTE_CREATION_MODEL_ID && cloudBalance && (
                  <span style={{ color: 'var(--mb-text-secondary)', fontSize: 12 }}>
                    Credit {cloudBalance.available}
                  </span>
                )}
              </div>
              <div className="creation-action-buttons">
                <button
                  onClick={isGenerating ? handleStopGenerate : handleGenerate}
                  disabled={!isGenerating && (
                    Boolean(inlineRunningAction)
                    ||
                    sessionTerminated
                    ||
                    isBrainstormLoading
                    || (creationMode === 'brainstorm'
                      ? brainstormState ? brainstormState.phase !== 'ready' : !prompt.trim()
                      : !prompt.trim())
                  )}
                  style={isGenerating ? dangerButtonStyle : primaryButtonStyle}
                >
                  {isGenerating || isBrainstormLoading
                    ? <Loader2 size={16} className="spin" />
                    : generatedContent ? <Send size={16} /> : <Sparkles size={16} />}
                  {isGenerating
                    ? '中止'
                    : creationMode === 'brainstorm'
                      ? isBrainstormLoading
                        ? '正在梳理'
                        : brainstormState?.phase === 'ready'
                          ? generatedContent
                            ? '继续生成文档内容'
                            : '按此生成'
                          : brainstormState
                            ? '请回答上方问题'
                            : '开始梳理'
                      : generatedContent ? '发送' : '开始创作'}
                </button>
              </div>
            </div>
            {isGenerating && (
              <ProgressStrip
                label={`已执行 ${elapsedSeconds} 秒`}
                percent={generationProgress}
              />
            )}
            {error && <div style={{ marginTop: 12, color: '#b42318', fontSize: 13 }}>{error}</div>}
          </section>

          <div
            className="creation-workspace-divider"
            role="separator"
            tabIndex={0}
            aria-label="调整生成内容和创作对话的宽度"
            aria-orientation="vertical"
            aria-valuenow={Math.round(workspaceSplit)}
            aria-valuetext={`生成内容占 ${Math.round(workspaceSplit)}%`}
            onPointerDown={handleWorkspaceResizeStart}
            onKeyDown={handleWorkspaceResizeKeyDown}
          >
            <span className="creation-workspace-divider__handle" aria-hidden="true" />
          </div>

          <section className={`creation-document-section${fullscreenPanel === 'document' ? ' creation-panel-fullscreen' : ''}`} aria-label="生成内容" style={{ flex: 1, minHeight: 0, overflow: 'hidden', padding: 22 }}>
            <div className="creation-document-card" style={{ height: '100%', border: '1px solid var(--mb-border-strong)', borderRadius: 8, background: 'var(--mb-bg-card)', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              <div className="creation-document-header" style={{ height: 48, padding: '0 16px', borderBottom: '1px solid var(--mb-border-strong)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
                <span style={{ fontSize: 14, fontWeight: 650 }}>
                  {!generatedContent && brainstormState ? '创作简报' : '创作文档'}
                  {latestDocumentPatch && (
                    <small className="creation-document-patch-badge">
                      本轮改动 {latestPatchChangeCount || latestPatchTargets.length} 处
                    </small>
                  )}
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {isGenerating && (
                    <span style={{ fontSize: 12, color: '#a45d22', fontWeight: 650 }}>
                      {generationProgress}% · {elapsedSeconds} 秒
                    </span>
                  )}
                  {isGenerating && (
                    <button onClick={handleStopGenerate} style={compactDangerButtonStyle}>
                      <Square size={14} />
                      中止
                    </button>
                  )}
                  {inlineUndo && !isGenerating && !inlineRunningAction && (
                    <button type="button" onClick={() => void undoInlineEdit()} style={compactButtonStyle}>
                      撤销选区修改
                    </button>
                  )}
                  {inlineError && !inlineSelection && (
                    <span className="creation-inline-edit-status is-error" role="alert">{inlineError}</span>
                  )}
                  <button
                    type="button"
                    onClick={(event) => fullscreenPanel === 'document'
                      ? closeFullscreenPanel()
                      : openFullscreenPanel('document', event.currentTarget)}
                    style={compactButtonStyle}
                    aria-label={fullscreenPanel === 'document' ? '退出文档全屏' : '全屏查看文档'}
                    title={fullscreenPanel === 'document' ? '退出全屏（Esc）' : '全屏查看文档'}
                  >
                    {fullscreenPanel === 'document' ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
                    {fullscreenPanel === 'document' ? '退出全屏' : '全屏'}
                  </button>
                  <button onClick={handleCopy} disabled={!generatedContent} style={compactButtonStyle}>
                    <Copy size={15} />
                    {copySuccess ? '已复制' : '复制'}
                  </button>
                </div>
              </div>
              {currentDocumentSkills.length > 0 && (
                <div className="creation-document-skills" aria-label="当前文档关联技能">
                  <span>关联技能</span>
                  {currentDocumentSkills.map(skill => (
                    <button type="button" key={skill.id} onClick={() => showLocalSkillDetail(skill)}>
                      <Sparkles size={13} /> {skill.title}
                      <small>{skill.status === 'draft' ? '草稿' : skill.installed ? '已安装' : '已保存'}</small>
                    </button>
                  ))}
                </div>
              )}
              {latestDocumentPatch && (
                <div className="creation-latest-change-summary" aria-label="本轮改动">
                  <div>
                    <Sparkles size={15} />
                    <strong>本轮改动</strong>
                    {latestUserInstruction && <span>{latestUserInstruction}</span>}
                  </div>
                  <div>
                    {latestPatchChanges.slice(0, 12).map((change, index) => (
                      <span
                        key={`${change.changeType}-${change.sectionTitle}-${change.startLine}-${index}`}
                        className={`is-${change.changeType}`}
                      >
                        {changeTypeLabel(change.changeType)} · {change.sectionTitle}
                      </span>
                    ))}
                    {latestPatchChanges.length > 12 && (
                      <small>另有 {latestPatchChanges.length - 12} 处</small>
                    )}
                  </div>
                </div>
              )}
              <div ref={contentRef} className="creation-document-content" style={{ flex: 1, overflowY: 'auto', padding: 20 }}>
                {selectionStableContent ? (
                  <MarkdownContent
                    content={selectionStableContent}
                    components={markdownComponents}
                    changes={latestPatchChanges}
                  />
                ) : brainstormState?.brief_markdown ? (
                  <div className="creation-brainstorm-brief">
                    <div className="creation-brainstorm-brief__status">
                      <span>{brainstormState.answered_count} 项已确认</span>
                      <span>{brainstormState.open_flags.length} 项待决定</span>
                      <span>版本 {brainstormState.revision}</span>
                    </div>
                    <MarkdownContent
                      content={brainstormState.brief_markdown}
                      components={markdownComponents}
                      changes={[]}
                    />
                  </div>
                ) : isGenerating ? (
                  <div style={{ height: '100%', display: 'grid', placeItems: 'center', color: 'var(--mb-text-secondary)', fontSize: 14, gap: 12 }}>
                    <Loader2 size={28} className="spin" color="#a45d22" />
                    <div style={{ textAlign: 'center', lineHeight: 1.6 }}>
                      <div className="creation-deep-thinking-label">
                        <span className="creation-deep-thinking-label__icon" aria-hidden="true">
                          <img src="/brand/memorybread-bread-mark.png" alt="" />
                        </span>
                        <span>模型正在深度推理中</span>
                      </div>
                      <div>已思考 {elapsedSeconds} 秒，预计进度 {generationProgress}%</div>
                    </div>
                  </div>
                ) : (
                  <div style={{ height: '100%', display: 'grid', placeItems: 'center', color: 'var(--mb-text-tertiary)', fontSize: 14 }}>
                    输入创作需求后即可开始生成，Agent 会按需检索参考资料。
                  </div>
                )}
              </div>
              <CreationSelectionToolbar
                snapshot={inlineSelection}
                actions={inlineCapabilities?.actions || []}
                customPrompt={inlineCustomPrompt}
                maxCustomPromptBytes={inlineCapabilities?.max_custom_prompt_bytes || 0}
                promptOpen={inlinePromptOpen}
                runningAction={inlineRunningAction}
                error={inlineError || (
                  inlineCapabilities && !inlineCapabilities.enabled
                    ? inlineCapabilities.disabled_reason || '当前文档暂不支持选区编辑'
                    : ''
                )}
                onInteractionStart={() => {
                  inlineToolbarInteractionRef.current = true
                }}
                onInteractionEnd={() => {
                  window.requestAnimationFrame(() => {
                    inlineToolbarInteractionRef.current = false
                  })
                }}
                onCustomPromptChange={setInlineCustomPrompt}
                onPromptOpenChange={setInlinePromptOpen}
                onRun={action => void runInlineEdit(action)}
                onBrainstorm={() => void beginInlineBrainstorm()}
                onCancel={() => void cancelInlineEdit()}
                onClose={() => {
                  setInlineSelection(null)
                  setInlinePromptOpen(false)
                  setInlineCustomPrompt('')
                  setInlineError('')
                  window.getSelection()?.removeAllRanges()
                }}
              />
            </div>
          </section>

          {/* 底部互斥 Tab */}
          <div ref={bottomPanelRef} className="creation-bottom-panel" style={{ background: 'var(--mb-bg-card)', borderTop: '1px solid var(--mb-border-strong)', flexShrink: 0 }}>
            <div className="creation-bottom-tabs" role="tablist" aria-label="创作参考与参数" style={{ display: 'flex', alignItems: 'center', padding: '0 16px' }}>
              {([
                { key: 'reference', label: '参考资料', badge: referencePreview?.references?.length || 0 },
                { key: 'data', label: '参考数据', badge: dataReferences.length },
                { key: 'config', label: '创作参数', badge: 0 },
              ] as const).map(({ key, label, badge }) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  data-bottom-tab={key}
                  aria-selected={activeBottomTab === key}
                  aria-controls={`creation-bottom-panel-${key}`}
                  onClick={() => toggleBottomTab(key)}
                  style={{
                    padding: '10px 16px',
                    border: 'none',
                    borderTop: activeBottomTab === key ? '2px solid #a45d22' : '2px solid transparent',
                    background: 'none',
                    color: activeBottomTab === key ? '#a45d22' : '#667085',
                    fontWeight: activeBottomTab === key ? 650 : 400,
                    fontSize: 13,
                    cursor: 'pointer',
                  }}
                >
                  {label}{badge > 0 ? ` (${badge})` : ''}
                </button>
              ))}
              {activeBottomTab && (
                <>
                  {activeBottomTab !== 'config' && (
                    <button
                      type="button"
                      className="creation-bottom-fullscreen-button"
                      onClick={(event) => openFullscreenPanel(activeBottomTab, event.currentTarget)}
                      aria-label={`全屏查看${activeBottomTab === 'reference' ? '参考资料' : '参考数据'}`}
                      title="全屏查看"
                    >
                      <Maximize2 size={14} /> 全屏
                    </button>
                  )}
                  <button
                  onClick={() => setActiveBottomTab(null)}
                  style={{ marginLeft: 'auto', padding: '4px 10px', border: '1px solid var(--mb-border-strong)', borderRadius: 5, background: 'var(--mb-bg-inset)', color: 'var(--mb-text-secondary)', fontSize: 12, cursor: 'pointer' }}
                >
                  收起
                  </button>
                </>
              )}
            </div>
            {activeBottomTab === 'reference' && (
              <div ref={referencePanelRef} id="creation-bottom-panel-reference" role="tabpanel" aria-label="参考资料" className={fullscreenPanel === 'reference' ? 'creation-panel-fullscreen creation-reference-fullscreen' : ''} style={{ padding: 16, maxHeight: fullscreenPanel === 'reference' ? 'none' : 280, overflowY: 'auto', background: 'var(--mb-bg-elevated)', borderTop: '1px solid var(--mb-border-strong)' }}>
                {fullscreenPanel === 'reference' && (
                  <header className="creation-panel-fullscreen__header">
                    <strong>参考资料</strong>
                    <button type="button" onClick={closeFullscreenPanel} aria-label="退出参考资料全屏"><Minimize2 size={15} /> 退出全屏</button>
                  </header>
                )}
                {referenceGroups.length ? (
                  <div className="creation-reference-groups">
                    {referenceGroups.map(group => (
                      <section
                        id={group.id}
                        key={group.id}
                        tabIndex={-1}
                        className={`creation-reference-group${highlightedReferenceGroup === group.id ? ' is-highlighted' : ''}`}
                        aria-label={`${group.title}参考资料组`}
                      >
                        <header className="creation-reference-group__header">
                          <div>
                            <strong>{group.title}</strong>
                            <span>{group.toolName} · {group.items.length} 条</span>
                          </div>
                          {group.legacy && <small>历史汇总</small>}
                        </header>
                        {group.query && <p className="creation-reference-group__query">{group.query}</p>}
                        <div className="creation-reference-group__items">
                          {group.items.map((ref, index) => (
                            <ReferenceRow
                              key={`${ref.source_type || 'document'}-${ref.source_id ?? ref.id}-${index}`}
                              item={ref}
                              onOpenSource={handleOpenReferenceSource}
                            />
                          ))}
                        </div>
                      </section>
                    ))}
                  </div>
                ) : (
                  <div style={{ color: 'var(--mb-text-secondary)', fontSize: 13 }}>暂无资料；开始创作后，Agent 会按需补充参考资料。</div>
                )}
              </div>
            )}
            {activeBottomTab === 'data' && (
              <div ref={dataPanelRef} id="creation-bottom-panel-data" role="tabpanel" aria-label="参考数据" className={`creation-data-references${fullscreenPanel === 'data' ? ' creation-panel-fullscreen' : ''}`}>
                {fullscreenPanel === 'data' && (
                  <header className="creation-panel-fullscreen__header">
                    <strong>参考数据</strong>
                    <button type="button" onClick={closeFullscreenPanel} aria-label="退出参考数据全屏"><Minimize2 size={15} /> 退出全屏</button>
                  </header>
                )}
                {dataReferencesLoading && (
                  <div className="creation-data-reference-notice" role="status">
                    <Loader2 size={14} className="spin" /> 正在加载参考数据详情…
                  </div>
                )}
                {legacyDataReferencesRecovered && (
                  <div className="creation-data-reference-notice">
                    该历史版本未保存来源编号，以下内容按原始需求从当前本地数据恢复。
                  </div>
                )}
                {dataReferencesError && (
                  <div className="creation-data-reference-error" role="alert">{dataReferencesError}</div>
                )}
                {dataReferenceGroups.length ? (
                  <div className="creation-reference-groups creation-data-reference-list">
                    {dataReferenceGroups.map(group => (
                      <section
                        id={group.id}
                        key={group.id}
                        tabIndex={-1}
                        className={`creation-reference-group${highlightedReferenceGroup === group.id ? ' is-highlighted' : ''}`}
                        aria-label={`${group.title}参考数据组`}
                      >
                        <header className="creation-reference-group__header">
                          <div>
                            <strong>{group.title}</strong>
                            <span>{group.toolName} · {group.items.length} 个来源</span>
                          </div>
                          {group.legacy && <small>历史汇总</small>}
                        </header>
                        {group.query && <p className="creation-reference-group__query">{group.query}</p>}
                        <div className="creation-reference-group__items">
                          {group.items.map((item, index) => (
                            <DataReferenceRow
                              key={`${item.source_id}-${index}`}
                              item={item}
                              source={dataSourcesById[item.source_id]}
                              loading={dataReferencesLoading && !dataSourcesById[item.source_id]}
                            />
                          ))}
                        </div>
                      </section>
                    ))}
                  </div>
                ) : !dataReferencesLoading && (
                  <div className="creation-bottom-empty">暂无参考数据，数据检索完成后会显示召回来源和具体指标。</div>
                )}
              </div>
            )}
            {activeBottomTab === 'config' && (
              <div id="creation-bottom-panel-config" role="tabpanel" aria-label="创作参数" style={{ padding: 16, maxHeight: 280, overflowY: 'auto', background: 'var(--mb-bg-elevated)', borderTop: '1px solid var(--mb-border-strong)', display: 'grid', gap: 12 }}>
                <label style={{ display: 'grid', gap: 7, fontSize: 13 }}>
                  文档类型
                  <input value={docType} onChange={(e) => setDocType(e.target.value)} placeholder="建设方案" style={inputStyle} />
                </label>
                <label style={{ display: 'grid', gap: 7, fontSize: 13 }}>
                  目标读者
                  <input value={audience} onChange={(e) => setAudience(e.target.value)} placeholder="客户" style={inputStyle} />
                </label>
                <Toggle label="继承历史格式" checked={inheritFormat} onChange={setInheritFormat} />
                <Toggle label="图片生成建议" checked={enableImageGeneration} onChange={setEnableImageGeneration} icon={<Image size={16} />} />
                <div style={{ height: 1, background: 'var(--mb-border-strong)', margin: '4px 0' }} />
                <div style={{ fontSize: 12, color: 'var(--mb-text-secondary)', display: 'flex', justifyContent: 'space-between' }}>
                  <span>权重配置</span>
                  <span style={{ color: totalWeight === 100 ? '#a45d22' : '#b54708' }}>{totalWeight}%</span>
                </div>
                <WeightSlider label="内容相关度" value={contentWeight} onChange={setContentWeight} />
                <WeightSlider label="文档质量" value={qualityWeight} onChange={setQualityWeight} />
                <WeightSlider label="完整性" value={completenessWeight} onChange={setCompletenessWeight} />
                <WeightSlider label="打开/引用热度" value={usageWeight} onChange={setUsageWeight} />
                <WeightSlider label="格式匹配" value={formatWeight} onChange={setFormatWeight} />
                <WeightSlider label="时效性" value={freshnessWeight} onChange={setFreshnessWeight} />
              </div>
            )}
          </div>
        </main>
      )}

      {skillEditor && (
        <CreationSkillEditor
          source={skillEditor.source}
          initialSkill={skillEditor.initialSkill}
          onClose={() => setSkillEditor(null)}
          onSaved={handleSkillSaved}
        />
      )}

      {skillDetail && (
        <CreationSkillDetail
          skill={skillDetail}
          onClose={closeSkillDetail}
          focusFiles={skillDetailFocusFiles}
          primaryAction={skillDetailMarketItem
            ? {
              label: skillDetail.installed ? '卸载技能' : '安装技能',
              loadingLabel: skillDetail.installed ? '正在卸载…' : '正在安装…',
              loading: installingMarketSkillId === skillDetailMarketItem.id,
              onClick: () => void handleInstallMarketSkill(skillDetailMarketItem),
            }
            : undefined}
        />
      )}

      {skillPendingDelete && (
        <div
          className="creation-skill-modal"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && deletingSkillId === null) {
              setSkillPendingDelete(null)
            }
          }}
        >
          <section
            className="creation-skill-delete-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="creation-skill-delete-title"
            aria-describedby="creation-skill-delete-description"
          >
            <span className="creation-skill-delete-confirm__icon" aria-hidden="true">
              <Trash2 size={20} />
            </span>
            <div>
              <h2 id="creation-skill-delete-title">确认删除技能？</h2>
              <p id="creation-skill-delete-description">
                将删除“{skillPendingDelete.title}”及其本地文件。此操作不会影响原始文档，但删除后无法恢复。
              </p>
            </div>
            <footer>
              <button
                type="button"
                autoFocus
                onClick={() => setSkillPendingDelete(null)}
                disabled={deletingSkillId !== null}
              >
                取消
              </button>
              <button
                type="button"
                className="is-danger"
                onClick={() => void confirmDeleteSkill()}
                disabled={deletingSkillId !== null}
              >
                {deletingSkillId !== null ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />}
                {deletingSkillId !== null ? '正在删除…' : '确认删除'}
              </button>
            </footer>
          </section>
        </div>
      )}

    </div>
  )
}

const displayCreationToolNames = (value: unknown) =>
  String(value || '').replace(
    /(互联网检索|记忆搜索|数据检索|网页爬取|PlantUML 画图|GitHub 检索) Tool/g,
    '$1',
  )

const displayAgentName = (event: CreationAgentEvent) => {
  if (event.actor?.id === 'creation_main_agent') return '创作 Agent'
  return displayCreationToolNames(event.actor?.name || '创作 Agent')
    .replace(/创作主 Agent/g, '创作 Agent')
}

const displayAgentText = (value: unknown) =>
  displayCreationToolNames(value)
    .replace(/创作主 Agent/g, '创作 Agent')
    // 历史记录里“创作 Agent · 标题”类摘要改写为动作描述，与后端新措辞对齐
    .replace(/创作 Agent · ([^\n，。]+?) 开始执行/g, '正在生成「$1」内容')
    .replace(/创作 Agent · ([^\n，。]+?) 已完成，并把结果写回创作环境/g, '已生成「$1」内容，并把结果写回创作文档')
    .replace(/创作 Agent · ([^\n，。]+?) 已完成当前 Skill 步骤/g, '已生成「$1」内容，并把结果写回创作文档')
    .replace(/质检通过，Harness 结束本轮优化循环/g, '质量检查通过')
    .replace(/已达到质检循环上限，Harness 保留剩余问题供用户复核/g, '已达到自动优化上限，剩余问题需要人工复核')
    .replace(/Harness 根据 [^\n，。]+反馈追加[:：][^\n]*/g, '已根据反馈补充后续处理')
    .replace(/Harness 根据 [^\n，。]+反馈跳过不必要的(?:后续|数据)步骤/g, '已根据反馈保留当前处理计划')

const agentEventCapabilityLabels = new Map<string, string>([
  ['creation_main_agent', '创作 Agent'],
  ...CREATION_SKILL_AGENT_OPTIONS.map(item => [item.id, item.label] as [string, string]),
  ...CREATION_SKILL_TOOL_OPTIONS.map(item => [item.id, item.label] as [string, string]),
])

const agentEventStatusLabels: Record<string, string> = {
  waiting: '等待中',
  running: '进行中',
  completed: '已完成',
  warning: '有警告',
  failed: '未完成',
  cancelled: '已结束',
  paused: '已暂停',
}

const harnessDecisionReasonLabels: Record<string, string> = {
  data_search_failed: '数据检索未完成，继续使用其他可用资料',
  refresh_required: '发现需要即时刷新的报表',
  snapshot_ready: '已有可分析的数据快照',
  source_metadata_only: '只找到来源信息，暂时没有可用数据',
  no_matching_data: '没有找到匹配的数据来源',
  refresh_failed_stale_snapshot_available: '即时刷新失败，保留历史快照并标记时效',
  refresh_feedback_ready: '即时采集完成，可以继续分析最新数据',
  refresh_failed_without_snapshot: '即时刷新失败，也没有可用历史快照',
  refresh_returned_no_analyzable_data: '页面已刷新，但没有提取到可分析数据',
  quality_review_failed: '质量检查未完成',
  quality_gate_passed: '质量要求已满足',
  quality_cycle_budget_exhausted: '已达到自动优化上限',
  quality_issues_detected: '发现可继续优化的问题',
  quality_issues_deferred: '已尝试自动修复但仍有遗留，保留当前版本',
  hard_failure_retry_exhausted: '完整文档重试后仍有阻断问题',
}

const qualityIssueLabels: Record<string, string> = {
  has_document: '正文内容不足',
  has_structure: '章节结构不足',
  revision_changed: '修订没有产生有效变化',
  ai_style_signals: '表达方式需要自然化',
  detail_incomplete: '章节细节不完整',
  table_needs_polish: '表格结构需要优化',
  emphasis_needs_polish: '重点标识需要优化',
  visual_needs_polish: '关键关系需要图示说明',
}

const agentEventCapabilityLabel = (value: unknown) =>
  agentEventCapabilityLabels.get(String(value || '')) || '其他处理步骤'

const CREATION_THINKING_STAGE_LABELS: Record<string, string> = {
  intent: '理解本轮要求',
  routing: '决定执行链路',
  generation: '生成文档内容',
  planning: '规划下一步',
}

const AgentExecutionTrace = ({
  events,
  onOpenReferences,
  browserLiveJob,
  apiBaseUrl,
}: {
  events: CreationAgentEvent[]
  onOpenReferences: (
    tab: Extract<BottomTab, 'reference' | 'data'>,
    groupId?: string,
  ) => void
  browserLiveJob?: BrowserLiveJob
  apiBaseUrl: string
}) => {
  const [expanded, setExpanded] = useState(true)
  const [openBlocks, setOpenBlocks] = useState<Record<string, boolean>>({})
  if (!events.length) return null
  const latestGoal = [...events].reverse().find(event => event.goal)?.goal
  const segments = segmentAgentTrace(events)
  type RenderThinkingSegment = TraceThinkingSegment & { innerGroups: AgentEventGroup[] }
  type RenderPhaseSegment = TracePhaseSegment & {
    innerSegments: Array<TraceStepSegment | RenderThinkingSegment>
  }
  type RenderSegment = TraceStepSegment | RenderThinkingSegment | RenderPhaseSegment
  const toRenderThinking = (segment: TraceThinkingSegment): RenderThinkingSegment => ({
    ...segment,
    innerGroups: groupConsecutiveAgentEvents(segment.innerEvents),
  })
  const renderSegments: RenderSegment[] = segments.map((segment) => {
    if (segment.kind === 'step') return segment
    if (segment.kind === 'thinking') return toRenderThinking(segment)
    return {
      ...segment,
      innerSegments: segment.segments.map((inner) => (
        inner.kind === 'thinking' ? toRenderThinking(inner) : inner
      )) as Array<TraceStepSegment | RenderThinkingSegment>,
    }
  })
  const flatGroups: AgentEventGroup[] = []
  const collectGroups = (list: Array<TraceStepSegment | RenderThinkingSegment>) => {
    list.forEach((segment) => {
      if (segment.kind === 'step') flatGroups.push(segment.group)
      else segment.innerGroups.forEach(group => flatGroups.push(group))
    })
  }
  renderSegments.forEach((segment) => {
    if (segment.kind === 'phase') collectGroups(segment.innerSegments)
    else collectGroups([segment])
  })
  // 阶段按出现顺序编号，作为顶层执行步骤的序号
  const phaseIndexes = new Map<string, number>()
  let phaseCounter = 0
  renderSegments.forEach((segment) => {
    if (segment.kind !== 'phase') return
    phaseCounter += 1
    phaseIndexes.set(segment.key, phaseCounter)
  })
  const runTerminalEvent = terminalEventForLatestRun(events)
  const runWasCancelled = runTerminalEvent?.type === 'run.cancelled'
  const runFailed = runTerminalEvent?.type === 'run.failed'
  const resolveGroupStatus = (group: AgentEventGroup, flatIndex: number) => {
    const latestEvent = group.events[group.events.length - 1]
    if (latestEvent.status !== 'running') return latestEvent.status
    const resolved = flatGroups
      .slice(flatIndex + 1)
      .some(next => (
        next.events.some(event => (
          event.actor?.id === latestEvent.actor?.id
          && ['completed', 'warning', 'failed'].includes(event.status)
        ))
      ))
    if (resolved) return 'completed'
    if (runWasCancelled) return 'cancelled'
    if (runFailed) return 'failed'
    return latestEvent.status
  }
  const groupStatusByKey = new Map<string, string>()
  flatGroups.forEach((group, flatIndex) => {
    groupStatusByKey.set(group.key, resolveGroupStatus(group, flatIndex))
  })
  const runningGroups = [...flatGroups].reverse().filter(group => (
    groupStatusByKey.get(group.key) === 'running'
  ))
  const browserLiveGroupKey = browserLiveJob
    ? runningGroups.find(group => group.events.some(event => event.actor?.id === 'webpage_scrape'))?.key
      || runningGroups[0]?.key
    : undefined
  const webpageScrapeTerminalStatus = [...events]
    .reverse()
    .find(event => (
      event.actor?.id === 'webpage_scrape'
      && ['tool.completed', 'tool.failed'].includes(event.type)
    ))
    ?.status

  const toggleBlock = (key: string) => {
    setOpenBlocks(previous => ({ ...previous, [key]: !previous[key] }))
  }

  const collectThinkingKeys = (list: RenderSegment[]): string[] => list.flatMap((segment) => {
    if (segment.kind === 'thinking') return [segment.key]
    if (segment.kind === 'phase') {
      return [
        segment.key,
        ...segment.innerSegments
          .filter((inner): inner is RenderThinkingSegment => inner.kind === 'thinking')
          .map(inner => inner.key),
      ]
    }
    return []
  })

  const allBlockKeys = [
    ...flatGroups.map(group => group.key),
    ...collectThinkingKeys(renderSegments),
  ]
  const openBlockCount = allBlockKeys.filter(key => openBlocks[key]).length
  const allExpanded = allBlockKeys.length > 0 && openBlockCount === allBlockKeys.length
  const toggleAllBlocks = () => {
    const nextOpen = !allExpanded
    setOpenBlocks(Object.fromEntries(allBlockKeys.map(key => [key, nextOpen])))
  }

  const renderStepGroup = (group: AgentEventGroup) => {
    const actorEvent = group.events[0]
    const latestEvent = group.events[group.events.length - 1]
    const displayStatus = groupStatusByKey.get(group.key) || latestEvent.status
    const isActive = displayStatus === 'running'
    const showDetails = isActive || Boolean(openBlocks[group.key])
    // 行标题突出动作目的；召回数量等次级结果拆成灰色小字
    const headline = splitHeadline(displayAgentText(latestEvent.summary))
    return (
      <div
        className={`creation-agent-event is-${displayStatus}${showDetails ? '' : ' is-collapsed'}`}
        key={group.key}
      >
        <div
          className="creation-agent-event__row"
          role="button"
          tabIndex={0}
          onClick={() => toggleBlock(group.key)}
          onKeyDown={(keyEvent) => {
            if (keyEvent.key !== 'Enter' && keyEvent.key !== ' ') return
            keyEvent.preventDefault()
            toggleBlock(group.key)
          }}
          aria-expanded={showDetails}
        >
          <span
            className={`creation-agent-event__icon is-${actorEvent.actor?.kind || 'agent'}${['completed', 'warning', 'failed', 'cancelled'].includes(displayStatus) ? ' is-dot' : ''}`}
            aria-hidden="true"
          >
            {displayStatus === 'completed'
                ? <span className="creation-agent-event__dot is-done" />
                : displayStatus === 'warning'
                  ? <span className="creation-agent-event__dot is-warning" />
                : displayStatus === 'failed'
                  ? <span className="creation-agent-event__dot is-failed" />
                  : displayStatus === 'cancelled'
                    ? <span className="creation-agent-event__dot is-cancelled" />
                  : actorEvent.actor?.kind === 'tool'
                    ? <Wrench size={13} />
                    : actorEvent.actor?.kind === 'skill'
                      ? <Sparkles size={13} />
                      : <Bot size={13} />}
            {displayStatus === 'running' && (
              <span className="creation-agent-event__activity" />
            )}
          </span>
          <span className="creation-agent-event__headline">
            {/* 行标题直接表达动作目的；次级结果置灰，具体调用的能力退到展开细节 */}
            <strong>{headline.main}</strong>
            {headline.sub && (
              <span className="creation-agent-event__headline-sub">{headline.sub}</span>
            )}
          </span>
        </div>
        {browserLiveJob && browserLiveGroupKey === group.key && (
          <BrowserLiveDock job={browserLiveJob} apiBaseUrl={apiBaseUrl} />
        )}
        {showDetails && (
          <div className="creation-agent-event__updates">
            <div className="creation-agent-event__capability">
              {actorEvent.actor?.id === 'creation_main_agent'
                ? displayAgentName(actorEvent)
                : `${displayAgentName(actorEvent)} · ${agentActorKindLabel(actorEvent.actor?.kind)}`}
            </div>
            {group.events.map((event) => {
              const details = agentEventDetails(event)
              return (
                <div
                  className="creation-agent-event__update"
                  key={event.event_id || `${event.run_id}-${event.sequence}`}
                >
                  {/* 最新事件的 summary 已提升为行标题，细节里只保留链路与摘要类信息 */}
                  <AgentEventSummary
                    event={event}
                    onOpenReferences={onOpenReferences}
                    omitHeadlineText={event === latestEvent}
                  />
                  {details.length > 0 && (
                    <dl>
                      {details.map(detail => (
                        <React.Fragment key={`${event.event_id}-${detail.label}`}>
                          <dt>{detail.label}</dt>
                          <dd>{detail.value}</dd>
                        </React.Fragment>
                      ))}
                    </dl>
                  )}
                  <BrowserPreviewStrip
                    event={event}
                    terminalStatus={webpageScrapeTerminalStatus}
                  />
                </div>
              )
            })}
          </div>
        )}
      </div>
    )
  }

  const renderThinkingSegment = (segment: RenderThinkingSegment) => {
    const isRunning = segment.status === 'running' && !runTerminalEvent
    const showBody = isRunning || Boolean(openBlocks[segment.key])
    const stageLabel = CREATION_THINKING_STAGE_LABELS[segment.stage] || ''
    const durationSeconds = segment.durationMs == null
      ? null
      : Math.max(1, Math.round(segment.durationMs / 1000))
    return (
      <div
        className={`creation-trace-thinking${isRunning ? ' is-running' : ''}`}
        key={segment.key}
      >
        <button
          type="button"
          className="creation-trace-thinking__header"
          onClick={() => toggleBlock(segment.key)}
          aria-expanded={showBody}
        >
          <span className="creation-trace-thinking__dot" aria-hidden="true" />
          <span className="creation-trace-thinking__title">
            {isRunning ? `深度思考中${stageLabel ? `：${stageLabel}` : ''}` : '深度思考'}
          </span>
          {!isRunning && (durationSeconds != null || stageLabel) && (
            <span className="creation-trace-thinking__meta">
              {[durationSeconds != null ? `${durationSeconds}s` : null, stageLabel || null]
                .filter(Boolean)
                .join(' · ')}
            </span>
          )}
          {!isRunning && segment.reasoning && (
            <span className="creation-trace-thinking__reasoning">
              {segment.reasoning}
            </span>
          )}
        </button>
        {showBody && (
          <div className="creation-trace-thinking__body">
            {segment.reasoning && (
              <p className="creation-trace-thinking__reasoning-full">
                {segment.reasoning}
              </p>
            )}
            {segment.innerGroups.map(group => renderStepGroup(group))}
          </div>
        )}
      </div>
    )
  }

  const renderInnerSegments = (list: Array<TraceStepSegment | RenderThinkingSegment>) => (
    list.map((segment) => {
      if (segment.kind === 'step') return renderStepGroup(segment.group)
      return renderThinkingSegment(segment)
    })
  )

  // 顶层阶段行：序号 + 阶段标题 + 灰色耗时，展开后是思考/动作块，竖线标识层级
  const renderPhaseSegment = (segment: RenderPhaseSegment) => {
    const isRunning = segment.status === 'running' && !runTerminalEvent
    const hasWarning = !isRunning && segment.innerSegments.some((inner) => (
      inner.kind === 'step'
        ? ['warning', 'failed'].includes(groupStatusByKey.get(inner.group.key) || '')
        : inner.innerGroups.some(group => (
          ['warning', 'failed'].includes(groupStatusByKey.get(group.key) || '')
        ))
    ))
    const showBody = isRunning || Boolean(openBlocks[segment.key])
    const durationSeconds = segment.durationMs == null
      ? null
      : Math.max(1, Math.round(segment.durationMs / 1000))
    return (
      <div
        className={`creation-trace-phase${isRunning ? ' is-running' : hasWarning ? ' is-warning' : ' is-completed'}`}
        key={segment.key}
      >
        <button
          type="button"
          className="creation-trace-phase__header"
          onClick={() => toggleBlock(segment.key)}
          aria-expanded={showBody}
        >
          <span
            className={`creation-trace-phase__dot${isRunning ? '' : hasWarning ? ' is-warning' : ' is-done'}`}
            aria-hidden="true"
          />
          <span className="creation-trace-phase__title">
            {`${phaseIndexes.get(segment.key) || ''}. ${segment.title}`}
          </span>
          <span className="creation-trace-phase__meta">
            {isRunning
              ? '进行中'
              : runWasCancelled
                ? '已结束'
                : runFailed
                  ? '未完成'
                  : hasWarning
                    ? '有警告'
                    : durationSeconds != null ? `${durationSeconds}s` : '已完成'}
          </span>
        </button>
        {showBody && (
          <div className="creation-trace-phase__body">
            {renderInnerSegments(segment.innerSegments)}
          </div>
        )}
      </div>
    )
  }

  const renderTraceSegments = (list: RenderSegment[]) => list.map((segment) => {
    if (segment.kind === 'phase') return renderPhaseSegment(segment)
    if (segment.kind === 'step') return renderStepGroup(segment.group)
    return renderThinkingSegment(segment)
  })

  const phaseCount = renderSegments.filter(segment => segment.kind === 'phase').length

  return (
    <section className="creation-agent-trace" aria-label="Agent 执行情况">
      <div className="creation-agent-trace__header">
        <button
          type="button"
          className="creation-agent-trace__toggle"
          onClick={() => setExpanded(value => !value)}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <span>执行过程</span>
          <small>
            {latestGoal && `目标修订 ${latestGoal.revision} · `}
            {phaseCount > 0
              ? `${phaseCount} 个阶段 · ${flatGroups.length} 个步骤`
              : `${flatGroups.length} 个步骤`}
          </small>
        </button>
        {expanded && allBlockKeys.length > 0 && (
          <button
            type="button"
            className="creation-agent-trace__expand-all"
            onClick={toggleAllBlocks}
          >
            {allExpanded ? '收起全部' : '展开全部'}
          </button>
        )}
      </div>
      {expanded && (
        <div className="creation-agent-trace__runs">
          <div className="creation-agent-run">
            <div>
              {renderTraceSegments(renderSegments)}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

const AgentEventSummary = ({
  event,
  onOpenReferences,
  omitHeadlineText = false,
}: {
  event: CreationAgentEvent
  onOpenReferences: (
    tab: Extract<BottomTab, 'reference' | 'data'>,
    groupId?: string,
  ) => void
  omitHeadlineText?: boolean
}) => {
  const text = displayAgentText(event.summary)
  const routingPrefix = '已由模型决定执行链路：'
  const routingSteps = text.startsWith(routingPrefix)
    ? text
      .slice(routingPrefix.length)
      .split(/[、\n]+/)
      .map(item => item.trim())
      .filter(Boolean)
    : []
  const tab = event.type === 'tool.completed' && event.actor?.id === 'memory_search'
    ? 'reference'
    : event.type === 'tool.completed' && ['data_search', 'webpage_scrape'].includes(event.actor?.id || '')
      ? 'data'
      : null
  const resultCount = Number(event.data?.result_count)
  if (routingSteps.length) {
    return (
      <small className="creation-agent-route-summary">
        <span>{routingPrefix.slice(0, -1)}</span>
        <span className="creation-agent-route-summary__steps">
          {routingSteps.map((step, index) => (
            <span key={`${step}-${index}`}>{step}</span>
          ))}
        </span>
      </small>
    )
  }
  if (!tab || !Number.isFinite(resultCount) || resultCount <= 0) {
    // 行标题已展示 summary 时，纯文本不再重复；但链路摘要与参考链接仍保留
    return omitHeadlineText ? null : <small>{text}</small>
  }

  const match = tab === 'reference'
    ? text.match(/召回\s*\d+\s*条(?:本地)?资料/)
    : text.match(/召回\s*\d+\s*个来源/)
  if (!match || match.index == null) return omitHeadlineText ? null : <small>{text}</small>

  const before = text.slice(0, match.index)
  const after = text.slice(match.index + match[0].length)
  return (
    <small>
      {before}
      <button
        type="button"
        className="creation-agent-reference-link"
        onClick={(clickEvent) => {
          // 避免触发外层动作行的展开/收起切换
          clickEvent.stopPropagation()
          onOpenReferences(tab, tab === 'reference' ? referenceGroupId(event) : dataGroupId(event))
        }}
        aria-label={`${match[0]}，打开${tab === 'reference' ? '参考资料' : '参考数据'}`}
      >
        {match[0]}
      </button>
      {after}
    </small>
  )
}

const BrowserPreviewStrip = ({
  event,
  terminalStatus,
}: {
  event: CreationAgentEvent
  terminalStatus?: string
}) => {
  if (!['browser.preview.started', 'browser.preview.completed'].includes(event.type)) return null
  const previews = (Array.isArray(event.data?.previews) ? event.data.previews : [])
    .filter((item): item is BrowserPreviewItem => Boolean(
      item
      && typeof item === 'object'
      && typeof (item as BrowserPreviewItem).id === 'string'
      && typeof (item as BrowserPreviewItem).image_url === 'string',
    ))
  if (!previews.length) return null
  const status = event.type === 'browser.preview.completed'
    ? 'completed'
    : terminalStatus || 'running'

  return (
    <div className="creation-browser-previews" aria-label="证据截图预览">
      {previews.map(preview => (
        <BrowserPreviewCard
          key={`${event.event_id}-${preview.id}`}
          preview={preview}
          status={preview.status || status}
        />
      ))}
    </div>
  )
}

const BROWSER_LIVE_STAGE_LABELS: Record<string, string> = {
  queued: '等待浏览器',
  opening: '创建后台页面',
  loading: '加载页面',
  reading: '读取页面数据',
  finalizing: '整理采集结果',
  complete: '采集完成',
  failed: '采集未完成',
  timeout: '页面加载超时',
  disconnected: '浏览器连接中断',
}

const BrowserLiveDock = ({
  job,
  apiBaseUrl,
}: {
  job: BrowserLiveJob
  apiBaseUrl: string
}) => {
  const isRunning = ['queued', 'running'].includes(job.status)
  const previewUrl = `${apiBaseUrl}/api/browser-integration/jobs/${job.browser_job_id}/preview?revision=${job.preview_revision}`
  let hostname = job.url
  try {
    hostname = new URL(job.url).hostname
  } catch {
    // 保留原始地址作为回退文案。
  }
  return (
    <section className={`creation-browser-live is-${job.status}`} aria-label="浏览器现场">
      <div className="creation-browser-live__header">
        <span className="creation-browser-live__eyebrow">
          <i aria-hidden /> 浏览器现场
        </span>
        <span className="creation-browser-live__stage">
          {isRunning && <Loader2 size={12} className="spin" />}
          {BROWSER_LIVE_STAGE_LABELS[job.stage] || (isRunning ? '后台读取中' : '采集已结束')}
        </span>
      </div>
      <div className="creation-browser-live__body">
        <div className="creation-browser-live__screen">
          {job.has_preview ? (
            <img src={previewUrl} alt={`${job.title || hostname}后台浏览器实时画面`} />
          ) : (
            <div className="creation-browser-live__waiting">
              <Globe2 size={22} />
              <span>正在连接页面画面</span>
              <i aria-hidden />
            </div>
          )}
        </div>
        <div className="creation-browser-live__meta">
          <span className="creation-browser-live__title">{job.title || '正在打开页面'}</span>
          <span>{hostname}</span>
        </div>
      </div>
    </section>
  )
}

const BrowserPreviewCard = ({
  preview,
  status,
}: {
  preview: BrowserPreviewItem
  status: string
}) => {
  const apiBaseUrl = useAppStore(state => state.apiBaseUrl)
  const [revision, setRevision] = useState(0)
  const [loaded, setLoaded] = useState(false)
  const isRunning = status === 'running'

  useEffect(() => {
    if (!isRunning) return undefined
    const timer = window.setInterval(() => setRevision(value => value + 1), 900)
    return () => window.clearInterval(timer)
  }, [isRunning])

  const absoluteUrl = preview.image_url.startsWith('/')
    ? `${apiBaseUrl}${preview.image_url}`
    : preview.image_url
  const separator = absoluteUrl.includes('?') ? '&' : '?'
  const imageUrl = isRunning ? `${absoluteUrl}${separator}preview=${revision}` : absoluteUrl
  const statusLabel = status === 'completed'
    ? '采集完成'
    : status === 'failed'
      ? '采集未完成'
      : status === 'rejected'
        ? '证据未通过'
        : '证据截图中'

  return (
    <figure className={`creation-browser-preview is-${status}`}>
      <a
        className="creation-browser-preview__viewport"
        href={imageUrl}
        target="_blank"
        rel="noopener noreferrer"
        title={isRunning ? '查看当前页面画面' : '打开完整网页长截图'}
      >
        {!loaded && (
          <span className="creation-browser-preview__loading">
            {isRunning ? <Loader2 size={15} className="spin" /> : <Eye size={15} />}
            {isRunning ? '正在等待页面画面…' : '缩略图暂不可用'}
          </span>
        )}
        <img
          src={imageUrl}
          alt={`${preview.title || '数据页面'}网页证据缩略图`}
          onLoad={() => setLoaded(true)}
          onError={() => setLoaded(false)}
          className={loaded ? 'is-loaded' : ''}
        />
        <span className="creation-browser-preview__live">{statusLabel}</span>
      </a>
      <figcaption>
        <strong>{preview.title || '实时数据页面'}</strong>
        <span>
          {preview.browser ? `${preview.browser} · ` : ''}
          {status === 'completed' ? '完整长图 · 点击查看' : '每个页面仅临时切换一次'}
        </span>
      </figcaption>
    </figure>
  )
}

const agentActorKindLabel = (kind?: string) => {
  if (kind === 'tool') return 'Tool'
  if (kind === 'skill') return '技能'
  return 'Agent'
}

const agentEventDetails = (event: CreationAgentEvent) => {
  const details: Array<{ label: string; value: string }> = []
  const data = event.data || {}
  const patch = (data.patch || event.environment_patch?.document_patch) as Record<string, unknown> | undefined
  const targetSections = (
    Array.isArray(data.target_sections)
      ? data.target_sections
      : Array.isArray(patch?.target_sections)
        ? patch?.target_sections
        : []
  ).map(item => String(item)).filter(Boolean)

  if (event.type === 'intent.interpreted') {
    const root = String(data.root_request || '').trim()
    if (root) details.push({ label: '原始需求', value: root })
    const current = String(data.current_instruction || '').trim()
    if (current && current !== root) details.push({ label: '本轮要求', value: current })
  }
  const reasoning = String(data.reasoning_summary || '').trim()
  if (reasoning) details.push({ label: '判断摘要', value: reasoning })
  if (targetSections.length) {
    details.push({ label: '变更范围', value: targetSections.join('、') })
  }
  if (event.actor?.kind === 'tool' && Number.isFinite(Number(data.result_count))) {
    details.push({ label: '结果', value: `${Number(data.result_count)} 条` })
  }
  const rejectedSources = normalizeRejectedDataSources(
    data.rejected_sources || event.environment_patch?.rejected_sources,
  )
  if (rejectedSources.length) {
    details.push({
      label: '未采用来源',
      value: rejectedSources
        .map(source => `${source.title} · ${source.url || '地址暂缺'}`)
        .join('\n'),
    })
  }
  if (event.actor?.kind === 'tool' && data.diagram_type) {
    details.push({ label: '图类型', value: String(data.diagram_type) })
  }
  if ((event.type === 'tool.failed' || event.type === 'agent.failed') && data.error_code) {
    details.push({ label: '错误码', value: String(data.error_code) })
  }
  if (event.type === 'agent.failed' && data.error_reason) {
    details.push({ label: '失败原因', value: String(data.error_reason) })
  }
  if (event.type === 'harness.decision') {
    const triggerStatus = agentEventStatusLabels[String(data.trigger_status || '')] || '状态未知'
    details.push({
      label: '触发反馈',
      value: `${agentEventCapabilityLabel(data.trigger)} · ${triggerStatus}`,
    })
    details.push({
      label: '决策原因',
      value: harnessDecisionReasonLabels[String(data.reason_code || '')] || '已根据本次反馈完成判断',
    })
    if (Number(data.issue_count) > 0) {
      const issueCodes = Array.isArray(data.issue_codes)
        ? data.issue_codes
          .map(item => qualityIssueLabels[String(item)] || '其他质量问题')
          .filter(Boolean)
        : []
      details.push({
        label: `质检问题（第 ${Number(data.quality_cycle) || 0} 轮）`,
        value: issueCodes.length ? issueCodes.join('、') : `${Number(data.issue_count)} 个`,
      })
    }
    const scheduled = Array.isArray(data.scheduled)
      ? data.scheduled.map(agentEventCapabilityLabel)
      : []
    const activatedSkills = Array.isArray(data.activated_skills)
      ? data.activated_skills.map(item => String(item)).filter(Boolean)
      : []
    const additions = [
      ...(activatedSkills.length ? [`${activatedSkills.length} 项已应用技能`] : []),
      ...scheduled,
    ]
    details.push({ label: '追加能力', value: additions.length ? additions.join(' → ') : '无，继续现有计划' })
  }
  if (event.type === 'agent.completed') {
    const resultEntry = Object.entries(event.environment_patch || {})
      .find(([key, value]) => (
        ['data_analysis', 'industry_research', 'solution_design', 'chapter_design'].includes(key)
        && typeof value === 'string'
        && value.trim()
      ))
    if (resultEntry) {
      details.push({
        label: '结果摘要',
        value: String(resultEntry[1]),
      })
    }
    const plan = event.environment_patch?.plan
    if (Array.isArray(plan) && plan.length) {
      details.push({
        label: '后续步骤',
        value: plan.map(item => String(item)).join(' → '),
      })
    }
    const quality = event.environment_patch?.quality_review as Record<string, unknown> | undefined
    if (quality) {
      const passed = Object.values(quality).every(Boolean)
      details.push({ label: '检查结果', value: passed ? '全部通过' : '存在待修订项' })
    }
  }
  if (event.type === 'skill.completed') {
    const skill = event.environment_patch?.skill as Record<string, unknown> | undefined
    if (skill?.source) {
      details.push({
        label: '来源',
        value: skill.source === 'installed' ? '已安装技能' : '内置技能市场',
      })
    }
  }
  if (event.type === 'document.patch.applied') {
    details.push({ label: '结果', value: String(patch?.summary || event.summary) })
    const changeCount = Number(patch?.change_count)
    if (Number.isFinite(changeCount) && changeCount > 0) {
      details.push({ label: '改动', value: `${changeCount} 处` })
    }
  }
  return details
}

const changeOverlappingLines = (
  changes: DocumentChange[],
  startLine: number,
  endLine: number,
) => changes.find(change => (
  change.startLine != null
  && change.endLine != null
  && change.startLine <= endLine
  && change.endLine >= startLine
))

const markdownComponentsWithChanges = (
  components: any,
  blockStartLine: number,
  blockStartOffset: number,
  changes: DocumentChange[],
) => {
  const decorated = { ...components }
  // Keep fenced-code renderers stable. Recreating the `pre` component on every
  // document tick remounts Mermaid diagrams and makes them visibly flash.
  ;['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'blockquote'].forEach((tag) => {
    const Original = components[tag]
    decorated[tag] = ({ node, className, ...props }: any) => {
      const localStart = Number(node?.position?.start?.line)
      const localEnd = Number(node?.position?.end?.line)
      const localStartOffset = Number(node?.position?.start?.offset)
      const localEndOffset = Number(node?.position?.end?.offset)
      const globalStart = blockStartLine + (Number.isFinite(localStart) ? localStart - 1 : 0)
      const globalEnd = blockStartLine + (Number.isFinite(localEnd) ? localEnd - 1 : 0)
      const change = changeOverlappingLines(changes, globalStart, globalEnd)
      const resolvedClassName = [
        className,
        change ? 'creation-latest-change' : '',
        change ? `is-${change.changeType}` : '',
      ].filter(Boolean).join(' ')
      const resolvedProps = {
        ...props,
        className: resolvedClassName || undefined,
        ...(Number.isFinite(localStartOffset) && Number.isFinite(localEndOffset)
          ? {
            'data-md-start': blockStartOffset + localStartOffset,
            'data-md-end': blockStartOffset + localEndOffset,
            'data-md-kind': tag,
          }
          : {}),
        ...(change
          ? {
            'data-change-type': change.changeType,
            'data-change-section': change.sectionTitle,
          }
          : {}),
      }
      if (Original) {
        return <Original node={node} {...resolvedProps} />
      }
      return React.createElement(tag, resolvedProps)
    }
  })
  return decorated
}

const MarkdownContent = React.memo(({
  content,
  components,
  changes = [],
}: {
  content: string
  components: any
  changes?: DocumentChange[]
}) => {
  const inlineComponents = {
    ...components,
    p: ({ children }: any) => <>{children}</>,
  }

  return (
    <>
      {parseMarkdownBlocks(stripInternalCreationMarkers(content)).map((block, index) => {
        if (block.type === 'markdown') {
          return (
            <ReactMarkdown
              key={`markdown-${index}`}
              components={markdownComponentsWithChanges(
                components,
                block.startLine,
                block.startOffset,
                changes,
              )}
            >
              {block.content}
            </ReactMarkdown>
          )
        }

        const tableChange = changeOverlappingLines(changes, block.startLine, block.endLine)
        return (
          <div
            key={`table-${index}`}
            className={[
              tableChange ? 'creation-latest-change' : '',
              tableChange ? `is-${tableChange.changeType}` : '',
            ].filter(Boolean).join(' ')}
            data-change-type={tableChange?.changeType}
            data-change-section={tableChange?.sectionTitle}
            style={{ overflowX: 'auto', margin: '16px 0' }}
          >
            <table style={{ width: '100%', minWidth: 720, borderCollapse: 'collapse', fontSize: 14, lineHeight: 1.55 }}>
              <thead>
                <tr>
                  {block.headers.map((header, cellIndex) => (
                    <th
                      key={cellIndex}
                      style={{
                        border: '1px solid var(--mb-brand-border)',
                        background: 'var(--mb-brand-soft)',
                        color: 'var(--mb-brand-text)',
                        fontWeight: 700,
                        padding: '10px 12px',
                        textAlign: block.alignments[cellIndex] || 'left',
                        verticalAlign: 'top',
                      }}
                    >
                      <ReactMarkdown components={inlineComponents}>{header}</ReactMarkdown>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {block.headers.map((_, cellIndex) => (
                      <td
                        key={cellIndex}
                        style={{
                          border: '1px solid var(--mb-border-strong)',
                          padding: '10px 12px',
                          textAlign: block.alignments[cellIndex] || 'left',
                          verticalAlign: 'top',
                          background: rowIndex % 2 === 0 ? 'var(--mb-bg-card)' : 'var(--mb-bg-warm)',
                        }}
                      >
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
})

const Toggle = ({ label, checked, onChange, icon }: { label: string; checked: boolean; onChange: (value: boolean) => void; icon?: React.ReactNode }) => (
  <label style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, fontSize: 14, color: 'var(--mb-text-primary)' }}>
    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>{icon}{label}</span>
    <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
  </label>
)

const WeightSlider = ({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) => (
  <label style={{ display: 'grid', gap: 6, marginBottom: 11, fontSize: 12, color: 'var(--mb-text-secondary)' }}>
    <span style={{ display: 'flex', justifyContent: 'space-between' }}>
      <span>{label}</span>
      <span>{value}%</span>
    </span>
    <input type="range" min={0} max={70} value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </label>
)

const ProgressStrip = ({ label, percent }: { label: string; percent: number }) => (
  <div style={{ marginTop: 12, display: 'grid', gap: 6, maxWidth: 360 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--mb-text-secondary)' }}>
      <span>{label}</span>
      <span>{percent}%</span>
    </div>
    <div style={{ height: 6, borderRadius: 999, background: 'var(--mb-border-strong)', overflow: 'hidden' }}>
      <div style={{ width: `${percent}%`, height: '100%', borderRadius: 999, background: '#a45d22', transition: 'width 0.25s ease' }} />
    </div>
  </div>
)

const dataSourceKindLabels: Record<string, string> = {
  report_url: '实时报表',
  timeline_snapshot: '工作记录',
  memory_snapshot: '本地数据',
}

const dataFreshnessLabels: Record<string, string> = {
  recent: '近期数据',
  stale: '建议刷新',
  historical: '历史数据',
  missing: '待采集',
  unverified: '证据未通过',
  superseded: '已由即时数据替代',
}

const dataEvidenceReasonLabels: Record<string, string> = {
  ocr_failed: '截图文字识别失败',
  no_verified_metric: '截图中未识别到可核验指标',
  dom_ocr_mismatch: '页面数据与截图文字不一致',
  screenshot_missing: '未生成证据截图',
  screenshot_unreadable: '证据截图无法读取',
  evidence_missing: '缺少可核验的页面证据',
}

const DataReferenceRow = ({
  item,
  source,
  loading,
}: {
  item: CreationDataReferenceItem
  source?: DataSource
  loading: boolean
}) => {
  const snapshot = source?.latest_snapshot
  const presentation = snapshot ? presentDataSnapshot(snapshot) : null
  const evidenceUnavailable = ['rejected', 'failed'].includes(item.evidence_status || '')
  const superseded = item.unavailable_reason === 'superseded_by_live_report'
  const availabilityLabel = item.can_use
    ? ''
    : superseded
      ? '已被即时数据替代'
      : evidenceUnavailable
        ? '证据未通过'
        : '暂不可用'
  const freshnessLabel = superseded
    ? '已由本轮即时报表替代'
    : evidenceUnavailable
      ? item.evidence_status === 'failed' ? '即时采集失败' : '已采集，证据未通过'
      : item.refresh_required
        ? '需要刷新'
        : item.freshness_class === 'fresh'
          ? ''
          : dataFreshnessLabels[item.freshness_class] || item.freshness_class || '时效未知'

  return (
    <article className="creation-data-reference-row">
      <div className="creation-data-reference-row__heading">
        <div>
          <span>数据来源 #{item.source_id}</span>
          <strong>{source?.title || item.title}</strong>
        </div>
        {availabilityLabel && <span className="is-unavailable">{availabilityLabel}</span>}
      </div>
      <div className="creation-data-reference-row__meta">
        <span>{dataSourceKindLabels[item.source_kind] || item.source_kind || '数据来源'}</span>
        {freshnessLabel && <span>{freshnessLabel}</span>}
        {snapshot && <span>数据时间 {formatDataTimestamp(snapshot.observed_at ?? snapshot.collected_at)}</span>}
      </div>
      {evidenceUnavailable && (
        <div className="creation-data-reference-row__state">
          {dataEvidenceReasonLabels[item.evidence_reason || ''] || '本次页面证据没有通过校验'}，该快照不会用于本轮创作。
        </div>
      )}
      {loading ? (
        <div className="creation-data-reference-row__state">正在读取具体数据…</div>
      ) : presentation && snapshot ? (
        <div className="creation-data-reference-row__detail">
          <h4>{presentation.title}</h4>
          <p>{presentation.summary}</p>
          {presentation.rows.length ? (
            <div className="creation-data-reference-table-wrap">
              <table className="creation-data-reference-table">
                <thead>
                  <tr>
                    <th scope="col">对象 / 范围</th>
                    <th scope="col">指标</th>
                    <th scope="col">数值</th>
                    <th scope="col">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {presentation.rows.map((row, index) => (
                    <tr key={`${row.dimension}-${row.metric}-${row.value}-${index}`}>
                      <td>{row.dimension || '整体'}</td>
                      <th scope="row">{row.metric}</th>
                      <td className="creation-data-reference-table__value">{row.value}</td>
                      <td>{row.note || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="creation-data-reference-row__state">该快照暂无可展示的结构化指标。</div>
          )}
          <details className="creation-data-reference-disclosure">
            <summary>查看完整采集内容</summary>
            <div>{snapshot.content_text || '暂无完整采集内容'}</div>
          </details>
        </div>
      ) : source ? (
        <div className="creation-data-reference-row__state">该来源尚未采集到数据快照。</div>
      ) : (
        <div className="creation-data-reference-row__state">暂未读取到该来源的具体数据。</div>
      )}
    </article>
  )
}

const referenceRefreshLabel = (item: ReferenceItem) => {
  if (item.refresh_status === 'fresh_complete') return '刚刚已校验'
  if (item.refresh_status === 'fresh_recent') return '近期已校验'
  if (item.refresh_status === 'fresh_partial') return '部分采集'
  if (item.refresh_status === 'fresh_recent_partial') return '近期部分采集'
  if (item.refresh_status === 'unavailable') return '当前不可用'
  return item.source_url ? '历史版本·本轮未验证' : ''
}

const ReferenceRow = ({ item, onOpenSource }: { item: ReferenceItem; onOpenSource: (item: ReferenceItem) => void }) => (
  <div style={{ border: '1px solid var(--mb-border-strong)', borderRadius: 8, padding: 12 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
      <div style={{ fontSize: 14, fontWeight: 650, lineHeight: 1.35 }}>{item.title}</div>
    </div>
    <div style={{ marginTop: 6, fontSize: 12, color: 'var(--mb-text-secondary)' }}>{item.doc_type || '未分类'} · 打开/引用 {item.usage_count}</div>
    {referenceRefreshLabel(item) && (
      <div style={{ marginTop: 6, fontSize: 11, color: ['fresh_complete', 'fresh_recent'].includes(item.refresh_status || '') ? '#287a45' : '#9a5b16', fontWeight: 650 }}>
        {referenceRefreshLabel(item)}
      </div>
    )}
    <div style={{ marginTop: 8, fontSize: 12, color: 'var(--mb-text-secondary)', lineHeight: 1.55 }}>{item.reason}</div>
    <div style={{ marginTop: 10, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
      <span>相关 {Math.round(item.relevance_score * 100)}</span>
      <span>完整 {Math.round(item.completeness_score * 100)}</span>
      <span>格式 {Math.round(item.format_score * 100)}</span>
    </div>
    <button
      type="button"
      onClick={() => onOpenSource(item)}
      style={{
        marginTop: 10,
        padding: '6px 10px',
        border: '1px solid var(--mb-border-strong)',
        borderRadius: 6,
        background: 'var(--mb-bg-card)',
        color: '#a45d22',
        fontSize: 12,
        fontWeight: 600,
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
      }}
    >
      <ExternalLink size={13} />
      资料来源
    </button>
  </div>
)

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '10px 12px',
  border: '1px solid var(--mb-border-strong)',
  borderRadius: 8,
  fontSize: 13,
  fontFamily: 'inherit',
  outline: 'none',
  background: 'var(--mb-bg-card)',
}

const primaryButtonStyle: React.CSSProperties = {
  height: 34,
  padding: '0 12px',
  border: '1px solid #a45d22',
  borderRadius: 8,
  background: '#a45d22',
  color: '#fff',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  cursor: 'pointer',
  fontSize: 12,
  fontWeight: 650,
}

const secondaryButtonStyle: React.CSSProperties = {
  ...primaryButtonStyle,
  background: 'var(--mb-bg-card)',
  color: 'var(--mb-text-primary)',
  border: '1px solid var(--mb-border-strong)',
}

const dangerButtonStyle: React.CSSProperties = {
  ...primaryButtonStyle,
  background: '#b42318',
  border: '1px solid #b42318',
}

const compactButtonStyle: React.CSSProperties = {
  ...secondaryButtonStyle,
  height: 32,
  padding: '0 10px',
  fontSize: 13,
}

const compactDangerButtonStyle: React.CSSProperties = {
  ...dangerButtonStyle,
  height: 32,
  padding: '0 10px',
  fontSize: 13,
}

const attachmentPillStyle: React.CSSProperties = {
  minHeight: 30,
  padding: '0 8px',
  border: '1px solid var(--mb-border-strong)',
  borderRadius: 999,
  background: 'var(--mb-bg-card)',
  color: 'var(--mb-text-primary)',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  fontSize: 12,
}

const attachmentRemoveStyle: React.CSSProperties = {
  width: 20,
  height: 20,
  border: 0,
  borderRadius: 999,
  background: 'var(--mb-bg-inset)',
  color: 'var(--mb-text-secondary)',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  cursor: 'pointer',
}

export default CreationPanel
