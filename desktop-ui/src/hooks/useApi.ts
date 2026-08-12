import { useCallback, useEffect, useState } from 'react'
import { serviceEnvironmentHeaders, useAppStore } from '../store/useAppStore'
import type { CreationModelConfig } from '../store/useAppStore'
import { LOCAL_CREATION_MODEL_ID, REMOTE_CREATION_MODEL_ID, getEffectiveCreationModelId } from '../utils/modelSelection'
import { toUserFacingError } from '../utils/userFacingError'
import type {
  ArticleTemplate,
  BakeBucket,
  BakeCaptureItem,
  BakeKnowledgeItem,
  CloudMemoryPackageBackupResult,
  CloudMemoryPackageRestoreResult,
  CaptureRecord,
  ConfigCheckActionResult,
  ConfigCheckItem,
  DebugLogContent,
  DebugLogFile,
  DataExtractionSummary,
  DataSource,
  MemoryPackageExportResult,
  MemoryPackageImportReport,
  TimelineItem,
  PaginatedBakeResponse,
  PreferenceRecord,
  RagContext,
  RagHistoryItem,
  RagHistoryPage,
  RagQueryResponse,
  ActionResult,
  SopCandidate,
  WritingStyleConfig,
} from '../types'

const LOCAL_CORE_API = 'http://127.0.0.1:7070'
const LOCAL_MODEL_API = 'http://127.0.0.1:7071'
export const RAG_REFERENCE_LIMIT = 10

export const normalizeLocalApiBaseUrl = (baseUrl: string) =>
  baseUrl.replace(/^http:\/\/localhost(?::7070)?$/i, LOCAL_CORE_API)

const isNetworkLoadFailure = (error: unknown) => {
  const message = error instanceof Error ? error.message : String(error)
  return message === 'Load failed' || message.includes('Failed to fetch') || message.includes('NetworkError')
}

export const fetchWithLocalhostFallback = async (input: string, init?: RequestInit) => {
  try {
    return await fetch(input, init)
  } catch (error) {
    if (!isNetworkLoadFailure(error)) throw error
    const fallbackInput = input.replace(/^http:\/\/localhost(?=:7070\/|\/)/i, 'http://127.0.0.1')
    if (fallbackInput === input) throw error
    return fetch(fallbackInput, init)
  }
}

const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  if (signal?.aborted) {
    reject(new DOMException('Aborted', 'AbortError'))
    return
  }
  const timer = window.setTimeout(resolve, ms)
  signal?.addEventListener('abort', () => {
    window.clearTimeout(timer)
    reject(new DOMException('Aborted', 'AbortError'))
  }, { once: true })
})

export const parseApiErrorMessage = async (resp: Response, fallback: string) => {
  let errMsg = fallback
  try {
    const errJson = await resp.json()
    if (errJson.error === 'MODEL_NOT_READY') {
      errMsg = '向量模型或推理模型尚未就绪，请前往「模型」界面检查模型状态'
    } else if (errJson.error || errJson.message) {
      errMsg = errJson.message || errJson.error
    }
  } catch {
    const errText = await resp.text()
    if (errText) errMsg += ` ${errText}`
  }
  if ((resp.status === 503 || resp.status === 504) && errMsg.startsWith(fallback) && !errMsg.includes('模型')) {
    errMsg = 'AI 正在处理其他任务，请稍候 1-2 分钟再试'
  }
  return errMsg
}

interface RagJobCreateResponse {
  job_id: string
  status: string
}

interface RagJobStatusResponse {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | string
  result?: RagQueryResponse | null
  error?: string | null
  created_at_ms: number
  updated_at_ms: number
}

export interface RagStreamStatus {
  stage: 'queued' | 'understanding' | 'retrieving' | 'answering' | string
  message: string
  progress: number
}

export interface RagStreamCallbacks {
  onStatus?: (status: RagStreamStatus) => void
  onReferences?: (contexts: RagContext[]) => void
  onDelta?: (text: string, accumulated: string) => void
}

interface RagStreamEvent {
  type: 'status' | 'references' | 'delta' | 'done' | 'error' | string
  stage?: string
  message?: string
  progress?: number
  contexts?: RagContext[]
  text?: string
  answer?: string
  model?: string
  done_reason?: string | null
  output_truncated?: boolean
  elapsed_ms?: number
  inference_elapsed_ms?: number
}

export const getActiveCreationModelPayload = (configs: CreationModelConfig[], remoteAllowed = false) => {
  const effectiveId = getEffectiveCreationModelId(configs, remoteAllowed)
  if (effectiveId !== LOCAL_CREATION_MODEL_ID) return {}
  const active = configs.find(config => config.id === LOCAL_CREATION_MODEL_ID)
  if (!active) return {}
  const payload: Record<string, string> = {
    creation_model: LOCAL_CREATION_MODEL_ID,
  }
  if (active.baseUrl) {
    payload.creation_base_url = active.baseUrl
  }
  return payload
}

