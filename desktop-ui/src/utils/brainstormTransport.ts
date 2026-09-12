import { fetchWithLocalhostFallback } from '../hooks/useApi'

interface BrainstormFailure extends Error {
  code?: string
  status?: number
  retryable?: boolean
}

const interrupted = () => Object.assign(new Error('脑暴连接中断，已保留当前输入和进度，请重试'), {
  code: 'BRAINSTORM_CONNECTION_INTERRUPTED', retryable: true,
})

const invalidResponse = () => Object.assign(new Error('脑暴返回结果无法读取，已保留当前输入和进度，请重试'), {
  code: 'BRAINSTORM_RESPONSE_INVALID', retryable: false,
})

const assertActive = (signal?: AbortSignal) => {
  if (signal?.aborted) throw new DOMException('脑暴请求已取消', 'AbortError')
}

const failureFromBody = (body: unknown, status?: number): BrainstormFailure => {
  const payload = body && typeof body === 'object' ? body as Record<string, unknown> : {}
  const failure: BrainstormFailure = new Error(
    typeof payload.message === 'string' && payload.message.trim() ? payload.message : '脑暴进度更新失败',
  )
  if (typeof payload.code === 'string') failure.code = payload.code
  if (typeof payload.retryable === 'boolean') failure.retryable = payload.retryable
  failure.status = typeof payload.status === 'number' ? payload.status : status
  return failure
}

/** Only a completed event represents a validated, persisted turn; heartbeats are not results. */
export async function readBrainstormResponse<T>(response: Response, signal?: AbortSignal): Promise<T> {
  assertActive(signal)
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null)
    assertActive(signal)
    throw failureFromBody(body, response.status)
  }
  if (!response.headers.get('content-type')?.toLowerCase().includes('text/event-stream')) {
    // Older Core versions still return the original JSON contract.
    try {
      const state = await response.json() as T
      assertActive(signal)
      return state
    } catch (error) {
      assertActive(signal)
      if (error instanceof SyntaxError) throw invalidResponse()
      throw interrupted()
    }
  }
  if (!response.body) throw interrupted()
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const cancel = () => { void reader.cancel().catch(() => {}) }
  signal?.addEventListener('abort', cancel, { once: true })
  try {
    while (true) {
      assertActive(signal)
      let chunk: ReadableStreamReadResult<Uint8Array>
      try {
        chunk = await reader.read()
      } catch {
        assertActive(signal)
        throw interrupted()
      }
      assertActive(signal)
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        let event = ''
        const data: string[] = []
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith('event:')) event = line.slice(6).trim()
          if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
        }
        if (event !== 'brainstorm.completed' && event !== 'brainstorm.failed') continue
        let payload: Record<string, unknown>
        try {
          payload = JSON.parse(data.join('\n')) as Record<string, unknown>
          if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw invalidResponse()
        } catch {
          throw invalidResponse()
        }
        assertActive(signal)
        if (event === 'brainstorm.failed') throw failureFromBody(payload)
        if (!payload.state || typeof payload.state !== 'object' || Array.isArray(payload.state)) throw invalidResponse()
        return payload.state as T
      }
      // SSE dispatch requires the blank-line boundary. A truncated terminal event is not success.
      if (chunk.done) throw interrupted()
    }
  } finally {
    signal?.removeEventListener('abort', cancel)
    cancel()
    reader.releaseLock()
  }
}

export async function requestBrainstormTurn<T>(url: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<T> {
  assertActive(signal)
  let response: Response
  try {
    response = await fetchWithLocalhostFallback(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      signal,
      body: JSON.stringify(payload),
    })
  } catch (error) {
    assertActive(signal)
    if ((error as { name?: unknown } | null)?.name === 'AbortError') throw error
    throw interrupted()
  }
  return readBrainstormResponse<T>(response, signal)
}
