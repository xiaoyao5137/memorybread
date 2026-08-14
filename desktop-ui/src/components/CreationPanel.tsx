import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { AtSign, Bot, Check, ChevronDown, ChevronRight, CloudOff, CloudUpload, Copy, ExternalLink, Eye, FileCode2, FileText, Image, Library, Loader2, MessageSquarePlus, PackageCheck, PackagePlus, Paperclip, Pencil, Plus, Search, Send, Sparkles, Square, Store, Trash2, Upload, Wrench, X } from 'lucide-react'
import { serviceEnvironmentHeaders, useAppStore } from '../store/useAppStore'
import type { CreationAgentEvent, CreationChatMessage, CreationDataReferenceItem, CreationReferenceItem, CreationReferencePreview } from '../store/useAppStore'
import { fetchWithLocalhostFallback } from '../hooks/useApi'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { MentionHighlightTextarea } from './MentionHighlightField'
import { getUserDisplayName } from '../utils/accountDisplay'
import { fetchBillingBalance } from '../utils/authApi'
import { createOptionalCloudRequestSignal, optionalCloudIsReachable } from '../utils/optionalCloud'
import { CREATION_MODEL_DEFS, LOCAL_CREATION_MODEL_ID, REMOTE_CREATION_MODEL_ID, canUseRemoteCreationModel, getEffectiveCreationModelId, getModelDisplayName } from '../utils/modelSelection'
import { buildAttachmentMetadata, buildAttachmentPrompt, filesToAttachments, formatAttachmentSize, type UserAttachment } from '../utils/attachments'
import { toLocalApiError, toUserFacingError } from '../utils/userFacingError'
import {
  buildCreationSkillInstruction,
  categoryPathFor,
  CREATION_SKILL_AGENT_OPTIONS,
  CREATION_SKILL_TOOL_OPTIONS,
  creationSkillCategoryOptions,
  deleteLocalCreationSkill,
  fetchCreationSkillCategories,
  importCodexSkillPackage,
  listLocalCreationSkills,
  marketCreationSkillToLocalInput,
  matchCreationSkills,
  publishCreationSkill,
  resolveCreationSkillDependencies,
  saveLocalCreationSkill,
  searchCreationSkillMarket,
  type CreationSkillMarketItem,
  type CreationSkillSource,
  type LocalCreationSkill,
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
import { HistoryPagination, HistorySearch } from './HistoryBrowserControls'
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
}

type ReferenceItem = CreationReferenceItem
type ReferencePreview = CreationReferencePreview
type BottomTab = 'reference' | 'data' | 'config'
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
interface BrowserPreviewItem {
  id: string
  source_id: number
  title: string
  image_url: string
  status?: string
  browser?: string | null
  interaction_mode?: string | null
}
type MarkdownBlock =
  | { type: 'markdown'; content: string; startLine: number; endLine: number }
  | { type: 'table'; headers: string[]; alignments: Array<'left' | 'center' | 'right'>; rows: string[][]; startLine: number; endLine: number }
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

const sanitizeGeneratedContent = (content: string) =>
  content.replace(/<a\s+(?:id|name)=["'][^"']+["']\s*>\s*<\/a>/gi, '')

