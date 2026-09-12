import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CreationPanel from '../components/CreationPanel'
import { sha256Hex } from '../components/creation-selection/creationInlineEdit'
import { useAppStore } from '../store/useAppStore'

const original = '# 第一篇文档\n\n需要修改的原始正文。'
const historyItem = (id: number, content = original) => ({
  id, prompt: `创作记录 ${id}`, root_request: `创作记录 ${id}`, generated_content: content,
  session_id: `session-${id}`, lifecycle_status: 'completed', revision_no: 1,
  references_json: '[]', conversation_json: '[]', agent_trace_json: '[]', evidence_json: '[]',
  created_at: 1, updated_at: 2,
})
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(resolvePromise => { resolve = resolvePromise })
  return { promise, resolve }
}
const eventStream = (sessionId: string) => new Response(`data: ${JSON.stringify({
  schema_version: 'creation.agent.v1', event_id: 'late-event', session_id: sessionId,
  run_id: 'late-run', sequence: 1, timestamp: 1, type: 'run.completed', status: 'completed',
  actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
  summary: '旧请求已完成', data: { document: '# 迟到的旧文档', response: '旧请求结果' },
})}\n\n`, { headers: { 'Content-Type': 'text/event-stream' } })

const committedResponse = async (payload: Record<string, any>) => {
  const selected = payload.selection.selected_markdown as string
  const start = payload.current_document.indexOf(selected)
  const prefix = payload.current_document.slice(0, start)
  const suffix = payload.current_document.slice(start + selected.length)
  const replacement = `${selected}补充后的细节。`
  const content = `${prefix}${replacement}${suffix}`
  return {
    schema_version: 'creation.inline-edit.v1', request_id: payload.request_id,
    status: 'committed', operation_fingerprint: 'isolation-test', content,
    replacement_markdown: replacement, revision_no: 2,
    patch: { base_hash: await sha256Hex(payload.current_document), result_hash: await sha256Hex(content),
      prefix_hash: await sha256Hex(prefix), suffix_hash: await sha256Hex(suffix),
      selection: { selected_markdown_hash: payload.selection.selected_markdown_hash },
      replacement: { replacement_markdown_hash: await sha256Hex(replacement) },
      preserved_untouched: true, operation: 'expand_selection' },
  }
}