const consumeRagEventStream = async (
  response: Response,
  callbacks: RagStreamCallbacks,
): Promise<RagQueryResponse> => {
  if (!response.body) throw new Error('咨询流式响应不可读，请重试')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let accumulated = ''
  let result: RagQueryResponse | null = null
  let streamError: Error | null = null

  const consumeEventBlock = (block: string) => {
    const dataText = block
      .split(/\r?\n/)
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\n')
    if (!dataText || dataText === '[DONE]') return
    let event: RagStreamEvent
    try {
      event = JSON.parse(dataText)
    } catch {
      return
    }
    if (event.type === 'status') {
      callbacks.onStatus?.({
        stage: event.stage || 'answering',
        message: event.message || '正在处理',
        progress: Math.max(0, Math.min(100, Number(event.progress) || 0)),
      })
      return
    }
    if (event.type === 'references') {
      callbacks.onReferences?.(Array.isArray(event.contexts) ? event.contexts : [])
      return
    }
    if (event.type === 'delta') {
      const delta = event.text || ''
      if (!delta) return
      accumulated += delta
      callbacks.onDelta?.(delta, accumulated)
      return
    }
    if (event.type === 'error') {
      streamError = new Error(event.message || '咨询流式生成失败，请重试')
      return
    }
    if (event.type === 'done') {
      const answer = String(event.answer ?? accumulated).trim()
      result = {
        answer,
        contexts: Array.isArray(event.contexts) ? event.contexts : [],
        model: event.model || '',
        done_reason: event.done_reason,
        output_truncated: Boolean(event.output_truncated),
        elapsed_ms: Number.isFinite(event.elapsed_ms) ? event.elapsed_ms : undefined,
        inference_elapsed_ms: Number.isFinite(event.inference_elapsed_ms) ? event.inference_elapsed_ms : undefined,
      }
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    let boundary = buffer.search(/\r?\n\r?\n/)
    while (boundary >= 0) {
      const match = buffer.slice(boundary).match(/^\r?\n\r?\n/)?.[0] || '\n\n'
      consumeEventBlock(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + match.length)
      boundary = buffer.search(/\r?\n\r?\n/)
    }
    if (streamError) {
      await reader.cancel().catch(() => undefined)
      throw streamError
    }
    if (done) break
  }
  if (buffer.trim()) consumeEventBlock(buffer)
  if (streamError) throw streamError
  if (!result) throw new Error('咨询流式响应提前结束，请重试')
  return result
}

export async function runRagQueryStream(
  apiBaseUrl: string,
  creationModelConfigs: CreationModelConfig[],
  query: string,
  topK = RAG_REFERENCE_LIMIT,
  extraPayload: Record<string, unknown> = {},
  remoteAllowed = false,
  signal?: AbortSignal,
  callbacks: RagStreamCallbacks = {},
): Promise<RagQueryResponse> {
  const normalizedApiBaseUrl = normalizeLocalApiBaseUrl(apiBaseUrl)
  const response = await fetchWithLocalhostFallback(`${normalizedApiBaseUrl}/api/rag/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    signal,
    body: JSON.stringify({
      query,
      top_k: topK,
      ...getActiveCreationModelPayload(creationModelConfigs, remoteAllowed),
      ...extraPayload,
    }),
  })
  if (!response.ok) {
    throw new Error(await parseApiErrorMessage(response, `query stream failed: ${response.status}`))
  }
  return consumeRagEventStream(response, callbacks)
}

export async function runRagQueryJob(
  apiBaseUrl: string,
  creationModelConfigs: CreationModelConfig[],
  query: string,
  topK = RAG_REFERENCE_LIMIT,
  extraPayload: Record<string, unknown> = {},
  remoteAllowed = false,
  signal?: AbortSignal,
): Promise<RagQueryResponse> {
  const normalizedApiBaseUrl = normalizeLocalApiBaseUrl(apiBaseUrl)
  const createResp = await fetchWithLocalhostFallback(`${normalizedApiBaseUrl}/api/rag/jobs`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body:    JSON.stringify({
      query,
      top_k: topK,
      ...getActiveCreationModelPayload(creationModelConfigs, remoteAllowed),
      ...extraPayload,
    }),
  })
  if (!createResp.ok) {
    throw new Error(await parseApiErrorMessage(createResp, `query job create failed: ${createResp.status}`))
  }

  const created: RagJobCreateResponse = await createResp.json()
  const deadline = Date.now() + 10 * 60 * 1000
  let data: RagQueryResponse | null = null

  while (Date.now() < deadline) {
    await sleep(2000, signal)
    const statusResp = await fetchWithLocalhostFallback(`${normalizedApiBaseUrl}/api/rag/jobs/${encodeURIComponent(created.job_id)}`, { signal })
    if (!statusResp.ok) {
      throw new Error(await parseApiErrorMessage(statusResp, `query job status failed: ${statusResp.status}`))
    }

    const job: RagJobStatusResponse = await statusResp.json()
    if (job.status === 'succeeded') {
      data = job.result ?? null
      break
    }
    if (job.status === 'failed') {
      throw new Error(job.error || '咨询生成失败，请稍后重试')
    }
  }

  if (!data) {
    throw new Error('咨询生成等待超时，请稍后在「咨询记录」中查看是否已完成')
  }

  return data
}

async function readGatewayError(response: Response, fallback: string) {
  try {
    const text = await response.text()
    if (!text.trim()) return fallback
    const data = JSON.parse(text)
    return data?.error?.message || data?.message || data?.error || fallback
  } catch {
    return fallback
  }
}

async function fetchRagReferences(apiBaseUrl: string, query: string, signal?: AbortSignal): Promise<RagContext[]> {
  const normalizedApiBaseUrl = normalizeLocalApiBaseUrl(apiBaseUrl)
  try {
    const response = await fetchWithLocalhostFallback(`${normalizedApiBaseUrl}/api/rag/references`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify({ query, top_k: RAG_REFERENCE_LIMIT }),
    })
    if (!response.ok) return []
    const data: RagQueryResponse = await response.json()
    return Array.isArray(data.contexts) ? data.contexts : []
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    return []
  }
}

interface GatewayRagHistoryOptions {
  source?: string
  metadata?: Record<string, unknown>
}

const optionalNumber = (value: unknown): number | null => {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

const buildFloatingAssistHistoryContext = (metadata?: Record<string, unknown>): RagContext | null => {
  if (metadata?.source !== 'floating_assist') return null
  return {
    capture_id: 0,
    doc_key: `floating-assist:${Date.now()}`,
    text: String(metadata.ocr_text || metadata.manual_instruction || '悬浮球咨询'),
    score: 1,
    source: 'floating_assist',
    source_type: 'floating_assist',
    screenshot_path: typeof metadata.screenshot_path === 'string' ? metadata.screenshot_path : null,
    screenshot_width: optionalNumber(metadata.screenshot_width),
    screenshot_height: optionalNumber(metadata.screenshot_height),
  }
}

export async function runGatewayRagQuery(
  apiBaseUrl: string,
  gatewayApiBaseUrl: string,
  query: string,
  userId?: string | null,
  signal?: AbortSignal,
  historyOptions?: GatewayRagHistoryOptions,
): Promise<RagQueryResponse> {
  const startedAt = Date.now()
  const contexts = await fetchRagReferences(apiBaseUrl, query, signal)
  const referenceText = contexts.length
    ? `\n\n本地记忆参考资料：\n${contexts.map((item, index) => {
      const title = item.title || item.win_title || item.app_name || item.doc_key || `参考资料 ${index + 1}`
      const rawText = item.summary || item.overview || item.text || ''
      const text = rawText.length > 800 ? `${rawText.slice(0, 800)}...` : rawText
      return `R#${index + 1} ${title}\n${text}`.trim()
    }).join('\n\n')}`
    : ''
  const normalizedGateway = gatewayApiBaseUrl.replace(/\/+$/, '')
  const response = await fetch(`${normalizedGateway}/v1/gateway/chat`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      request_id: `rag-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      user_id: userId || null,
      brand_model_id: REMOTE_CREATION_MODEL_ID,
      caller: 'rag',
      messages: [
        {
          role: 'system',
          content: '你是 MemoryBread 的咨询助手。请用清晰、结构化的中文回答，不要提及底层供应商或模型实现。',
        },
        { role: 'user', content: `${query}${referenceText}` },
      ],
      stream: false,
      privacy: { content_logging: false, client_scrubbed: true },
      limits: { max_output_tokens: 4096, max_credit: '50.0000' },
    }),
  })
  if (!response.ok) {
    throw new Error(await readGatewayError(response, `云端咨询失败: ${response.status}`))
  }
  const data = await response.json()
  const answer = String(data.content || data.answer || '').trim()
  if (!answer) throw new Error('云端咨询没有返回内容，请稍后重试或切换本地模型')
  const result: RagQueryResponse = {
    answer,
    contexts,
    model: REMOTE_CREATION_MODEL_ID,
  }
  const normalizedApiBaseUrl = normalizeLocalApiBaseUrl(apiBaseUrl)
  const floatingContext = buildFloatingAssistHistoryContext(historyOptions?.metadata)
  const historyContexts = floatingContext ? [floatingContext, ...contexts] : contexts
  await fetchWithLocalhostFallback(`${normalizedApiBaseUrl}/api/rag/history`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query,
      answer,
      contexts: historyContexts,
      latency_ms: Date.now() - startedAt,
      model: REMOTE_CREATION_MODEL_ID,
      source: historyOptions?.source,
      scene_type: historyOptions?.source === 'floating_assist' ? 'floating_assist' : undefined,
    }),
  }).catch(() => undefined)
  return result
}

