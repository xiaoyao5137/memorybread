import { describe, expect, it, vi } from 'vitest'
import {
  consumeGatewayChatStream,
  fetchGatewayChat,
  readGatewayChatError,
  type GatewayChatStreamError,
} from '../utils/gatewayChatStream'

const chunkedResponse = (chunks: string[]) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      chunks.forEach(chunk => controller.enqueue(encoder.encode(chunk)))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('Gateway 品牌模型流式响应', () => {
  it('跨网络分片解析 delta，并以 done 中的完整结果为准', async () => {
    const response = chunkedResponse([
      ': keep-alive\r\n\r\ndata: {"type":"del',
      'ta","text":"方案"}\r\n\r\ndata: {"type":"delta","text":"结论"}\r\n',
      '\r\ndata: {"type":"done","answer":"方案结论"}\r\n\r\n',
    ])

    await expect(consumeGatewayChatStream(response)).resolves.toEqual({
      content: '方案结论',
    })
  })

  it('将稳定错误码和可重试属性保留给 Agent 失败轨迹', async () => {
    const response = chunkedResponse([
      'data: {"type":"error","code":"MODEL_SERVICE_UNAVAILABLE","message":"云端模型连接中断，请重试"}\n\n',
    ])

    const error = await consumeGatewayChatStream(response).catch(value => value) as GatewayChatStreamError
    expect(error.message).toBe('云端模型连接中断，请重试')
    expect(error.code).toBe('MODEL_SERVICE_UNAVAILABLE')
    expect(error.retryable).toBe(true)
  })

  it('遵守 Gateway 明确返回的结算重试语义', async () => {
    const response = chunkedResponse([
      'data: {"type":"error","code":"SETTLEMENT_FAILED","message":"本次咨询结算暂时未完成，请稍后重试","retryable":true}\n\n',
    ])

    const error = await consumeGatewayChatStream(response).catch(value => value) as GatewayChatStreamError
    expect(error.code).toBe('SETTLEMENT_FAILED')
    expect(error.retryable).toBe(true)
  })

  it('收到部分内容但缺少 done 时拒绝把不完整结果交回 Agent', async () => {
    const response = chunkedResponse([
      'data: {"type":"delta","text":"未完成内容"}\n\n',
    ])

    const error = await consumeGatewayChatStream(response).catch(value => value) as GatewayChatStreamError
    expect(error.code).toBe('GATEWAY_STREAM_INCOMPLETE')
    expect(error.retryable).toBe(true)
  })

  it('网络 reset 导致 reader.read 拒绝时收敛为可重试连接错误', async () => {
    const encoder = new TextEncoder()
    let pullCount = 0
    const response = new Response(new ReadableStream({
      pull(controller) {
        if (pullCount === 0) {
          pullCount += 1
          controller.enqueue(encoder.encode('data: {"type":"delta","text":"部分内容"}\n\n'))
          return
        }
        controller.error(new TypeError('socket reset by peer'))
      },
    }), { headers: { 'Content-Type': 'text/event-stream' } })

    const error = await consumeGatewayChatStream(response).catch(value => value) as GatewayChatStreamError
    expect(error.message).toBe('云端模型流式连接中断，请重试')
    expect(error.message).not.toContain('socket reset')
    expect(error.code).toBe('GATEWAY_STREAM_CONNECTION_INTERRUPTED')
    expect(error.retryable).toBe(true)
  })

  it('用户主动中止读取时保留 AbortError 语义', async () => {
    const response = new Response(new ReadableStream({
      start(controller) {
        controller.error(new DOMException('aborted', 'AbortError'))
      },
    }))

    const error = await consumeGatewayChatStream(response).catch(value => value) as Error
    expect(error.name).toBe('AbortError')
  })

  it('非流式 HTTP 失败也保留 Gateway 的稳定错误契约', async () => {
    const response = Response.json({
      error: {
        code: 'MODEL_SERVICE_UNAVAILABLE',
        message: '云端模型服务暂时不可用，请稍后重试',
        retryable: true,
      },
    }, { status: 503 })

    const error = await readGatewayChatError(response, '生成失败，请稍后重试')
    expect(error.message).toBe('云端模型服务暂时不可用，请稍后重试')
    expect(error.code).toBe('MODEL_SERVICE_UNAVAILABLE')
    expect(error.retryable).toBe(true)
  })

  it('WebKit 首包失败收敛为可重试网络错误且不暴露底层信息', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Load failed')) as typeof fetch

    const error = await fetchGatewayChat(
      'https://gateway.example.test/v1/gateway/chat',
      { method: 'POST' },
      fetchImpl,
    ).catch(value => value) as GatewayChatStreamError

    expect(error.message).toBe('云端模型连接失败，请检查网络后重试')
    expect(error.message).not.toContain('Load failed')
    expect(error.code).toBe('GATEWAY_NETWORK_UNAVAILABLE')
    expect(error.retryable).toBe(true)
  })

  it('首包超时使用稳定超时码，用户中止仍保留 AbortError', async () => {
    const timeoutFetch = vi.fn().mockRejectedValue(new TypeError('request timed out')) as typeof fetch
    const timeout = await fetchGatewayChat(
      'https://gateway.example.test/v1/gateway/chat',
      { method: 'POST' },
      timeoutFetch,
    ).catch(value => value) as GatewayChatStreamError
    expect(timeout.code).toBe('GATEWAY_REQUEST_TIMEOUT')
    expect(timeout.retryable).toBe(true)

    const abort = new DOMException('aborted', 'AbortError')
    const abortFetch = vi.fn().mockRejectedValue(abort) as typeof fetch
    await expect(fetchGatewayChat(
      'https://gateway.example.test/v1/gateway/chat',
      { method: 'POST' },
      abortFetch,
    )).rejects.toBe(abort)
  })
})
