import { afterEach, describe, expect, it, vi } from 'vitest'
import { readBrainstormResponse, requestBrainstormTurn } from '../utils/brainstormTransport'

const encoder = new TextEncoder()
const state = { session_id: 'brainstorm-test', revision: 0, current_question: { prompt: '期望什么结果？' } }
const completed = `event: brainstorm.completed\ndata: ${JSON.stringify({ state })}\n\n`
const response = (content: string) => new Response(content, { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('脑暴长请求传输', () => {
  it('请求 SSE，兼容旧 Core 的 JSON 成功结果', async () => {
    const fetchMock = vi.fn(async () => Response.json(state))
    vi.stubGlobal('fetch', fetchMock)
    await expect(requestBrainstormTurn('http://127.0.0.1:7070/api/creation/brainstorm/turn', { action: 'start' })).resolves.toEqual(state)
    expect(fetchMock.mock.calls[0]).toEqual([
      'http://127.0.0.1:7070/api/creation/brainstorm/turn',
      expect.objectContaining({ method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' } }),
    ])
  })

  it('UTF-8 字符、CRLF 和事件边界可以跨任意字节块', async () => {
    const bytes = encoder.encode(`: keep-alive\r\nevent: brainstorm.started\r\ndata: {}\r\n\r\n${completed.replace(/\n/g, '\r\n')}`)
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
      controller.close()
    } })
    await expect(readBrainstormResponse(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }))).resolves.toEqual(state)
  })

  it('等待超过 60 秒仍只在明确 completed 后返回', async () => {
    vi.useFakeTimers()
    let controller!: ReadableStreamDefaultController<Uint8Array>
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
    let finished = false
    const result = readBrainstormResponse(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }))
      .then(value => { finished = true; return value })
    controller.enqueue(encoder.encode('event: brainstorm.started\ndata: {}\n\n'))
    for (let index = 0; index < 7; index += 1) {
      await vi.advanceTimersByTimeAsync(10_000)
      controller.enqueue(encoder.encode(': keep-alive\n\n'))
    }
    expect(finished).toBe(false)
    controller.enqueue(encoder.encode(completed))
    await expect(result).resolves.toEqual(state)
  })

  it.each(['', ': keep-alive\n\n', 'event: brainstorm.started\ndata: {}\n\n', completed.trimEnd()])(
    '终态前结束或不完整终态不能成功：%s', async content => {
      await expect(readBrainstormResponse(response(content))).rejects.toMatchObject({
        code: 'BRAINSTORM_CONNECTION_INTERRUPTED', message: '脑暴连接中断，已保留当前输入和进度，请重试', retryable: true,
      })
    },
  )

  it.each(['{', '{}', 'null', '{"state":null}'])('拒绝无效终态数据：%s', async data => {
    await expect(readBrainstormResponse(response(`event: brainstorm.completed\ndata: ${data}\n\n`)))
      .rejects.toMatchObject({ code: 'BRAINSTORM_RESPONSE_INVALID' })
  })

  it.each(['json', 'sse'])('保留 %s 失败的错误码、状态和可重试标记', async format => {
    const failure = { code: 'BRAINSTORM_REVISION_CONFLICT', message: '脑暴内容已更新，请刷新后重试', retryable: false }
    const reply = format === 'json'
      ? Response.json(failure, { status: 409 })
      : response(`event: brainstorm.failed\ndata: ${JSON.stringify({ ...failure, status: 409 })}\n\n`)
    await expect(readBrainstormResponse(reply)).rejects.toMatchObject({ ...failure, status: 409 })
  })

  it('读流失败只显示固定中文，不暴露底层异常', async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.error(new TypeError('Load failed at http://private')) } })
    await expect(readBrainstormResponse(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })))
      .rejects.toMatchObject({ code: 'BRAINSTORM_CONNECTION_INTERRUPTED', message: '脑暴连接中断，已保留当前输入和进度，请重试' })
  })

  it('取消正在等待读取的流，拒绝迟到结果', async () => {
    const abort = new AbortController()
    const cancel = vi.fn()
    const stream = new ReadableStream<Uint8Array>({ cancel })
    const result = readBrainstormResponse(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }), abort.signal)
    const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' })
    abort.abort()
    await rejected
    expect(cancel).toHaveBeenCalledOnce()
    await expect(readBrainstormResponse(Response.json(state), abort.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('首包前网络中断也返回固定中文原因', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Load failed') }))
    await expect(requestBrainstormTurn('http://127.0.0.1:7070/api/creation/brainstorm/turn', {}))
      .rejects.toMatchObject({ code: 'BRAINSTORM_CONNECTION_INTERRUPTED' })
  })
})