export async function runGatewayRagQueryStream(
  apiBaseUrl: string,
  gatewayApiBaseUrl: string,
  query: string,
  userId: string,
  signal?: AbortSignal,
  historyOptions?: GatewayRagHistoryOptions,
  callbacks: RagStreamCallbacks = {},
): Promise<RagQueryResponse> {
  const startedAt = Date.now()
  callbacks.onStatus?.({ stage: 'retrieving', message: '正在召回相关资料', progress: 42 })
  const contexts = await fetchRagReferences(apiBaseUrl, query, signal)
  callbacks.onReferences?.(contexts)
  callbacks.onStatus?.({ stage: 'answering', message: '正在生成答案', progress: 58 })
  const referenceText = contexts.length
    ? `\n\n本地记忆参考资料：\n${contexts.map((item, index) => {
      const title = item.title || item.win_title || item.app_name || item.doc_key || `参考资料 ${index + 1}`
      const rawText = item.summary || item.overview || item.text || ''
      const text = rawText.length > 800 ? `${rawText.slice(0, 800)}...` : rawText
      return `R#${index + 1} ${title}\n${text}`.trim()
    }).join('\n\n')}`
    : ''
  const normalizedGateway = gatewayApiBaseUrl.replace(/\/+$/, '')
  const response = await fetch(`${normalizedGateway}/v1/gateway/chat`, {
    method: 'POST',
    headers: {
      ...serviceEnvironmentHeaders(),
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    signal,
    body: JSON.stringify({
      request_id: `rag-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      user_id: userId,
      brand_model_id: REMOTE_CREATION_MODEL_ID,
      caller: 'rag',
      messages: [
        {
          role: 'system',
          content: '你是 MemoryBread 的咨询助手。请用清晰、结构化的中文回答，不要提及底层供应商或模型实现。',
        },
        { role: 'user', content: `${query}${referenceText}` },
      ],
      stream: true,
      privacy: { content_logging: false, client_scrubbed: true },
      limits: { max_output_tokens: 4096, max_credit: '50.0000' },
    }),
  })
  if (!response.ok) {
    throw new Error(await readGatewayError(response, `云端咨询失败: ${response.status}`))
  }
  const streamed = await consumeRagEventStream(response, callbacks)
  const result: RagQueryResponse = {
    ...streamed,
    contexts,
    model: REMOTE_CREATION_MODEL_ID,
    elapsed_ms: streamed.elapsed_ms ?? Date.now() - startedAt,
  }

  const normalizedApiBaseUrl = normalizeLocalApiBaseUrl(apiBaseUrl)
  const floatingContext = buildFloatingAssistHistoryContext(historyOptions?.metadata)
  const historyContexts = floatingContext ? [floatingContext, ...contexts] : contexts
  await fetchWithLocalhostFallback(`${normalizedApiBaseUrl}/api/rag/history`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query,
      answer: result.answer,
      contexts: historyContexts,
      latency_ms: result.elapsed_ms,
      model: REMOTE_CREATION_MODEL_ID,
      source: historyOptions?.source,
      scene_type: historyOptions?.source === 'floating_assist' ? 'floating_assist' : undefined,
    }),
  }).catch(() => undefined)
  return result
}

export interface ModelStatus {
  llm: boolean
  embedding: boolean
  runtime: boolean
}

export function useModelStatus() {
  const [status, setStatus] = useState<ModelStatus>({ llm: false, embedding: false, runtime: false })
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const check = async () => {
      try {
        const resp = await fetch(`${LOCAL_MODEL_API}/api/models`)
        if (!resp.ok) {
          setStatus({ llm: false, embedding: false, runtime: false })
          return
        }
        const data = await resp.json()
        const models = data.models || []
        const llm = models.some((m: any) => m.category === 'llm' && (m.status === 'active' || m.is_active))
        const embedding = models.some((m: any) => m.category === 'embedding' && (m.status === 'active' || m.is_active))
        const runtime = models.some((m: any) => m.category === 'inference_engine' && (m.status === 'active' || m.is_active))
        setStatus({ llm, embedding, runtime })
      } catch {
        setStatus({ llm: false, embedding: false, runtime: false })
      } finally {
        setLoading(false)
      }
    }
    check()
    const interval = setInterval(check, 10000)
    return () => clearInterval(interval)
  }, [])

  return { status, loading, ready: status.llm && status.embedding && status.runtime }
}

