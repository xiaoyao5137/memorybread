export interface GatewayChatStreamResult {
  content: string
}

export interface GatewayChatStreamError extends Error {
  code?: string
  retryable?: boolean
}

interface GatewayChatStreamEvent {
  type?: string
  text?: string
  answer?: string
  code?: string
  message?: string
  retryable?: boolean
}

const streamError = (
  message: string,
  code: string,
  retryable = false,
): GatewayChatStreamError => {
  const error = new Error(message) as GatewayChatStreamError
  error.code = code
  error.retryable = retryable
  return error
}

export const fetchGatewayChat = async (
  input: RequestInfo | URL,
  init: RequestInit,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> => {
  try {
    return await fetchImpl(input, init)
  } catch (error) {
    // 用户主动中止必须继续走创作取消分支；WebKit 的首包超时和其他网络失败则
    // 统一成品牌中立、可重试的稳定错误，避免历史记录落成不可重试的客户端异常。
    if ((error as { name?: unknown } | null)?.name === 'AbortError') throw error
    const rawMessage = error instanceof Error ? error.message : String(error || '')
    const timedOut = /(?:timed?\s*out|timeout|-1001)/i.test(rawMessage)
    throw streamError(
      timedOut ? '云端模型响应超时，请重试' : '云端模型连接失败，请检查网络后重试',
      timedOut ? 'GATEWAY_REQUEST_TIMEOUT' : 'GATEWAY_NETWORK_UNAVAILABLE',
      true,
    )
  }
}

export const readGatewayChatError = async (
  response: Response,
  fallback: string,
): Promise<GatewayChatStreamError> => {
  let code = `GATEWAY_HTTP_${response.status}`
  let message = fallback
  let retryable = response.status === 429 || response.status >= 500
  try {
    const payload = await response.json() as {
      error?: string | { code?: unknown; message?: unknown; retryable?: unknown }
      message?: unknown
    }
    if (payload.error && typeof payload.error === 'object') {
      const candidateCode = String(payload.error.code || '')
      if (/^[A-Z][A-Z0-9_]{2,63}$/.test(candidateCode)) code = candidateCode
      if (typeof payload.error.message === 'string' && payload.error.message.trim()) {
        message = payload.error.message.trim()
      }
      if (typeof payload.error.retryable === 'boolean') retryable = payload.error.retryable
    } else if (typeof payload.message === 'string' && payload.message.trim()) {
      message = payload.message.trim()
    } else if (typeof payload.error === 'string' && payload.error.trim()) {
      message = payload.error.trim()
    }
  } catch {
    // 反向代理可能返回 HTML 或空正文；不把原始响应暴露到客户端。
  }
  return streamError(message, code, retryable)
}

const defaultRetryable = (code: string) => (
  /(?:UNAVAILABLE|RATE_LIMITED|TIMEOUT|CONNECTION_INTERRUPTED)/.test(code)
)

const nextEventBoundary = (buffer: string): { index: number; length: number } | null => {
  const match = /\r?\n\r?\n/.exec(buffer)
  if (!match || match.index === undefined) return null
  return { index: match.index, length: match[0].length }
}

/**
 * 读取 MemoryBread Gateway 的品牌中立 SSE 契约。
 *
 * 只有收到 done 事件才返回完整结果；即使已经收到部分 delta，连接提前结束也
 * 会失败，避免把未结算或不完整的模型结果交回 Agent 继续执行。
 */
export const consumeGatewayChatStream = async (
  response: Response,
): Promise<GatewayChatStreamResult> => {
  if (!response.body) {
    throw streamError('云端模型流式响应不可读，请重试', 'GATEWAY_STREAM_UNREADABLE', true)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let accumulated = ''
  let completedContent = ''
  let doneReceived = false
  let receivedError: GatewayChatStreamError | null = null

  const consumeBlock = (block: string) => {
    const dataText = block
      .split(/\r?\n/)
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\n')
    if (!dataText || dataText === '[DONE]') return

    let event: GatewayChatStreamEvent
    try {
      event = JSON.parse(dataText) as GatewayChatStreamEvent
    } catch {
      receivedError = streamError(
        '云端模型返回了无法解析的流式事件，请重试',
        'GATEWAY_STREAM_INVALID',
        true,
      )
      return
    }

    if (event.type === 'delta') {
      accumulated += String(event.text || '')
      return
    }
    if (event.type === 'error') {
      const code = String(event.code || 'GATEWAY_STREAM_FAILED')
      receivedError = streamError(
        String(event.message || '云端模型连接中断，请重试'),
        code,
        typeof event.retryable === 'boolean' ? event.retryable : defaultRetryable(code),
      )
      return
    }
    if (event.type === 'done') {
      completedContent = String(event.answer ?? accumulated)
      doneReceived = true
    }
  }

  while (true) {
    let chunk: ReadableStreamReadResult<Uint8Array>
    try {
      chunk = await reader.read()
    } catch (error) {
      // 用户主动中止仍交给外层按取消处理；真实网络 reset/read failure 则
      // 收敛为稳定且可重试的 Gateway 错误，避免落成不可重试的客户端失败。
      if ((error as { name?: unknown } | null)?.name === 'AbortError') throw error
      throw streamError(
        '云端模型流式连接中断，请重试',
        'GATEWAY_STREAM_CONNECTION_INTERRUPTED',
        true,
      )
    }
    const { done, value } = chunk
    if (value) buffer += decoder.decode(value, { stream: !done })
    if (done) buffer += decoder.decode()

    let boundary = nextEventBoundary(buffer)
    while (boundary) {
      consumeBlock(buffer.slice(0, boundary.index))
      buffer = buffer.slice(boundary.index + boundary.length)
      boundary = nextEventBoundary(buffer)
    }

    if (receivedError) {
      await reader.cancel().catch(() => undefined)
      throw receivedError
    }
    if (done) break
  }

  if (buffer.trim()) consumeBlock(buffer)
  if (receivedError) throw receivedError
  if (!doneReceived) {
    throw streamError(
      '云端模型流式响应提前结束，请重试',
      'GATEWAY_STREAM_INCOMPLETE',
      true,
    )
  }
  if (!completedContent.trim()) {
    throw streamError('云端模型没有返回内容', 'GATEWAY_STREAM_EMPTY', true)
  }
  return { content: completedContent }
}