const selectAndExpand = async () => {
  const paragraph = await waitFor(() => {
    const node = document.querySelector('.creation-document-content p')
    expect(node).toHaveAttribute('data-md-start')
    return node as HTMLParagraphElement
  })
  const range = document.createRange()
  range.selectNodeContents(paragraph)
  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
  const button = await waitFor(() => {
    fireEvent.pointerUp(paragraph)
    const action = screen.getByRole('button', { name: '扩充' })
    expect(action).toBeEnabled()
    fireEvent.pointerDown(action)
    return action
  })
  fireEvent.click(button)
}

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true, value: () => ({ left: 120, top: 240, width: 160, height: 22, bottom: 262 }),
  })
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('创作异步请求的会话隔离', () => {
  it.each(['start', 'agent', 'save', 'fallback'] as const)('在 %s 请求未完成时切换历史，迟到响应不能覆盖目标文档或继续运行', async pendingStage => {
    const response = deferred<Response>()
    const agentCalls: unknown[] = []
    const historySaves: Record<string, any>[] = []
    let pendingSignal: AbortSignal | null | undefined
    let pendingStarted = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history' && init?.method !== 'POST') {
        return Response.json({ items: [historyItem(2, '# 保留的第二篇文档')], total: 1 })
      }
      if (path === '/api/creation/history/start') {
        if (pendingStage === 'start') { pendingSignal = init?.signal; pendingStarted = true; return response.promise }
        return Response.json({ id: 1, progress_epoch: 1 })
      }
      if (path === '/api/creation/agent/run') {
        agentCalls.push(JSON.parse(String(init?.body)))
        if (pendingStage === 'agent' || pendingStage === 'fallback') { pendingSignal = init?.signal; pendingStarted = true; return response.promise }
        return eventStream('session-1')
      }
      if (path.endsWith('/progress')) return Response.json({}, { status: pendingStage === 'fallback' ? 503 : 200 })
      if (path === '/api/creation/history' && init?.method === 'POST') {
        historySaves.push(JSON.parse(String(init?.body)))
        if (pendingStage === 'save' && !pendingStarted) { pendingStarted = true; return response.promise }
        return Response.json({ id: 1 })
      }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '创建第一篇文档' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))
    await waitFor(() => expect(pendingStarted).toBe(true))
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('创作记录 2'))
    await act(async () => {
      response.resolve(pendingStage === 'start' || pendingStage === 'save' ? Response.json({ id: 1, progress_epoch: 1 }) : eventStream('session-1'))
      await new Promise(resolve => setTimeout(resolve, 20))
    })
    if (pendingStage !== 'save') expect(pendingSignal?.aborted).toBe(true)
    expect(useAppStore.getState().creationDraft).toMatchObject({
      sessionId: 'session-2', generatedContent: '# 保留的第二篇文档', agentEvents: [],
    })
    expect(useAppStore.getState().creationDraft.conversation.every(item => !item.content.includes('旧请求'))).toBe(true)
    expect(agentCalls).toHaveLength(pendingStage === 'start' ? 0 : 1)
    expect(screen.queryByRole('button', { name: '中止' })).not.toBeInTheDocument()
    const capabilityCalls = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes('/inline-edit/capabilities'))
    expect(String(capabilityCalls[capabilityCalls.length - 1]?.[0])).toContain('history_id=2')
    expect(historySaves.every(save => save.session_id !== 'session-2')).toBe(true)
    if (pendingStage === 'fallback') expect(historySaves).toHaveLength(1)
    if (pendingStage === 'save') expect(historySaves.every(save => save.lifecycle_status === 'completed')).toBe(true)
  })

  it.each(['no_change', 'committed'] as const)('局部修改 %s 收到结果前开启新会话，旧结果不添加消息且新会话可以立即提交', async status => {
    const response = deferred<Response>()
    const baseHash = await sha256Hex(original)
    let pending = false
    let result: Record<string, unknown> = { status }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [historyItem(1)], total: 1 })
      if (path === '/api/creation/inline-edit/capabilities') return Response.json({
        schema_version: 'creation.inline-edit.v1', enabled: true, actions: ['expand'],
        max_selection_bytes: 12000, max_custom_prompt_bytes: 2000, supported_node_kinds: ['p', 'h1'],
        history_id: 1, revision_no: 1, base_document_hash: baseHash,
      })
      if (path === '/api/creation/inline-edit/run') {
        if (status === 'committed') result = await committedResponse(JSON.parse(String(init?.body)))
        pending = true; return response.promise
      }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('创作记录 1'))
    await selectAndExpand()
    await waitFor(() => expect(pending).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: '开启新会话' }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '新会话需求' } })
    expect(screen.getByRole('button', { name: '开始创作' })).toBeEnabled()
    await act(async () => { response.resolve(Response.json(result)); await response.promise })
    expect(useAppStore.getState().creationDraft).toMatchObject({
      sessionId: null, generatedContent: '', conversation: [], agentEvents: [], prompt: '新会话需求',
    })
    expect(screen.queryByText('所选内容已经符合要求，未产生修改')).not.toBeInTheDocument()
  })

  it.each(['undo', 'recovery_failure'] as const)('选区提交后的 %s 不串写文档或误报未修改', async scenario => {
    const delayed = deferred<Response>()
    let revision = 1
    let documentContent = original
    let undoStarted = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [historyItem(1, documentContent)], total: 1 })
      if (path === '/api/creation/inline-edit/capabilities') return Response.json({
        schema_version: 'creation.inline-edit.v1', enabled: true, actions: ['expand'],
        max_selection_bytes: 12000, max_custom_prompt_bytes: 2000, supported_node_kinds: ['p', 'h1'],
        history_id: 1, revision_no: revision, base_document_hash: await sha256Hex(documentContent),
      })
      if (path === '/api/creation/inline-edit/run') {
        const result = await committedResponse(JSON.parse(String(init?.body)))
        documentContent = result.content
        revision = 2
        if (scenario === 'recovery_failure') result.patch.prefix_hash = 'damaged-response'
        return Response.json(result)
      }
      if (path === '/api/creation/inline-edit/undo') { undoStarted = true; return delayed.promise }
      return new Response('{}', { status: 503 })
    }))
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('创作记录 1'))
    await selectAndExpand()
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/inline-edit/run'))).toBe(true))
    if (scenario === 'recovery_failure') {
      await waitFor(() => expect(useAppStore.getState().creationDraft.conversation.some(item => item.content.includes('修改已提交'))).toBe(true))
      expect(useAppStore.getState().creationDraft.conversation.some(item => item.content.includes('文档未修改'))).toBe(false)
      return
    }
    fireEvent.click(await screen.findByRole('button', { name: '撤销选区修改' }))
    await waitFor(() => expect(undoStarted).toBe(true))
    expect(screen.getByRole('button', { name: '正在撤销…' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '开启新会话' }))
    await act(async () => {
      delayed.resolve(Response.json({ content: original, patch: { result_hash: await sha256Hex(original) }, revision_no: 3 }))
      await delayed.promise
    })
    expect(useAppStore.getState().creationDraft).toMatchObject({ sessionId: null, generatedContent: '', conversation: [], agentEvents: [] })
  })


  it.each([200, 409])('局部中止返回 %s 时按服务端结果处理，并保留已经提交的结果', async cancelStatus => {
    const delayed = deferred<Response>()
    const baseHash = await sha256Hex(original)
    let runSignal: AbortSignal | null | undefined
    let committed: Awaited<ReturnType<typeof committedResponse>>
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [historyItem(1)], total: 1 })
      if (path === '/api/creation/inline-edit/capabilities') return Response.json({
        schema_version: 'creation.inline-edit.v1', enabled: true, actions: ['expand'],
        max_selection_bytes: 12000, max_custom_prompt_bytes: 2000, supported_node_kinds: ['p', 'h1'],
        history_id: 1, revision_no: 1, base_document_hash: baseHash,
      })
      if (path === '/api/creation/inline-edit/run') {
        committed = await committedResponse(JSON.parse(String(init?.body)))
        runSignal = init?.signal
        return delayed.promise
      }
      if (path === '/api/creation/inline-edit/cancel') return Response.json(
        cancelStatus === 200 ? { status: 'cancelled' } : { message: '修改正在提交，请同步最新文档' },
        { status: cancelStatus },
      )
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('创作记录 1'))
    await selectAndExpand()
    await waitFor(() => expect(runSignal).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '中止' }))
    if (cancelStatus === 200) {
      await waitFor(() => expect(runSignal?.aborted).toBe(true))
      expect(await screen.findByText('本次扩充已取消，文档未修改。')).toBeInTheDocument()
    } else {
      await screen.findByText('修改正在提交，请同步最新文档')
      expect(runSignal?.aborted).toBe(false)
    }
    await act(async () => { delayed.resolve(Response.json(committed!)); await delayed.promise })
    await waitFor(() => expect(screen.queryByRole('button', { name: '中止' })).not.toBeInTheDocument())
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(cancelStatus === 200 ? original : committed!.content)
  })


  it('脑暴失败后重试在首事件前中止，单独保存本次取消并保留旧失败轨迹', async () => {
    const delayed = deferred<Response>()
    const progress: Record<string, any>[] = []
    let requestStarted = false
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm', sessionId: 'session-retry', rootRequest: '继续原来的方案',
      brainstormState: { session_id: 'session-retry', phase: 'ready', revision: 1,
        current_question: null, brief_markdown: '方案已确认', answered_count: 0, depth: 0,
        can_continue_brainstorm: true, open_flags: [], readiness_reason: '', continuation_directions: [],
        invalidated_question_ids: [], history: [], decisions: [] },
      conversation: [{ id: 'original-user', role: 'user', content: '继续原来的方案', createdAt: 1, runIds: ['original-run'] }],
      agentEvents: [{ schema_version: 'creation.agent.v1', event_id: 'old-failure', session_id: 'session-retry',
        run_id: 'original-run', sequence: 1, timestamp: 1, type: 'run.failed', status: 'failed',
        actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' }, summary: '上一轮失败', data: {} }],
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/start') return Response.json({ id: 1, progress_epoch: 2 })
      if (path === '/api/creation/agent/run') { requestStarted = true; return delayed.promise }
      if (path.endsWith('/progress')) { progress.push(JSON.parse(String(init?.body))); return Response.json({}) }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))
    await waitFor(() => expect(requestStarted).toBe(true))
    fireEvent.click(screen.getAllByRole('button', { name: '中止' })[0])
    await act(async () => { delayed.resolve(eventStream('session-retry')); await delayed.promise })
    expect(progress.some(item => item.lifecycle_status === 'cancelled')).toBe(true)
    const events = useAppStore.getState().creationDraft.agentEvents
    expect(events.find(item => item.run_id === 'original-run')?.type).toBe('run.failed')
    expect(events.some(item => item.type === 'run.cancelled' && item.run_id !== 'original-run')).toBe(true)
    expect(useAppStore.getState().creationDraft.conversation.some(item => item.kind === 'user_abort')).toBe(true)
  })

  it('兼容旧版生成接口的迟到内容也不能覆盖切换后的会话', async () => {
    const delayed = deferred<Response>()
    const saves: Record<string, any>[] = []
    let localStarted = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history' && init?.method !== 'POST') return Response.json({ items: [historyItem(2, '# 第二篇文档')], total: 1 })
      if (path === '/api/creation/history/start') return Response.json({ id: 1, progress_epoch: 1 })
      if (path === '/api/creation/agent/run') return new Response('{}', { status: 404 })
      if (path === '/api/creation/generate') { localStarted = true; return delayed.promise }
      if (path.endsWith('/progress')) return Response.json({})
      if (path === '/api/creation/history' && init?.method === 'POST') { saves.push(JSON.parse(String(init?.body))); return Response.json({ id: 1 }) }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), { target: { value: '创建第一篇文档' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))
    await waitFor(() => expect(localStarted).toBe(true))
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('创作记录 2'))
    await act(async () => {
      delayed.resolve(new Response('data: {"content":"迟到的旧版生成内容"}\n', { headers: { 'Content-Type': 'text/event-stream' } }))
      await new Promise(resolve => setTimeout(resolve, 20))
    })
    expect(useAppStore.getState().creationDraft).toMatchObject({ sessionId: 'session-2', generatedContent: '# 第二篇文档' })
    expect(saves.every(item => item.session_id !== 'session-2')).toBe(true)
  })

})