export function useHealthCheck() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<{ status: string; version: string }> => {
    const resp = await fetch(`${normalizeLocalApiBaseUrl(apiBaseUrl)}/health`)
    if (!resp.ok) throw new Error(`health check failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export interface FetchCapturesParams {
  from?:  number
  to?:    number
  app?:   string
  q?:     string
  limit?: number
  offset?: number
  ids?:   string
}

export function useFetchCaptures() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: FetchCapturesParams = {}): Promise<{
    total: number
    captures: CaptureRecord[]
  }> => {
    const url   = new URL(`${apiBaseUrl}/captures`)
    if (params.from  != null) url.searchParams.set('from',  String(params.from))
    if (params.to    != null) url.searchParams.set('to',    String(params.to))
    if (params.app)            url.searchParams.set('app',   params.app)
    if (params.q)              url.searchParams.set('q',     params.q)
    if (params.limit != null)  url.searchParams.set('limit', String(params.limit))
    if (params.offset != null) url.searchParams.set('offset', String(params.offset))
    if (params.ids)            url.searchParams.set('ids',   params.ids)

    const resp = await fetch(url.toString())
    if (!resp.ok) throw new Error(`captures fetch failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export function useRagQuery() {
  const apiBaseUrl   = useAppStore((s) => normalizeLocalApiBaseUrl(s.apiBaseUrl))
  const gatewayApiBaseUrl = useAppStore((s) => s.gatewayApiBaseUrl)
  const creationModelConfigs = useAppStore((s) => s.creationModelConfigs)
  const currentUser = useAppStore((s) => s.currentUser)
  const cloudBalance = useAppStore((s) => s.cloudBalance)
  const setLoading   = useAppStore((s) => s.setRagLoading)
  const setResult    = useAppStore((s) => s.setRagResult)
  const setError     = useAppStore((s) => s.setRagError)

  return useCallback(async (query: string, topK = RAG_REFERENCE_LIMIT, extraPayload: Record<string, unknown> = {}, signal?: AbortSignal): Promise<RagQueryResponse> => {
    setLoading(true)
    try {
      const remoteAllowed = Boolean(currentUser) && Number(cloudBalance?.available ?? 0) > 0
      const activeModelId = getEffectiveCreationModelId(creationModelConfigs, remoteAllowed)
      const data = activeModelId === REMOTE_CREATION_MODEL_ID
        ? await runGatewayRagQuery(apiBaseUrl, gatewayApiBaseUrl, query, currentUser?.id, signal)
        : await runRagQueryJob(apiBaseUrl, creationModelConfigs, query, topK, extraPayload, remoteAllowed, signal)
      setResult(data.answer, data.contexts ?? [])
      return data
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        setLoading(false)
        throw err
      }
      setError(toUserFacingError(err, '咨询暂时无法完成，请确认应用运行状态后重试'))
      throw err
    }
  }, [apiBaseUrl, gatewayApiBaseUrl, creationModelConfigs, currentUser, cloudBalance, setLoading, setResult, setError])
}

export function useFetchRagHistory() {
  const apiBaseUrl = useAppStore((s) => normalizeLocalApiBaseUrl(s.apiBaseUrl))

  return useCallback(async (
    params: { limit?: number; offset?: number; query?: string } = {},
    signal?: AbortSignal,
  ): Promise<RagHistoryPage> => {
    const limit = params.limit ?? 20
    const offset = params.offset ?? 0
    const searchParams = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })
    if (params.query?.trim()) searchParams.set('q', params.query.trim())
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/rag/history?${searchParams}`, { signal })
    if (resp.status === 404) return { items: [], total: 0, limit, offset }
    if (!resp.ok) throw new Error(`rag history fetch failed: ${resp.status}`)
    const data = await resp.json()
    const items: RagHistoryItem[] = Array.isArray(data) ? data : data.items ?? []
    return {
      items,
      total: Number.isFinite(Number(data?.total)) ? Number(data.total) : items.length,
      limit: Number.isFinite(Number(data?.limit)) ? Number(data.limit) : limit,
      offset: Number.isFinite(Number(data?.offset)) ? Number(data.offset) : offset,
    }
  }, [apiBaseUrl])
}

export function useFetchPreferences() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<PreferenceRecord[]> => {
    const resp = await fetch(`${apiBaseUrl}/preferences`)
    if (!resp.ok) throw new Error(`preferences fetch failed: ${resp.status}`)
    const data = await resp.json()
    return data.preferences
  }, [apiBaseUrl])
}

export function useFetchDebugLogFiles() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<DebugLogFile[]> => {
    const resp = await fetch(`${apiBaseUrl}/api/debug/log-files`)
    if (!resp.ok) throw new Error(`debug log files fetch failed: ${resp.status}`)
    const data = await resp.json()
    return data.items ?? []
  }, [apiBaseUrl])
}

export function useFetchDebugLogContent() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (key: string): Promise<DebugLogContent> => {
    const resp = await fetch(`${apiBaseUrl}/api/debug/log-files/${encodeURIComponent(key)}`)
    if (!resp.ok) throw new Error(`debug log content fetch failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export function useUpdatePreference() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (key: string, value: string): Promise<PreferenceRecord> => {
    const resp = await fetch(`${apiBaseUrl}/preferences/${encodeURIComponent(key)}`, {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ value }),
    })
    if (!resp.ok) throw new Error(`update preference failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export interface ScreenshotCleanupResult {
  keep_days: number
  deleted_count: number
  freed_bytes: number
}

export interface CaptureCleanupResult {
  retention_days: number
  deleted_count: number
  deleted_screenshot_count: number
  freed_bytes: number
}

export function useRunScreenshotCleanup() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<ScreenshotCleanupResult> => {
    const resp = await fetch(`${apiBaseUrl}/preferences/screenshot-cleanup/run`, {
      method: 'POST',
    })
    if (!resp.ok) throw new Error(`run screenshot cleanup failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export function useRunCaptureCleanup() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<CaptureCleanupResult> => {
    const resp = await fetch(`${apiBaseUrl}/preferences/capture-cleanup/run`, {
      method: 'POST',
    })
    if (!resp.ok) throw new Error(`run capture cleanup failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export function useFetchConfigChecks() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<ConfigCheckItem[]> => {
    const resp = await fetch(`${apiBaseUrl}/api/config-checks`)
    if (!resp.ok) throw new Error(`config checks fetch failed: ${resp.status}`)
    const data = await resp.json()
    return data.items ?? []
  }, [apiBaseUrl])
}

export function useRunConfigCheckAction() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (
    id: string,
    action: 'verify' | 'install' | 'delete',
  ): Promise<ConfigCheckActionResult> => {
    const path = action === 'delete'
      ? `${apiBaseUrl}/api/config-checks/${encodeURIComponent(id)}`
      : `${apiBaseUrl}/api/config-checks/${encodeURIComponent(id)}/${action}`
    const resp = await fetch(path, {
      method: action === 'delete' ? 'DELETE' : 'POST',
    })
    if (!resp.ok) throw new Error(`config check ${action} failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export function useExecuteAction() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (action: Record<string, unknown>): Promise<ActionResult> => {
    const resp = await fetch(`${apiBaseUrl}/action/execute`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(action),
    })
    if (!resp.ok) throw new Error(`execute action failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export function useExportMemoryPackage() {
  const apiBaseUrl = useAppStore((s) => normalizeLocalApiBaseUrl(s.apiBaseUrl))

  return useCallback(async (): Promise<MemoryPackageExportResult> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/snapshots/assets/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    if (!resp.ok) {
      throw new Error(await parseApiErrorMessage(resp, `memory package export failed: ${resp.status}`))
    }
    return resp.json()
  }, [apiBaseUrl])
}

export function useImportMemoryPackage() {
  const apiBaseUrl = useAppStore((s) => normalizeLocalApiBaseUrl(s.apiBaseUrl))

  return useCallback(async (content: string, dryRun = false): Promise<MemoryPackageImportReport> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/snapshots/assets/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, dry_run: dryRun }),
    })
    if (!resp.ok) {
      throw new Error(await parseApiErrorMessage(resp, `memory package import failed: ${resp.status}`))
    }
    return resp.json()
  }, [apiBaseUrl])
}

export function useBackupMemoryPackageToCloud() {
  const apiBaseUrl = useAppStore((s) => normalizeLocalApiBaseUrl(s.apiBaseUrl))

  return useCallback(async (payload: {
    admin_base_url: string
    service_environment: 'production' | 'staging'
    access_token: string
    device_id: string
  }): Promise<CloudMemoryPackageBackupResult> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/snapshots/cloud/backup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) {
      throw new Error(await parseApiErrorMessage(resp, `cloud memory package backup failed: ${resp.status}`))
    }
    return resp.json()
  }, [apiBaseUrl])
}

export function useRestoreMemoryPackageFromCloud() {
  const apiBaseUrl = useAppStore((s) => normalizeLocalApiBaseUrl(s.apiBaseUrl))

  return useCallback(async (payload: {
    admin_base_url: string
    service_environment: 'production' | 'staging'
    access_token: string
    snapshot_id: string
    import_to_local: boolean
    dry_run?: boolean
  }): Promise<CloudMemoryPackageRestoreResult> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/snapshots/cloud/restore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) {
      throw new Error(await parseApiErrorMessage(resp, `cloud memory package restore failed: ${resp.status}`))
    }
    return resp.json()
  }, [apiBaseUrl])
}

export interface BakeOverviewResponse {
  capture_count: number
  memory_count: number
  data_count?: number
  knowledge_count: number
  template_count: number
  sop_count?: number
  pending_candidates: number
  recent_activities: string[]
  inventory_trend?: Array<{
    label: string
    start_ts: number
    end_ts: number
    memory_count: number
    data_count?: number
    knowledge_count: number
    template_count: number
    sop_count: number
  }>
}

export interface DataSourceListResponse {
  items: DataSource[]
  total: number
  pending_items?: DataSource[]
  pending_total?: number
  limit: number
  offset: number
}

export function useFetchDataSources() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: { q?: string; limit?: number; offset?: number } = {}): Promise<DataSourceListResponse> => {
    const url = new URL(`${apiBaseUrl}/api/data/sources`)
    if (params.q) url.searchParams.set('q', params.q)
    if (params.limit != null) url.searchParams.set('limit', String(params.limit))
    if (params.offset != null) url.searchParams.set('offset', String(params.offset))
    const resp = await fetchWithLocalhostFallback(url.toString())
    if (!resp.ok) throw new Error(await parseApiErrorMessage(resp, `data source fetch failed: ${resp.status}`))
    return resp.json()
  }, [apiBaseUrl])
}

export function useFetchDataSource() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (sourceId: number): Promise<DataSource> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/data/sources/${sourceId}`)
    if (!resp.ok) throw new Error(await parseApiErrorMessage(resp, `data source fetch failed: ${resp.status}`))
    return resp.json()
  }, [apiBaseUrl])
}

export function useExtractDataSources() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (limit = 1000): Promise<DataExtractionSummary> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/data/sources/extract`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit }),
    })
    if (!resp.ok) throw new Error(await parseApiErrorMessage(resp, `data source extraction failed: ${resp.status}`))
    return resp.json()
  }, [apiBaseUrl])
}

export function useRefreshDataSource() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (sourceId: number, mode: 'auto' | 'browser' | 'http' = 'auto') => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/data/sources/${sourceId}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    })
    if (!resp.ok) throw new Error(await parseApiErrorMessage(resp, `data source refresh failed: ${resp.status}`))
    return resp.json() as Promise<{
      schema_version: string
      source_id: number
      collector: string
      browser?: 'chrome' | 'chrome_canary' | 'edge' | 'brave' | 'chromium' | 'vivaldi' | 'safari' | null
      interaction_mode?: 'background_tab' | 'background_browser_window' | 'temporary_foreground_tab' | 'none'
      collected_at: number
      title: string
      url: string
      content_text: string
      structured_data: Record<string, unknown>
      content_hash: string
    }>
  }, [apiBaseUrl])
}

export function useDeleteDataSource() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (sourceId: number): Promise<void> => {
    const resp = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/data/sources/${sourceId}`, {
      method: 'DELETE',
    })
    if (!resp.ok) throw new Error(await parseApiErrorMessage(resp, `data source delete failed: ${resp.status}`))
  }, [apiBaseUrl])
}