const retainConversationContext = (messages: CreationChatMessage[]) => {
  if (messages.length <= MAX_CONVERSATION_MESSAGES) return messages
  return [...messages.slice(0, 4), ...messages.slice(-(MAX_CONVERSATION_MESSAGES - 4))]
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

const collapseAgentLifecycleEvents = (events: CreationAgentEvent[]) => {
  const startTypeForTerminal: Record<string, string> = {
    'agent.completed': 'agent.started',
    'tool.completed': 'tool.started',
    'tool.failed': 'tool.started',
    'skill.completed': 'skill.started',
    'browser.preview.completed': 'browser.preview.started',
  }
  const visible: CreationAgentEvent[] = []

  events.forEach((event) => {
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

const mapCreationHistory = (histories: any[]): CreationHistoryItem[] => histories.map((h: any) => {
  const fullContent = sanitizeGeneratedContent(h.generated_content)
  const rootRequest = String(h.root_request || '')
  let references: CreationReferenceItem[] = []
  try {
    const parsed = typeof h.references_json === 'string' ? JSON.parse(h.references_json || '[]') : h.references_json
    references = Array.isArray(parsed) ? parsed : []
  } catch {
    references = []
  }
  const agentEvents = parseHistoryJson<CreationAgentEvent[]>(h.agent_trace_json, [])
  return {

    id: Number(h.id),
    prompt: rootRequest || h.prompt,
    timestamp: new Date(h.updated_at ?? h.created_at).toLocaleString('zh-CN'),
    preview: fullContent.slice(0, 100) + (fullContent.length > 100 ? '...' : ''),
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
      blocks.push({
        type: 'markdown',
        content: markdown,
        startLine: markdownBufferStart + first + 1,
        endLine: markdownBufferStart + last,
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

const CreationPanel: React.FC<CreationPanelProps> = ({ className = '' }) => {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)
  const adminApiBaseUrl = useAppStore((s) => s.adminApiBaseUrl)
  const gatewayApiBaseUrl = useAppStore((s) => s.gatewayApiBaseUrl)
  const authToken = useAppStore((s) => s.authToken)
  const currentUser = useAppStore((s) => s.currentUser)
  const cloudBalance = useAppStore((s) => s.cloudBalance)
  const setCloudBalance = useAppStore((s) => s.setCloudBalance)
  const draft = useAppStore((s) => s.creationDraft)
  const setCreationDraft = useAppStore((s) => s.setCreationDraft)
  const setWindowMode = useAppStore((s) => s.setWindowMode)
  const setBakeTab = useAppStore((s) => s.setBakeTab)
  const setSelectedTemplateId = useAppStore((s) => s.setSelectedTemplateId)
  const setBakeTemplateFocusId = useAppStore((s) => s.setBakeTemplateFocusId)
  const setBakeTemplateOffset = useAppStore((s) => s.setBakeTemplateOffset)
  const setBakeTemplateQuery = useAppStore((s) => s.setBakeTemplateQuery)
  const setBakeTemplateLimit = useAppStore((s) => s.setBakeTemplateLimit)
  const pushBakeNavigationTarget = useAppStore((s) => s.pushBakeNavigationTarget)
  const creationModelConfigs = useAppStore((s) => s.creationModelConfigs)
  const setCreationModelConfig = useAppStore((s) => s.setCreationModelConfig)
  const userDisplayName = currentUser ? getUserDisplayName(currentUser) : '用户'

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

  const [dataSourcesById, setDataSourcesById] = useState<Record<number, DataSource>>({})
  const [dataReferencesLoading, setDataReferencesLoading] = useState(false)
  const [dataReferencesError, setDataReferencesError] = useState('')
  const [legacyDataReferencesRecovered, setLegacyDataReferencesRecovered] = useState(false)
  const [isGenerating, setIsGenerating] = useState(false)
  const [isPreviewing, setIsPreviewing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copySuccess, setCopySuccess] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [topTab, setTopTab] = useState<'creation' | 'history' | 'skills' | 'tools'>('creation')
  const [activeBottomTab, setActiveBottomTab] = useState<BottomTab | null>(null)
  const toggleBottomTab = (tab: BottomTab) =>

    setActiveBottomTab(prev => prev === tab ? null : tab)
  const [creationTools, setCreationTools] = useState(loadCreationTools)
  const [creationHistory, setCreationHistory] = useState<CreationHistoryItem[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(1)
  const [historySearch, setHistorySearch] = useState('')
  const [debouncedHistorySearch, setDebouncedHistorySearch] = useState('')
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [lastInferenceMeta, setLastInferenceMeta] = useState<{ model: string; latencyMs: number | null } | null>(null)
  const [attachments, setAttachments] = useState<UserAttachment[]>([])
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [currentDocumentSource, setCurrentDocumentSource] = useState<CreationSkillSource | null>(null)
  const [localSkills, setLocalSkills] = useState<LocalCreationSkill[]>([])
  const [skillsLoading, setSkillsLoading] = useState(false)
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
  const [currentDocumentSkills, setCurrentDocumentSkills] = useState<LocalCreationSkill[]>([])
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
  const bottomPanelRef = useRef<HTMLDivElement>(null)
  const chatTimelineRef = useRef<HTMLDivElement>(null)
  const workspaceRef = useRef<HTMLElement>(null)
  const workspaceResizeCleanupRef = useRef<(() => void) | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const legacyDataRecoveryRef = useRef(0)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const skillPackageInputRef = useRef<HTMLInputElement>(null)
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

  const openBottomTab = (tab: Extract<BottomTab, 'reference' | 'data'>) => {
    setActiveBottomTab(tab)
    window.requestAnimationFrame(() => {
      bottomPanelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' })
      bottomPanelRef.current
        ?.querySelector<HTMLButtonElement>(`[data-bottom-tab="${tab}"]`)
        ?.focus()
    })
  }

  const handleOpenReferenceSource = (item: Pick<ReferenceItem, 'id'>) => {
    const templateId = String(item.id)
    pushBakeNavigationTarget({ windowMode: 'creation' })
    setBakeTemplateQuery('')
    setBakeTemplateOffset(0)
    setBakeTemplateLimit(100)
    setBakeTemplateFocusId(templateId)
    setBakeTab('templates')
    setSelectedTemplateId(templateId)
    setWindowMode('bake')
  }

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
    legacyDataRecoveryRef.current += 1
    setPrompt('')
    setGeneratedContent(item.fullContent)
    setSessionId(item.sessionId || `history-${item.id}`)
    const restoredConversation: CreationChatMessage[] = item.conversation.length
      ? item.conversation
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
    const restoredEvents = [...item.agentEvents]
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
    if (contentRef.current) {
      setTimeout(() => contentRef.current?.scrollTo({ top: 0, behavior: 'smooth' }), 100)
    }
  }

  const loadLocalSkills = useCallback(async () => {
    setSkillsLoading(true)
    setSkillsError('')
    try {
      setLocalSkills(await listLocalCreationSkills(apiBaseUrl))
    } catch (err) {
      setLocalSkills([])
      setSkillsError(toLocalApiError(err, '技能加载失败'))
    } finally {
      setSkillsLoading(false)
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
      const input = await importCodexSkillPackage(files)
      const saved = await saveLocalCreationSkill(apiBaseUrl, input)
      handleSkillSaved(saved)
      setSkillLibraryView('mine')
      showLocalSkillDetail(saved)
    } catch (err) {
      setSkillsError(toLocalApiError(err, '上传技能包失败'))
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
      const marketInput = marketCreationSkillToLocalInput(marketSkill)
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

  const showMarketSkillDetail = (skill: CreationSkillMarketItem, focusFiles = false) => {
    const installed = localSkills.some(item =>
      item.cloudSkillId === skill.id && item.installed,
    )
    setSkillDetailMarketItem(skill)
    setSkillDetail(marketSkillDetail(skill, installed))
    setSkillDetailFocusFiles(focusFiles)
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
  const matchedSkills = useMemo(
    () => matchCreationSkills(prompt, installedSkills),
    [installedSkills, prompt],
  )
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
  const promptWithAttachments = () => {
    const basePrompt = messageWithAttachments()
    return `${basePrompt}${buildCreationSkillInstruction(matchedSkills)}`
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

  const buildGatewayMessages = (references: CreationReferenceItem[]) => {
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
      { role: 'user', content: `${options}\n\n创作需求：\n${promptWithAttachments()}${referenceText}` },
    ]
  }

  const postGatewayCreation = async (references: CreationReferenceItem[], signal?: AbortSignal) => {
    const response = await fetch(`${gatewayApiBaseUrl.replace(/\/+$/, '')}/v1/gateway/chat`, {
      method: 'POST',
      headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify({
        request_id: `creation-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        user_id: currentUser?.id || null,
        brand_model_id: 'mbcd-plus-v1',
        caller: 'creation',
        messages: buildGatewayMessages(references),
        stream: false,
        privacy: { content_logging: false, client_scrubbed: true },
        limits: { max_output_tokens: 8192, max_credit: '100.0000' },
      }),
    })
    if (!response.ok) {
      throw new Error(await readApiErrorMessage(response, `生成失败: ${response.status}`))
    }
    return response.json()
  }

  const postGatewayAgentCall = async (
    messages: Array<{ role: string; content: string }>,
    signal?: AbortSignal,
  ) => {
    const response = await fetch(`${gatewayApiBaseUrl.replace(/\/+$/, '')}/v1/gateway/chat`, {
      method: 'POST',
      headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify({
        request_id: `creation-agent-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        user_id: currentUser?.id || null,
        brand_model_id: 'mbcd-plus-v1',
        caller: 'creation',
        messages,
        stream: false,
        privacy: { content_logging: false, client_scrubbed: true },
        limits: { max_output_tokens: 8192, max_credit: '100.0000' },
      }),
    })
    if (!response.ok) {
      throw new Error(await readApiErrorMessage(response, `Agent 推理失败: ${response.status}`))
    }
    const data = await response.json()
    const content = sanitizeGeneratedContent(String(data.content || ''))
    if (!content.trim()) throw new Error('品牌模型没有返回 Agent 结果')
    return content
  }

  const postLocalCreation = async (signal?: AbortSignal) => {
    const response = await fetch(`${apiBaseUrl}/api/creation/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify(buildPayload()),
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

  const selectedSkillPayload = () => resolveCreationSkillDependencies(
    matchedSkills.map(({ skill }) => skill),
    localSkills,
  ).map(skill => ({
    id: skill.clientSkillKey || String(skill.id),
    title: skill.title,
    summary: skill.summary,
    skillDescription: skill.skillDescription,
    executionSteps: skill.executionSteps,
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
    exampleDocument: skill.exampleDocument,
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
      conversation: chat.map(item => ({ role: item.role, content: item.content })),
      selected_skills: selectedSkillPayload(),
      model_mode: useGatewayCreation && currentUser?.id ? 'external' : 'local',
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
      }
    }
    if (event.type === 'document.replaced') {
      return {
        ...base,
        data: { operation: event.data?.operation || 'rewrite_document' },
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
          }))
        : []
      return { ...base, data: { previews } }
    }
    if (event.type === 'tool.completed' || event.type === 'tool.failed') {
      const dataSources = isDataReferenceEvent(event)
        ? normalizeDataReferences(event.environment_patch?.data_sources)
        : []
      return {
        ...base,
        environment_patch: dataSources.length > 0 ? { data_sources: dataSources } : {},
        data: {
          result_count: event.data?.result_count,
          refresh_required_count: event.data?.refresh_required_count,
          diagram_type: event.data?.diagram_type,
          error_code: event.data?.error_code,
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
    if (!['document.delta', 'document.patch.delta'].includes(event.type)) {
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
        setReferencePreview({
          requirement: {
            topic: messageWithAttachments(activeUserMessageRef.current),
            doc_type: docType,
            audience,
            style: '',
            keywords: [],
          },
          references: items.map((item: any) => ({
            id: Number(item.id),
            title: String(item.title || '本地参考资料'),
            doc_type: String(item.doc_type || ''),
            final_weight: Number(item.final_weight || 0),
            relevance_score: Number(item.relevance_score || 0),
            quality_score: Number(item.quality_score || 0),
            completeness_score: Number(item.completeness_score || 0),
            usage_score: Number(item.usage_score || 0),
            format_score: Number(item.format_score || 0),
            freshness_score: Number(item.freshness_score || 0),
            usage_count: Number(item.usage_count || 0),
            reason: String(item.reason || ''),
            summary: String(item.summary || ''),
            source_url: item.source_url ? String(item.source_url) : undefined,
          })),
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
    if (event.type === 'run.failed') throw new Error(event.summary || '创作 Agent 执行失败')
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

  const postReferencePreview = async (signal?: AbortSignal) => {
    const payload = buildPayload()
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

  const addFiles = async (files: Iterable<File>) => {
    setAttachmentError(null)
    try {
      const next = await filesToAttachments(files, attachments.length)
      setAttachments(prev => [...prev, ...next])
    } catch (err) {
      setAttachmentError(toUserFacingError(err, '附件读取失败'))
    }
  }

  const handlePreviewReferences = async () => {
    if (!prompt.trim()) return
    setIsPreviewing(true)
    setError(null)
    try {
      const response = await postReferencePreview()
      if (!response.ok) {
        throw new Error(await readApiErrorMessage(response, `参考资料预览失败: ${response.status}`))
      }
      const data = await response.json()
      setReferencePreview(data)
    } catch (err) {
      setError(toUserFacingError(err, '参考资料预览失败'))
    } finally {
      setIsPreviewing(false)
    }
  }

  const persistCreationResult = async (
    userMessage: string,
    content: string,
    chat: CreationChatMessage[],
    usedModelId: string,
    latencyMs: number | null,
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
        }),
      })
      if (saveResponse.ok) {
        const saved = await saveResponse.json()
        const savedTitle = state.rootRequest || userMessage
        setCurrentDocumentSource({
          kind: 'creation_history',
          id: String(saved.id),
          title: savedTitle,
          content: sanitizeGeneratedContent(content),
          docType,
        })
      }
      if (historyPage === 1) void loadCreationHistory()
      else setHistoryPage(1)
    } catch (saveErr) {
      console.error('保存创作记录失败:', saveErr)
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
        const refResponse = await postReferencePreview(controller.signal)
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
      const data = await postGatewayCreation(referencesForHistory, controller.signal)
      content = sanitizeGeneratedContent(String(data.content || ''))
    } else {
      usedModelId = LOCAL_CREATION_MODEL_ID
      content = await postLocalCreation(controller.signal)
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
    const message = userMessage.trim()
    if (!message) return
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

    try {
      let payload = buildAgentPayload(message, chat, {
        session_id: activeSessionId,
        confirmed,
      })
      let finalRunId: string | null = null
      while (true) {
        const phase = await postAgentPhase(payload, controller.signal)
        if (phase.sessionId) setSessionId(phase.sessionId)
        finalRunId = phase.runId || finalRunId
        if (phase.pausedForConfirmation) return
        if (phase.modelMessages) {
          if (!phase.continuation) throw new Error('创作 Agent 缺少恢复状态')
          const modelResult = await postGatewayAgentCall(phase.modelMessages, controller.signal)
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
        setError('已中止本次创作')
        return
      }
      setError(toUserFacingError(err, '生成失败，请稍后重试'))
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setIsGenerating(false)
      stopTimer()
    }
  }

  const handleGenerate = async () => {
    await runAgentTurn({ userMessage: prompt })
  }

  const handleConfirmContinue = async () => {
    if (!pendingConfirmation) return
    const message = pendingConfirmation.userMessage
    setPendingConfirmation(null)
    await runAgentTurn({ userMessage: message, confirmed: true, appendUser: false })
  }

  const handleStopGenerate = () => {
    abortRef.current?.abort()
    setIsGenerating(false)
    stopTimer()
    setError('已中止本次创作')
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
    || pendingConfirmation,
  )

  const handleNewConversation = () => {
    if (isGenerating || !hasActiveSession) return

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
    })
    setAttachments([])
    setAttachmentError(null)
    setCurrentDocumentSource(null)
    setCurrentDocumentSkills([])
    setPendingConfirmation(null)
    setSkillPickerOpen(false)
    setSkillQuery('')
    setError(null)
    setCopySuccess(false)
    setLastInferenceMeta(null)
    setElapsedSeconds(0)
    setActiveBottomTab(null)
    activeUserMessageRef.current = ''
    if (contentRef.current) contentRef.current.scrollTop = 0
    window.requestAnimationFrame(() => promptInputRef.current?.focus())
  }

  const handleCopy = async () => {
    await navigator.clipboard.writeText(generatedContent)
    setCopySuccess(true)
    setTimeout(() => setCopySuccess(false), 2000)
  }

  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = contentRef.current.scrollHeight
  }, [generatedContent])

  useEffect(() => {
    if (chatTimelineRef.current) {
      chatTimelineRef.current.scrollTop = chatTimelineRef.current.scrollHeight
    }
  }, [agentEvents, conversation, pendingConfirmation])

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
  const canDisplayLatestMutation = latestRunCompleted
    || (!isGenerating && !latestRunHasLifecycle)
  // 首版创作 run 内的润色 patch 也展示局部改动；仅排除首版的整篇 document.replaced。
  const latestDocumentMutation = canDisplayLatestMutation
    ? [...agentEvents]
      .reverse()
      .find(item => (
        ['document.patch.applied', 'document.replaced'].includes(item.type)
        && (!latestAgentRunId || item.run_id === latestAgentRunId)
        && !(latestRunIsInitialCreation && item.type === 'document.replaced')
      ))
    : undefined
  const latestDocumentPatch = latestDocumentMutation?.type === 'document.patch.applied'
    ? latestDocumentMutation.data?.patch as Record<string, unknown> | undefined
    : undefined
  const latestPatchTargets = Array.isArray(latestDocumentPatch?.target_sections)
    ? latestDocumentPatch.target_sections.map(item => String(item)).filter(Boolean)
    : []
  const latestPatchChanges = documentChangesFromPatch(latestDocumentPatch, generatedContent)
  const latestPatchChangeCount = Math.max(
    latestPatchChanges.length,
    Number(latestDocumentPatch?.change_count) || 0,
  )
  const latestUserInstruction = [...conversation]
    .reverse()
    .find(message => message.role === 'user')
    ?.content
  const creationTimeline = buildCreationTimeline(conversation, agentEvents)

  const handleReferenceClick = (refId: string) => {
    const normalizedId = Number(refId)
    if (!Number.isFinite(normalizedId) || normalizedId <= 0) return
    handleOpenReferenceSource({ id: normalizedId })
  }

  const headingId = (node: any) =>
    node.children.map((c: any) => c.value || '').join('').toLowerCase().replace(/\s+/g, '-')

  const markdownComponents = {
    h1: ({ node, children, ...props }: any) => <h1 id={headingId(node)} style={{ fontSize: 26, lineHeight: 1.25, margin: '0 0 18px' }} {...props}>{children}</h1>,
    h2: ({ node, children, ...props }: any) => <h2 id={headingId(node)} style={{ fontSize: 20, lineHeight: 1.35, margin: '24px 0 12px' }} {...props}>{children}</h2>,
    h3: ({ node, children, ...props }: any) => <h3 id={headingId(node)} style={{ fontSize: 16, lineHeight: 1.45, margin: '18px 0 9px' }} {...props}>{children}</h3>,
    p: ({ node, ...props }: any) => <p style={{ margin: '9px 0', lineHeight: 1.75 }} {...props} />,
    li: ({ node, ...props }: any) => <li style={{ margin: '6px 0', lineHeight: 1.65 }} {...props} />,
    code: ({ node, ...props }: any) => <code style={{ background: '#f2f4f7', padding: '2px 5px', borderRadius: 4 }} {...props} />,
    strong: ({ node, ...props }: any) => (
      <strong
        style={{
          color: '#9a4f1c',
          fontWeight: 750,
          textDecoration: 'underline',
          textDecorationColor: '#e4b48e',
          textUnderlineOffset: 3,
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
  }

  return (
    <div className={`creation-panel ${className}`.trim()} style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: '#f6f7f9', color: '#172033' }}>

      {/* 顶部 Tab 栏 */}
      <div className="creation-top-tabs" style={{ display: 'flex', borderBottom: '1px solid #e1e5ea', background: '#fff', padding: '0 22px', flexShrink: 0 }}>
        {(['creation', 'history', 'skills', 'tools'] as const).map((tab) => (
          <button
            key={tab}
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
                      onClick={() => { handleRestoreHistory(item); setTopTab('creation') }}
                    >
                      <span className="creation-history__title">{item.prompt}</span>
                      <span className="creation-history__meta">
                        完整会话 · {item.timestamp} · 模型：{getModelDisplayName(item.model)} · 推理耗时：{formatInferenceLatency(item.latencyMs)}
                      </span>
                      <span className="creation-history__preview">{item.preview}</span>
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
                    onChange={handleSkillPackageSelected}
                    {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
                  />
                  <button
                    type="button"
                    onClick={() => skillPackageInputRef.current?.click()}
                    disabled={uploadingSkillPackage}
                    title="选择一个包含根目录 SKILL.md 的 Codex 技能文件夹"
                  >
                    {uploadingSkillPackage
                      ? <Loader2 className="spin" size={14} />
                      : <Upload size={14} />}
                    {uploadingSkillPackage ? '正在上传…' : '上传技能包'}
                  </button>
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
                <div className="creation-skill-library__empty"><Library size={32} /><strong>还没有技能</strong><span>可以手工新建、上传 Codex 技能包，或去市场安装一份。</span><div className="creation-skill-library__empty-actions"><button type="button" onClick={() => setSkillEditor({})}>新建技能</button><button type="button" onClick={() => setSkillLibraryView('market')}>浏览技能市场</button></div></div>
              ) : (
                <div className="creation-skill-library__grid">
                  {localSkills.map(skill => {
                    const fromMarket = skill.sourceKind === 'market'
                    const imported = skill.sourceKind === 'imported'
                    const sourceStatus = fromMarket
                      ? '来自市场'
                      : imported
                        ? '手工上传'
                        : skill.sourceKind === 'manual'
                          ? null
                          : skill.published
                            ? '已发布'
                            : skill.status === 'draft'
                              ? '草稿'
                              : '已保存'
                    return (
                      <article key={skill.id}>
                        <div className="creation-skill-library__status-row">
                          {sourceStatus && <div className="creation-skill-library__status">{sourceStatus}</div>}
                          <span className={skill.installed ? 'is-installed' : ''}>{skill.installed ? '已安装' : '未安装'}</span>
                        </div>
                        <button
                          type="button"
                          className="creation-skill-library__title"
                          onClick={() => showLocalSkillDetail(skill)}
                        >
                          {skill.title}
                        </button>
                        <p>{skill.summary}</p>
                        <div className="creation-skill-library__meta">
                          {imported
                            ? `${skill.packageFiles?.length || 0} 个文件 · Codex 兼容`
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
                            title="查看这份技能的源文件"
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
                              title="查看这份技能的源文件"
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
                {generatedContent && <span>继续对话优化当前文档</span>}
              </div>
              <div className="creation-chat-header__actions">
                {sessionId && <code>{sessionId.slice(-8)}</code>}
                <button
                  type="button"
                  className="creation-new-session-button"
                  onClick={handleNewConversation}
                  disabled={isGenerating || !hasActiveSession}
                  aria-label="开启新会话"
                  title={isGenerating ? '创作完成或中止后可开启新会话' : '清空当前内容并开启新会话'}
                >
                  <MessageSquarePlus size={15} />
                  新会话
                </button>
              </div>
            </header>
            {(conversation.length > 0 || agentEvents.length > 0) && (
              <div className="creation-chat-timeline" ref={chatTimelineRef} aria-live="polite">
                {creationTimeline.map((item) => {
                  if (item.kind === 'trace') {
                    return (
                      <AgentExecutionTrace
                        key={item.key}
                        events={item.events}
                        onOpenReferences={openBottomTab}
                      />
                    )
                  }

                  const timestamp = formatCreationMessageTimestamp(item.message.createdAt)
                  return (
                    <article
                      key={item.key}
                      className={`creation-chat-message is-${item.message.role}`}
                      aria-label={item.message.role === 'user' ? '用户消息' : 'Agent 消息'}
                    >
                      <div className="creation-chat-message__meta">
                        <span>{item.message.role === 'user' ? userDisplayName : '创作 Agent'}</span>
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
                })}
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
            <div className="creation-prompt-skill-shell" ref={promptSkillShellRef}>
              <MentionHighlightTextarea
                ref={promptInputRef}
                value={prompt}
                mentionLabels={installedSkills.map(skill => skill.title)}
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
                    if (prompt.trim() && !isGenerating) void handleGenerate()
                  }
                }}
                onPaste={(event) => {
                  const files = Array.from(event.clipboardData.files || [])
                  if (files.length) void addFiles(files)
                }}
                placeholder={generatedContent
                  ? '继续告诉 Agent 如何修改当前文档。Enter 发送，Shift+Enter 换行；输入 @ 可选择技能。'
                  : `${defaultPrompt}\n输入 @ 可选择已安装的技能。`}
                style={{ ...inputStyle, minHeight: conversation.length ? 82 : 112, resize: 'vertical', lineHeight: 1.6 }}
                disabled={isGenerating}
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
                  <button type="button" key={match.skill.id} onClick={() => showLocalSkillDetail(match.skill)}>
                    <Sparkles size={13} /> {match.skill.title}
                    <small>{match.reason === 'mentioned' ? '@ 已选择' : '自动匹配'}</small>
                  </button>
                ))}
              </div>
            )}
            {attachments.length > 0 && (
              <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {attachments.map(item => (
                  <span key={item.id} style={attachmentPillStyle}>
                    <Paperclip size={13} />
                    <span style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.name}</span>
                    <small style={{ color: '#667085' }}>{formatAttachmentSize(item.size)}</small>
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
            <div className="creation-composer-actions">
              <div className="creation-model-row">
                <ModelSelect
                  label="模型"
                  value={activeCreationModelId}
                  options={CREATION_MODEL_DEFS}
                  disabled={isGenerating}
                  remoteAllowed={remoteModelAllowed}
                  onChange={handleSelectModel}
                  title="选择创作生成模型"
                />
                {activeCreationModelId === REMOTE_CREATION_MODEL_ID && cloudBalance && (
                  <span style={{ color: '#667085', fontSize: 12 }}>
                    Credit {cloudBalance.available}
                  </span>
                )}
              </div>
              <div className="creation-action-buttons">
                <button onClick={handlePreviewReferences} disabled={!prompt.trim() || isPreviewing || isGenerating} style={secondaryButtonStyle}>
                  {isPreviewing ? <Loader2 size={16} className="spin" /> : <FileText size={16} />}
                  预览参考
                </button>
                <button onClick={() => fileInputRef.current?.click()} disabled={isGenerating} style={secondaryButtonStyle}>
                  <Paperclip size={16} />
                  附件
                </button>
                <button onClick={isGenerating ? handleStopGenerate : handleGenerate} disabled={!isGenerating && !prompt.trim()} style={isGenerating ? dangerButtonStyle : primaryButtonStyle}>
                  {isGenerating ? <Loader2 size={16} className="spin" /> : generatedContent ? <Send size={16} /> : <Sparkles size={16} />}
                  {isGenerating ? '中止' : generatedContent ? '发送' : '开始创作'}
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

          <section className="creation-document-section" aria-label="生成内容" style={{ flex: 1, minHeight: 0, overflow: 'hidden', padding: 22 }}>
            <div className="creation-document-card" style={{ height: '100%', border: '1px solid #e1e5ea', borderRadius: 8, background: '#fff', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              <div className="creation-document-header" style={{ height: 48, padding: '0 16px', borderBottom: '1px solid #e1e5ea', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
                <span style={{ fontSize: 14, fontWeight: 650 }}>
                  创作文档
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
              <div ref={contentRef} style={{ flex: 1, overflowY: 'auto', padding: 20 }}>
                {generatedContent ? (
                  <MarkdownContent
                    content={generatedContent}
                    components={markdownComponents}
                    changes={latestPatchChanges}
                  />
                ) : isGenerating ? (
                  <div style={{ height: '100%', display: 'grid', placeItems: 'center', color: '#667085', fontSize: 14, gap: 12 }}>
                    <Loader2 size={28} className="spin" color="#a45d22" />
                    <div style={{ textAlign: 'center', lineHeight: 1.6 }}>
                      <div style={{ fontWeight: 600, color: '#a45d22', marginBottom: 4 }}>模型正在深度推理中</div>
                      <div>已思考 {elapsedSeconds} 秒，预计进度 {generationProgress}%</div>
                    </div>
                  </div>
                ) : (
                  <div style={{ height: '100%', display: 'grid', placeItems: 'center', color: '#98a2b3', fontSize: 14 }}>
                    输入创作需求后，可以先预览参考资料，也可以直接开始生成。
                  </div>
                )}
              </div>
            </div>
          </section>

          {/* 底部互斥 Tab */}
          <div ref={bottomPanelRef} className="creation-bottom-panel" style={{ background: '#fff', borderTop: '1px solid #e1e5ea', flexShrink: 0 }}>
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
                <button
                  onClick={() => setActiveBottomTab(null)}
                  style={{ marginLeft: 'auto', padding: '4px 10px', border: '1px solid #e1e5ea', borderRadius: 5, background: '#f3f4f6', color: '#6b7280', fontSize: 12, cursor: 'pointer' }}
                >
                  收起
                </button>
              )}
            </div>
            {activeBottomTab === 'reference' && (
              <div id="creation-bottom-panel-reference" role="tabpanel" aria-label="参考资料" style={{ padding: 16, maxHeight: 280, overflowY: 'auto', background: '#fafbfc', borderTop: '1px solid #e1e5ea' }}>
                {referencePreview?.references?.length ? (
                  <div style={{ display: 'grid', gap: 10 }}>
                    {referencePreview.references.map((ref: any) => (
                      <ReferenceRow key={ref.id} item={ref} onOpenSource={handleOpenReferenceSource} />
                    ))}
                  </div>
                ) : (
                  <div style={{ color: '#667085', fontSize: 13 }}>暂无资料，请先点击「预览参考」。</div>
                )}
              </div>
            )}
            {activeBottomTab === 'data' && (
              <div id="creation-bottom-panel-data" role="tabpanel" aria-label="参考数据" className="creation-data-references">
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
                {dataReferences.length ? (
                  <div className="creation-data-reference-list">
                    {dataReferences.map(item => (
                      <DataReferenceRow
                        key={item.source_id}
                        item={item}
                        source={dataSourcesById[item.source_id]}
                        loading={dataReferencesLoading && !dataSourcesById[item.source_id]}
                      />
                    ))}
                  </div>
                ) : !dataReferencesLoading && (
                  <div className="creation-bottom-empty">暂无参考数据，数据检索完成后会显示召回来源和具体指标。</div>
                )}
              </div>
            )}
            {activeBottomTab === 'config' && (
              <div id="creation-bottom-panel-config" role="tabpanel" aria-label="创作参数" style={{ padding: 16, maxHeight: 280, overflowY: 'auto', background: '#fafbfc', borderTop: '1px solid #e1e5ea', display: 'grid', gap: 12 }}>
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
                <div style={{ height: 1, background: '#e1e5ea', margin: '4px 0' }} />
                <div style={{ fontSize: 12, color: '#475467', display: 'flex', justifyContent: 'space-between' }}>
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

const displayAgentName = (event: CreationAgentEvent) => {
  if (event.actor?.id === 'creation_main_agent') return '创作 Agent'
  return String(event.actor?.name || '创作 Agent').replace(/创作主 Agent/g, '创作 Agent')
}

const displayAgentText = (value: unknown) =>
  String(value || '')
    .replace(/创作主 Agent/g, '创作 Agent')
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
  failed: '未完成',
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

const AgentExecutionTrace = ({
  events,
  onOpenReferences,
}: {
  events: CreationAgentEvent[]
  onOpenReferences: (tab: Extract<BottomTab, 'reference' | 'data'>) => void
}) => {
  const [expanded, setExpanded] = useState(true)
  if (!events.length) return null
  const latestGoal = [...events].reverse().find(event => event.goal)?.goal
  const displayEvents = collapseAgentLifecycleEvents(events)
  const eventGroups = groupConsecutiveAgentEvents(displayEvents)
  const webpageScrapeTerminalStatus = [...events]
    .reverse()
    .find(event => (
      event.actor?.id === 'webpage_scrape'
      && ['tool.completed', 'tool.failed'].includes(event.type)
    ))
    ?.status

  return (
    <section className="creation-agent-trace" aria-label="Agent 执行情况">
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
          {displayEvents.length} 个步骤
        </small>
      </button>
      {expanded && (
        <div className="creation-agent-trace__runs">
          <div className="creation-agent-run">
            <div>
              {eventGroups.map((group, groupIndex) => {
                const actorEvent = group.events[0]
                const latestEvent = group.events[group.events.length - 1]
                const resolved = latestEvent.status === 'running' && eventGroups
                  .slice(groupIndex + 1)
                  .some(next => (
                    next.events.some(event => (
                      event.actor?.id === latestEvent.actor?.id
                      && ['completed', 'failed'].includes(event.status)
                    ))
                  ))
                const displayStatus = resolved ? 'completed' : latestEvent.status
                return (
                  <div
                    className={`creation-agent-event is-${displayStatus}`}
                    key={group.key}
                  >
                    <span
                      className={`creation-agent-event__icon is-${actorEvent.actor?.kind || 'agent'}`}
                      aria-hidden="true"
                    >
                      {displayStatus === 'completed'
                          ? <Check size={13} />
                          : actorEvent.actor?.kind === 'tool'
                            ? <Wrench size={13} />
                            : actorEvent.actor?.kind === 'skill'
                              ? <Sparkles size={13} />
                              : <Bot size={13} />}
                      {displayStatus === 'running' && (
                        <span className="creation-agent-event__activity" />
                      )}
                    </span>
                    <span>
                      <strong>
                        {displayAgentName(actorEvent)}
                        <em>{agentActorKindLabel(actorEvent.actor?.kind)}</em>
                      </strong>
                      <div className="creation-agent-event__updates">
                        {group.events.map((event) => {
                          const details = agentEventDetails(event)
                          return (
                            <div
                              className="creation-agent-event__update"
                              key={event.event_id || `${event.run_id}-${event.sequence}`}
                            >
                              <AgentEventSummary
                                event={event}
                                onOpenReferences={onOpenReferences}
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
                    </span>
                  </div>
                )
              })}
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
}: {
  event: CreationAgentEvent
  onOpenReferences: (tab: Extract<BottomTab, 'reference' | 'data'>) => void
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
    : event.type === 'tool.completed' && event.actor?.id === 'data_search'
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
  if (!tab || !Number.isFinite(resultCount) || resultCount <= 0) return <small>{text}</small>

  const match = tab === 'reference'
    ? text.match(/召回\s*\d+\s*条(?:本地)?资料/)
    : text.match(/召回\s*\d+\s*个来源/)
  if (!match || match.index == null) return <small>{text}</small>

  const before = text.slice(0, match.index)
  const after = text.slice(match.index + match[0].length)
  return (
    <small>
      {before}
      <button
        type="button"
        className="creation-agent-reference-link"
        onClick={() => onOpenReferences(tab)}
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
    <div className="creation-browser-previews" aria-label="后台浏览器预览">
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
        : '后台采集中'

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
          alt={`${preview.title || '数据页面'}后台浏览器缩略图`}
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
          {status === 'completed' ? '完整长图 · 点击查看' : '不切换前台窗口'}
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
  if (event.actor?.kind === 'tool' && data.diagram_type) {
    details.push({ label: '图类型', value: String(data.diagram_type) })
  }
  if (event.type === 'tool.failed' && data.error_code) {
    details.push({ label: '错误码', value: String(data.error_code) })
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
  changes: DocumentChange[],
) => {
  const decorated = { ...components }
  ;['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'blockquote', 'pre'].forEach((tag) => {
    const Original = components[tag]
    decorated[tag] = ({ node, className, ...props }: any) => {
      const localStart = Number(node?.position?.start?.line)
      const localEnd = Number(node?.position?.end?.line)
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

const MarkdownContent = ({
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
      {parseMarkdownBlocks(content).map((block, index) => {
        if (block.type === 'markdown') {
          return (
            <ReactMarkdown
              key={`markdown-${index}`}
              components={markdownComponentsWithChanges(components, block.startLine, changes)}
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
                        border: '1px solid #ddc5b2',
                        background: '#f7eadf',
                        color: '#6b3517',
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
                          border: '1px solid #d0d5dd',
                          padding: '10px 12px',
                          textAlign: block.alignments[cellIndex] || 'left',
                          verticalAlign: 'top',
                          background: rowIndex % 2 === 0 ? '#fff' : '#fdf9f5',
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
}

const Toggle = ({ label, checked, onChange, icon }: { label: string; checked: boolean; onChange: (value: boolean) => void; icon?: React.ReactNode }) => (
  <label style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, fontSize: 14, color: '#344054' }}>
    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>{icon}{label}</span>
    <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
  </label>
)

const WeightSlider = ({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) => (
  <label style={{ display: 'grid', gap: 6, marginBottom: 11, fontSize: 12, color: '#667085' }}>
    <span style={{ display: 'flex', justifyContent: 'space-between' }}>
      <span>{label}</span>
      <span>{value}%</span>
    </span>
    <input type="range" min={0} max={70} value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </label>
)

const ProgressStrip = ({ label, percent }: { label: string; percent: number }) => (
  <div style={{ marginTop: 12, display: 'grid', gap: 6, maxWidth: 360 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: '#475467' }}>
      <span>{label}</span>
      <span>{percent}%</span>
    </div>
    <div style={{ height: 6, borderRadius: 999, background: '#e4e7ec', overflow: 'hidden' }}>
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

const ReferenceRow = ({ item, onOpenSource }: { item: ReferenceItem; onOpenSource: (item: ReferenceItem) => void }) => (
  <div style={{ border: '1px solid #e1e5ea', borderRadius: 8, padding: 12 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
      <div style={{ fontSize: 14, fontWeight: 650, lineHeight: 1.35 }}>{item.title}</div>
    </div>
    <div style={{ marginTop: 6, fontSize: 12, color: '#667085' }}>{item.doc_type || '未分类'} · 打开/引用 {item.usage_count}</div>
    <div style={{ marginTop: 8, fontSize: 12, color: '#475467', lineHeight: 1.55 }}>{item.reason}</div>
    <div style={{ marginTop: 10, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6, fontSize: 11, color: '#667085' }}>
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
        border: '1px solid #d0d5dd',
        borderRadius: 6,
        background: '#fff',
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
  border: '1px solid #d0d5dd',
  borderRadius: 8,
  fontSize: 13,
  fontFamily: 'inherit',
  outline: 'none',
  background: '#fff',
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
  background: '#fff',
  color: '#344054',
  border: '1px solid #d0d5dd',
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
  border: '1px solid #d0d5dd',
  borderRadius: 999,
  background: '#fff',
  color: '#344054',
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
  background: '#f2f4f7',
  color: '#475467',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  cursor: 'pointer',
}

export default CreationPanel