export function useFetchBakeOverview() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<BakeOverviewResponse> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/overview`)
    if (!resp.ok) throw new Error(`bake overview fetch failed: ${resp.status}`)
    return resp.json()
  }, [apiBaseUrl])
}

export interface BakeListQueryParams {
  q?: string
  bucket?: BakeBucket
  sort?: 'recent' | 'heat'
  from?: number
  to?: number
  limit?: number
  offset?: number
}

export interface BakeCaptureListQueryParams extends BakeListQueryParams {
  source_capture_id?: number
}

export function useFetchBakeMemories() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: BakeListQueryParams = {}): Promise<PaginatedBakeResponse<TimelineItem>> => {
    const buildUrl = (path: string) => {
      const url = new URL(`${apiBaseUrl}${path}`)
      if (params.q) url.searchParams.set('q', params.q)
      if (params.from != null) url.searchParams.set('from', String(params.from))
      if (params.to != null) url.searchParams.set('to', String(params.to))
      if (params.limit != null) url.searchParams.set('limit', String(params.limit))
      if (params.offset != null) url.searchParams.set('offset', String(params.offset))
      return url
    }

    const resp = await fetch(buildUrl('/api/knowledge').toString())
    if (!resp.ok) throw new Error(`timelines fetch failed: ${resp.status}`)
    const data = await resp.json()
    return {
      items: (data.entries ?? []).map(mapKnowledgeEntryToTimeline),
      total: data.total ?? 0,
      limit: data.limit ?? params.limit ?? 20,
      offset: data.offset ?? params.offset ?? 0,
    }
  }, [apiBaseUrl])
}

export function useFetchBakeMemory() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<TimelineItem> => {
    const resp = await fetch(`${apiBaseUrl}/api/knowledge/${encodeURIComponent(id)}`)
    if (!resp.ok) throw new Error(`timeline fetch failed: ${resp.status}`)
    return mapKnowledgeEntryToTimeline(await resp.json())
  }, [apiBaseUrl])
}

export function useDeleteBakeMemory() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<void> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/memories/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!resp.ok) throw new Error(`delete timeline failed: ${resp.status}`)
  }, [apiBaseUrl])
}

export function useFetchBakeKnowledge() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: BakeListQueryParams = {}): Promise<PaginatedBakeResponse<BakeKnowledgeItem>> => {
    const url = new URL(`${apiBaseUrl}/api/bake/knowledge`)
    if (params.q) url.searchParams.set('q', params.q)
    if (params.bucket) url.searchParams.set('bucket', params.bucket)
    if (params.sort) url.searchParams.set('sort', params.sort)
    if (params.from != null) url.searchParams.set('from', String(params.from))
    if (params.to != null) url.searchParams.set('to', String(params.to))
    if (params.limit != null) url.searchParams.set('limit', String(params.limit))
    if (params.offset != null) url.searchParams.set('offset', String(params.offset))

    const resp = await fetch(url.toString())
    if (!resp.ok) throw new Error(`bake knowledge fetch failed: ${resp.status}`)
    const data = await resp.json()
    return {
      items: (data.items ?? []).map(mapBakeKnowledge),
      total: data.total ?? 0,
      limit: data.limit ?? params.limit ?? 20,
      offset: data.offset ?? params.offset ?? 0,
    }
  }, [apiBaseUrl])
}

export function useFetchBakeKnowledgeDetail() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<BakeKnowledgeItem> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/knowledge/${encodeURIComponent(id)}`)
    if (!resp.ok) throw new Error(`bake knowledge detail fetch failed: ${resp.status}`)
    return mapBakeKnowledge(await resp.json())
  }, [apiBaseUrl])
}

export function useDeleteBakeKnowledge() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<void> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/knowledge/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!resp.ok) throw new Error(`delete bake knowledge failed: ${resp.status}`)
  }, [apiBaseUrl])
}

export function useFetchBakeCaptures() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: BakeCaptureListQueryParams = {}): Promise<PaginatedBakeResponse<BakeCaptureItem>> => {
    const url = new URL(`${apiBaseUrl}/api/bake/captures`)
    if (params.q) url.searchParams.set('q', params.q)
    if (params.from != null) url.searchParams.set('from', String(params.from))
    if (params.to != null) url.searchParams.set('to', String(params.to))
    if (params.source_capture_id != null) url.searchParams.set('source_capture_id', String(params.source_capture_id))
    if (params.limit != null) url.searchParams.set('limit', String(params.limit))
    if (params.offset != null) url.searchParams.set('offset', String(params.offset))

    const resp = await fetch(url.toString())
    if (!resp.ok) throw new Error(`bake captures fetch failed: ${resp.status}`)
    const data = await resp.json()
    return {
      items: (data.items ?? data.captures ?? []).map(mapBakeCapture),
      total: data.total ?? 0,
      limit: data.limit ?? params.limit ?? 20,
      offset: data.offset ?? params.offset ?? 0,
    }
  }, [apiBaseUrl])
}

export function useFetchBakeCaptureDetail() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<BakeCaptureItem> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/captures/${encodeURIComponent(id)}`)
    if (!resp.ok) throw new Error(`bake capture detail fetch failed: ${resp.status}`)
    return mapBakeCapture(await resp.json())
  }, [apiBaseUrl])
}

export function useDeleteBakeCapture() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<void> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/captures/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!resp.ok) throw new Error(`delete capture failed: ${resp.status}`)
  }, [apiBaseUrl])
}

export function useInitializeBakeMemories() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (limit = 20): Promise<{ created_count: number; skipped_count: number; memories: TimelineItem[] }> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/memories/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit }),
    })
    if (!resp.ok) throw new Error(`initialize bake memories failed: ${resp.status}`)
    const data = await resp.json()
    return {
      created_count: data.created_count ?? 0,
      skipped_count: data.skipped_count ?? 0,
      memories: (data.memories ?? data.articles ?? []).map(mapBakeMemory),
    }
  }, [apiBaseUrl])
}

export function useIgnoreBakeMemory() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<TimelineItem> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/memories/${encodeURIComponent(id)}/ignore`, { method: 'POST' })
    if (!resp.ok) throw new Error(`ignore bake memory failed: ${resp.status}`)
    return mapBakeMemory(await resp.json())
  }, [apiBaseUrl])
}

export function usePromoteBakeMemoryToTemplate() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<ArticleTemplate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/memories/${encodeURIComponent(id)}/promote-design`, { method: 'POST' })
    if (!resp.ok) throw new Error(`promote bake memory to design failed: ${resp.status}`)
    const item = await resp.json()
    return mapBakeTemplate(item)
  }, [apiBaseUrl])
}

export function usePromoteBakeMemoryToSop() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<SopCandidate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/memories/${encodeURIComponent(id)}/promote-sop`, { method: 'POST' })
    if (!resp.ok) throw new Error(`promote bake memory to sop failed: ${resp.status}`)
    const item = await resp.json()
    return mapBakeSop(item)
  }, [apiBaseUrl])
}

export function useFetchBakeTemplates() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: BakeListQueryParams = {}): Promise<PaginatedBakeResponse<ArticleTemplate>> => {
    const url = new URL(`${apiBaseUrl}/api/bake/documents`)
    if (params.q) url.searchParams.set('q', params.q)
    if (params.bucket) url.searchParams.set('bucket', params.bucket)
    if (params.from != null) url.searchParams.set('from', String(params.from))
    if (params.to != null) url.searchParams.set('to', String(params.to))
    if (params.limit != null) url.searchParams.set('limit', String(params.limit))
    if (params.offset != null) url.searchParams.set('offset', String(params.offset))

    const resp = await fetch(url.toString())
    if (!resp.ok) throw new Error(`bake documents fetch failed: ${resp.status}`)
    const data = await resp.json()
    return {
      items: (data.items ?? data.templates ?? []).map(mapBakeTemplate),
      total: data.total ?? 0,
      limit: data.limit ?? params.limit ?? 20,
      offset: data.offset ?? params.offset ?? 0,
    }
  }, [apiBaseUrl])
}

export function useFetchBakeTemplate() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<ArticleTemplate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/documents/${encodeURIComponent(id)}`)
    if (!resp.ok) throw new Error(`bake document fetch failed: ${resp.status}`)
    return mapBakeTemplate(await resp.json())
  }, [apiBaseUrl])
}

export function useCreateBakeTemplate() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (template: ArticleTemplate): Promise<ArticleTemplate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/documents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializeBakeTemplate(template)),
    })
    if (!resp.ok) throw new Error(`create bake document failed: ${resp.status}`)
    return mapBakeTemplate(await resp.json())
  }, [apiBaseUrl])
}

export function useUpdateBakeTemplate() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (template: ArticleTemplate): Promise<ArticleTemplate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/documents/${encodeURIComponent(template.id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializeBakeTemplate(template)),
    })
    if (!resp.ok) throw new Error(`update bake document failed: ${resp.status}`)
    return mapBakeTemplate(await resp.json())
  }, [apiBaseUrl])
}

export function useToggleBakeTemplateStatus() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<ArticleTemplate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/documents/${encodeURIComponent(id)}/toggle-status`, { method: 'POST' })
    if (!resp.ok) throw new Error(`toggle bake document failed: ${resp.status}`)
    return mapBakeTemplate(await resp.json())
  }, [apiBaseUrl])
}

export function useDeleteBakeTemplate() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<void> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/documents/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!resp.ok) throw new Error(`delete bake document failed: ${resp.status}`)
  }, [apiBaseUrl])
}

export function useFetchBakeSops() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (params: BakeListQueryParams = {}): Promise<PaginatedBakeResponse<SopCandidate>> => {
    const url = new URL(`${apiBaseUrl}/api/bake/sops`)
    if (params.q) url.searchParams.set('q', params.q)
    if (params.bucket) url.searchParams.set('bucket', params.bucket)
    if (params.from != null) url.searchParams.set('from', String(params.from))
    if (params.to != null) url.searchParams.set('to', String(params.to))
    if (params.limit != null) url.searchParams.set('limit', String(params.limit))
    if (params.offset != null) url.searchParams.set('offset', String(params.offset))

    const resp = await fetch(url.toString())
    if (!resp.ok) throw new Error(`bake sops fetch failed: ${resp.status}`)
    const data = await resp.json()
    return {
      items: (data.items ?? data.candidates ?? []).map(mapBakeSop),
      total: data.total ?? 0,
      limit: data.limit ?? params.limit ?? 20,
      offset: data.offset ?? params.offset ?? 0,
    }
  }, [apiBaseUrl])
}

export function useFetchBakeSop() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<SopCandidate> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/sops/${encodeURIComponent(id)}`)
    if (!resp.ok) throw new Error(`bake sop fetch failed: ${resp.status}`)
    return mapBakeSop(await resp.json())
  }, [apiBaseUrl])
}

export function useDeleteBakeSop() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (id: string): Promise<void> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/sops/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!resp.ok) throw new Error(`delete bake sop failed: ${resp.status}`)
  }, [apiBaseUrl])
}

export function useFetchBakeStyleConfig() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (): Promise<WritingStyleConfig> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/style-config`)
    if (!resp.ok) throw new Error(`bake style config fetch failed: ${resp.status}`)
    const item = await resp.json()
    return {
      preferredPhrases: item.preferred_phrases ?? [],
      replacementRules: item.replacement_rules ?? [],
      styleSamples: item.style_samples ?? [],
      applyToCreation: item.apply_to_creation ?? true,
      applyToTemplateEditing: item.apply_to_template_editing ?? true,
    }
  }, [apiBaseUrl])
}

export function useUpdateBakeStyleConfig() {
  const apiBaseUrl = useAppStore((s) => s.apiBaseUrl)

  return useCallback(async (config: WritingStyleConfig): Promise<WritingStyleConfig> => {
    const resp = await fetch(`${apiBaseUrl}/api/bake/style-config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        preferred_phrases: config.preferredPhrases,
        replacement_rules: config.replacementRules,
        style_samples: config.styleSamples,
        apply_to_creation: config.applyToCreation,
        apply_to_template_editing: config.applyToTemplateEditing,
      }),
    })
    if (!resp.ok) throw new Error(`update bake style config failed: ${resp.status}`)
    const item = await resp.json()
    return {
      preferredPhrases: item.preferred_phrases ?? [],
      replacementRules: item.replacement_rules ?? [],
      styleSamples: item.style_samples ?? [],
      applyToCreation: item.apply_to_creation ?? true,
      applyToTemplateEditing: item.apply_to_template_editing ?? true,
    }
  }, [apiBaseUrl])
}

function normalizeTimelineKeyTimestamps(value: unknown): TimelineItem['keyTimestamps'] {
  if (!Array.isArray(value)) return undefined

  return value.flatMap(segment => {
    if (!segment || typeof segment !== 'object') return []

    const raw = segment as Record<string, unknown>
    const startTs = Number(raw.start_ts)
    const endTs = Number(raw.end_ts)
    if (!Number.isFinite(startTs) || !Number.isFinite(endTs)) return []

    return [{
      // 旧数据和早期演示数据可能只有时间范围与摘要。统一补成空数组，
      // 由展示层按时间范围关联 capture，避免缺字段直接击穿 React 根节点。
      capture_ids: Array.isArray(raw.capture_ids)
        ? raw.capture_ids.map(Number).filter(Number.isFinite)
        : [],
      start_ts: startTs,
      end_ts: endTs,
      summary: typeof raw.summary === 'string' ? raw.summary : '',
    }]
  })
}

function mapKnowledgeEntryToTimeline(item: any): TimelineItem {
  return {
    id: String(item.id),
    title: item.summary ?? '',
    summary: item.overview ?? item.summary,
    details: item.details ?? undefined,
    sourceCaptureId: item.capture_id != null ? String(item.capture_id) : '',
    weight: item.importance ?? 3,
    openCount: item.occurrence_count ?? 0,
    hasEditAction: false,
    knowledgeRefCount: 0,
    status: 'confirmed' as const,
    tags: [],
    createdAt: item.created_at ?? '',
    createdAtMs: item.created_at_ms ?? 0,
    captureIds: item.capture_ids ?? [],
    // 后端 KnowledgeEntry 通过 #[serde(rename = "keyTimestamps")] 序列化为驼峰，
    // 这里优先读驼峰键，兼容历史 snake_case 以防回退。
    keyTimestamps: normalizeTimelineKeyTimestamps(item.keyTimestamps ?? item.key_timestamps),
  }
}

function mapBakeMemory(item: any): TimelineItem {
  return {
    id: String(item.id),
    title: item.title,
    url: item.url,
    sourceCaptureId: item.source_capture_id ?? '',
    sourceTimelineId: item.source_timeline_id ?? item.source_knowledge_id ?? undefined,
    details: item.details ?? undefined,
    summary: item.summary,
    weight: item.weight,
    openCount: item.open_count,
    hasEditAction: item.has_edit_action,
    knowledgeRefCount: item.knowledge_ref_count,
    status: item.status,
    suggestedAction: item.suggested_action,
    tags: item.tags ?? [],
    lastVisitedAt: item.last_visited_at,
    createdAt: item.created_at ?? '',
    createdAtMs: item.created_at_ms ?? 0,
    knowledgeMatchScore: item.knowledge_match_score ?? undefined,
    knowledgeMatchLevel: item.knowledge_match_level ?? undefined,
    templateMatchScore: item.template_match_score ?? undefined,
    templateMatchLevel: item.template_match_level ?? undefined,
    sopMatchScore: item.sop_match_score ?? undefined,
    sopMatchLevel: item.sop_match_level ?? undefined,
    captureIds: item.capture_ids ?? [],
    keyTimestamps: normalizeTimelineKeyTimestamps(item.keyTimestamps ?? item.key_timestamps),
  }
}

function mapBakeKnowledge(item: any): BakeKnowledgeItem {
  const details = parseMaybeJsonObject(item.details)
  const sourceCaptureIds = Array.isArray(item.source_capture_ids)
    ? item.source_capture_ids.map(String)
    : Array.isArray(details?.source_capture_ids)
      ? details.source_capture_ids.map(String)
      : []
  const captureId = String(item.capture_id)
  if (captureId && !sourceCaptureIds.includes(captureId)) {
    sourceCaptureIds.unshift(captureId)
  }

  return {
    id: String(item.id),
    captureId,
    sourceCaptureIds,
    sourceTimelineId: item.source_timeline_id != null ? String(item.source_timeline_id) : String(item.id),
    summary: item.summary,
    overview: item.overview,
    details: item.details,
    detailedContent: item.detailed_content,
    entities: item.entities ?? [],
    category: item.category,
    importance: item.importance ?? 0,
    occurrenceCount: item.occurrence_count ?? 0,
    observedAt: item.observed_at,
    status: item.status ?? '',
    reviewStatus: item.review_status ?? item.status ?? '',
    matchScore: item.match_score ?? undefined,
    matchLevel: item.match_level ?? undefined,
    createdAt: item.created_at ?? '',
    createdAtMs: item.created_at_ms ?? 0,
    updatedAt: item.updated_at ?? '',
    updatedAtMs: item.updated_at_ms ?? 0,
  }
}

function parseMaybeJsonObject(raw: unknown): Record<string, any> | null {
  if (!raw || typeof raw !== 'string') return null
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

function mapBakeCapture(item: any): BakeCaptureItem {
  return {
    id: String(item.id),
    ts: item.ts,
    appName: item.app_name,
    appBundleId: item.app_bundle_id,
    winTitle: item.win_title,
    eventType: item.event_type,
    semanticTypeLabel: item.semantic_type_label ?? item.event_type ?? '未知片段',
    rawTypeLabel: item.raw_type_label ?? item.event_type ?? '未知模态',
    axText: item.ax_text,
    axFocusedRole: item.ax_focused_role,
    axFocusedId: item.ax_focused_id,
    ocrText: item.ocr_text,
    inputText: item.input_text,
    audioText: item.audio_text,
    screenshotPath: item.screenshot_path,
    screenshotSource: item.screenshot_source,
    url: item.url,
    webpageTitle: item.webpage_title,
    isSensitive: item.is_sensitive ?? false,
    piiScrubbed: item.pii_scrubbed ?? false,
    bestText: item.best_text,
    summary: item.summary,
    linkedTimelineId: item.linked_timeline_id,
    linkedTimelineSummary: item.linked_timeline_summary,
  }
}

function toTimestampMs(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value)
    if (Number.isFinite(numeric) && numeric > 0) return numeric
    const parsed = Date.parse(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return 0
}

function mapBakeTemplate(item: any): ArticleTemplate {
  const createdAtMs = toTimestampMs(item.created_at_ms ?? item.created_at ?? item.updated_at)
  return {
    id: String(item.id),
    title: item.title,
    docType: item.doc_type,
    status: item.status,
    tags: item.tags ?? [],
    applicableTasks: item.applicable_tasks ?? [],
    sourceMemoryIds: item.source_memory_ids ?? [],
    sourceCaptureIds: item.source_capture_ids ?? [],
    sourceEpisodeIds: item.source_episode_ids ?? [],
    linkedKnowledgeIds: item.linked_knowledge_ids ?? [],
    sections: item.sections ?? [],
    stylePhrases: item.style_phrases ?? [],
    replacementRules: item.replacement_rules ?? [],
    summary: item.summary,
    fullContent: item.full_content,
    sourceUrl: item.source_url,
    promptHint: item.prompt_hint,
    diagramCode: item.diagram_code,
    imageAssets: item.image_assets ?? [],
    usageCount: item.usage_count ?? 0,
    reviewStatus: item.review_status ?? '',
    matchScore: item.match_score ?? undefined,
    matchLevel: item.match_level ?? undefined,
    createdAt: item.created_at ?? '',
    createdAtMs,
    updatedAt: item.updated_at,
    updatedAtMs: toTimestampMs(item.updated_at_ms ?? item.updated_at),
  }
}

function serializeBakeTemplate(template: ArticleTemplate) {
  return {
    title: template.title,
    doc_type: template.docType,
    status: template.status,
    tags: template.tags,
    applicable_tasks: template.applicableTasks,
    source_memory_ids: template.sourceMemoryIds,
    source_capture_ids: template.sourceCaptureIds,
    source_episode_ids: template.sourceEpisodeIds,
    linked_knowledge_ids: template.linkedKnowledgeIds,
    sections: template.sections,
    style_phrases: template.stylePhrases,
    replacement_rules: template.replacementRules,
    summary: template.summary ?? null,
    full_content: template.fullContent ?? null,
    source_url: template.sourceUrl ?? null,
    prompt_hint: template.promptHint,
    diagram_code: template.diagramCode,
    image_assets: template.imageAssets ?? [],
    usage_count: template.usageCount,
    match_score: template.matchScore ?? null,
    match_level: template.matchLevel ?? null,
    review_status: template.reviewStatus ?? null,
  }
}

function mapBakeSop(item: any): SopCandidate {
  const createdAtMs = toTimestampMs(item.created_at_ms ?? item.created_at ?? item.updated_at)
  return {
    id: String(item.id),
    sourceCaptureId: item.source_capture_id ?? '',
    sourceTimelineId: item.source_timeline_id != null ? String(item.source_timeline_id) : String(item.id),
    sourceTitle: item.source_title,
    triggerKeywords: item.trigger_keywords ?? [],
    confidence: item.confidence,
    extractedProblem: item.extracted_problem,
    detailedContent: item.detailed_content,
    steps: item.steps ?? [],
    linkedKnowledgeIds: item.linked_knowledge_ids ?? [],
    linkedKnowledgeSummaries: item.linked_knowledge_summaries ?? [],
    status: item.status,
    createdAt: item.created_at ?? '',
    createdAtMs,
    updatedAt: item.updated_at ?? '',
    updatedAtMs: item.updated_at_ms ?? 0,
  }
}
