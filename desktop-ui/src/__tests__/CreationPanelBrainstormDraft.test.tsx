import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CreationPanel from '../components/CreationPanel'
import { useAppStore, type CreationBrainstormQuestion, type CreationBrainstormState } from '../store/useAppStore'

const sessionId = 'session-brainstorm-draft'
const rootRequest = '为企业知识库编写落地方案，保留现有用户工作流程。'
const draftDocument = '# 企业知识库方案\n\n已确认保留现有工作流程。\n\n## 待确认\n试点规模。'
const currentQuestion: CreationBrainstormQuestion = {
  id: 'delivery.pilot', dimension: '落地安排', type: 'multi_choice',
  prompt: '本次试点应采用多大的样本规模？', why_now: '规模仍需由用户确认。',
  required: true, allow_custom: true, answer_template: '输入本次试点的具体安排',
  options: [
    { id: 'pilot-1000', label: '试点一千条样本', description: '当前尚未确认的规模。', recommended: true },
    { id: 'pilot-50', label: '试点五十条样本', description: '先验证小规模流程。' },
  ],
}

const makeState = (answered = true): CreationBrainstormState => ({
  session_id: sessionId, phase: 'exploring', revision: answered ? 1 : 0,
  current_question: currentQuestion,
  brief_markdown: answered ? '# 创作简报\n\n## 目标与决策\n- 保留现有用户工作流程' : '# 创作简报\n\n## 待决定\n- 试点规模',
  answered_count: answered ? 1 : 0, depth: answered ? 1 : 0,
  can_continue_brainstorm: false, open_flags: ['试点规模待确认'],
  readiness_reason: '仍有关键问题需要确认。', continuation_directions: [], invalidated_question_ids: [],
  history: answered ? [{
    question: { ...currentQuestion, id: 'outcome.primary', dimension: '目标与决策', prompt: '用户流程应如何调整？' },
    answer: { selected_option_ids: [], custom_text: '保留现有用户工作流程', source: 'user' },
  }] : [],
  decisions: answered ? [{ question_id: 'outcome.primary', dimension: '目标与决策', summary: '保留现有用户工作流程', source: 'user' }] : [],
})

const successResponse = (attempt = 1) => {
  const event = (type: string, sequence: number, data: object = {}) => ({
    schema_version: 'creation.agent.v1', event_id: `draft-${attempt}-${sequence}`,
    session_id: sessionId, run_id: `run-draft-${attempt}`, sequence, timestamp: sequence,
    type, status: type === 'run.started' ? 'running' : 'completed',
    actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
    summary: type === 'run.started' ? '开始生成首版文档' : '首版文档已生成', environment_patch: {}, data,
  })
  return new Response([
    event('run.started', 1),
    event('document.replaced', 2, { content: draftDocument }),
    event('run.completed', 3, { document: draftDocument }),
  ].map(item => `data: ${JSON.stringify(item)}\n\n`).join(''), {
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

const mockRequests = (run: (attempt: number) => Response | Promise<Response> = successResponse) => {
  const agentPayloads: Record<string, any>[] = []
  const brainstormPayloads: Record<string, any>[] = []
  const historyStarts: Record<string, any>[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname
    if (path === '/api/creation/skills') return Response.json([])
    if (path === '/api/creation/skills/match') return Response.json({ matches: [] })
    if (path === '/api/creation/history/start') {
      historyStarts.push(JSON.parse(String(init?.body)))
      return Response.json({ id: 95, progress_epoch: historyStarts.length })
    }
    if (path.endsWith('/progress')) return new Response(null, { status: 204 })
    if (path === '/api/creation/history') return Response.json(init?.method === 'POST' ? { id: 95 } : { items: [], total: 0 })
    if (path === '/api/creation/brainstorm/turn') {
      brainstormPayloads.push(JSON.parse(String(init?.body)))
      return Response.json(makeState())
    }
    if (path === '/api/creation/agent/run') {
      agentPayloads.push(JSON.parse(String(init?.body)))
      return run(agentPayloads.length)
    }
    return new Response('{}', { status: 404 })
  }))
  return { agentPayloads, brainstormPayloads, historyStarts }
}

const seedDraft = (state = makeState(), generatedContent = '') => {
  useAppStore.getState().setCreationDraft({
    creationMode: 'brainstorm', sessionId, rootRequest, brainstormState: state, generatedContent, brainstormPaused: false,
    conversation: [{ id: 'root-instruction', role: 'user', content: rootRequest, createdAt: 0 }],
  })
  return state
}

describe('脑暴过程中先生成一版', () => {
  beforeEach(() => {
    window.localStorage.clear()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })
  afterEach(() => vi.unstubAllGlobals())

  it('正文生成后验收失败仍暂停脑暴，保留原题并允许直接修改正文', async () => {
    const requests = mockRequests(async attempt => {
      if (attempt > 1) return successResponse(attempt)
      const response = await successResponse().text()
      const events = response.trim().split('\n\n').map(line => JSON.parse(line.slice(6)))
      events[2] = { ...events[2], type: 'run.failed', status: 'failed',
        summary: '无法核验最终交付结果',
        data: { error_code: 'CREATION_DELIVERY_UNVERIFIED', retryable: false } }
      return new Response(events.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''), {
        headers: { 'Content-Type': 'text/event-stream' },
      })
    })
    const savedState = seedDraft()
    const view = render(<CreationPanel />)
    fireEvent.click(screen.getByRole('checkbox', { name: /试点一千条样本/ }))
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    expect(await screen.findByText('无法核验最终交付结果')).toBeInTheDocument()
    expect(screen.queryByText('无法核验最终交付结果；已保存已生成内容')).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(draftDocument)
    expect(useAppStore.getState().creationDraft.brainstormPaused).toBe(true)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    expect(screen.queryByRole('group', { name: '最新脑暴操作' })).not.toBeInTheDocument()
    expect(screen.queryByText(currentQuestion.prompt)).not.toBeInTheDocument()
    expect(requests.brainstormPayloads).toHaveLength(0)
    view.unmount()
    render(<CreationPanel />)
    expect(screen.queryByText(currentQuestion.prompt)).not.toBeInTheDocument()
    const restoredInput = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    expect(restoredInput).toBeEnabled()
    await userEvent.setup().type(restoredInput, '检查当前正文中的来源依据。')
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    await waitFor(() => expect(requests.agentPayloads).toHaveLength(2))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.agentPayloads[1].current_document).toBe(draftDocument)
    fireEvent.click(screen.getByRole('button', { name: '继续脑暴' }))
    expect(screen.getByRole('group', { name: '最新脑暴操作' })).toHaveTextContent(currentQuestion.prompt)
    expect(requests.brainstormPayloads).toHaveLength(0)
  })

  it.each(['未选择', '已勾选', '自定义未提交'] as const)('%s当前问题也能生成，且不把未提交答案写进简报', async (selection) => {
    const requests = mockRequests()
    const savedState = seedDraft()
    render(<CreationPanel />)
    if (selection === '已勾选') fireEvent.click(screen.getByRole('checkbox', { name: /试点一千条样本/ }))
    if (selection === '自定义未提交') {
      fireEvent.click(screen.getByRole('checkbox', { name: /自定义答案/ }))
      fireEvent.change(screen.getByPlaceholderText(currentQuestion.answer_template), { target: { value: '未提交草稿：先做三千条样本' } })
    }
    const generate = screen.getByRole('button', { name: '先生成一版' })
    expect(generate).toBeEnabled()
    fireEvent.click(generate)

    await waitFor(() => expect(useAppStore.getState().creationDraft.generatedContent).toBe(draftDocument))
    expect(await screen.findByRole('button', { name: '继续脑暴' })).toBeEnabled()
    expect(screen.queryByRole('group', { name: '最新脑暴操作' })).not.toBeInTheDocument()
    expect(screen.queryByText(currentQuestion.prompt)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '先生成一版' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /试点一千条样本/ })).not.toBeInTheDocument()
    expect(screen.getByText('已按当前回答发起文档生成')).toBeInTheDocument()
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(requests.agentPayloads).toHaveLength(1)
    expect(requests.agentPayloads[0]).toMatchObject({
      creation_mode: 'brainstorm', creation_brief: savedState,
      root_request: rootRequest, current_document: '',
      user_prompt: expect.stringContaining('生成一版文档。'),
    })
    expect(requests.agentPayloads[0].creation_brief).toEqual(savedState)
    expect(requests.agentPayloads[0].user_prompt).toContain('已提交的回答')
    expect(requests.agentPayloads[0].user_prompt).toContain('待确认')
    expect(requests.agentPayloads[0].user_prompt).not.toContain('三千条样本')
    expect(JSON.stringify(requests.agentPayloads[0].conversation)).not.toContain('三千条样本')
    expect(requests.agentPayloads[0].conversation[0]).toEqual({ role: 'user', content: rootRequest })
    expect(requests.historyStarts[0]).toMatchObject({ creation_brief: savedState, brainstorm_revision: 1 })
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    expect(useAppStore.getState().creationDraft.rootRequest).toBe(rootRequest)
  })

  it('还未提交任何回答时可用原始要求生成，问题仍保持待回答', async () => {
    const requests = mockRequests()
    const savedState = seedDraft(makeState(false))
    render(<CreationPanel />)
    expect(screen.getByRole('button', { name: /确认并继续/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.agentPayloads[0].creation_brief).toEqual(savedState)
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(useAppStore.getState().creationDraft.brainstormState?.answered_count).toBe(0)
    expect(useAppStore.getState().creationDraft.brainstormState?.current_question).toEqual(currentQuestion)
  })

  it('生成后仅显式继续才恢复原题，切换页面仍保持暂停且不额外提交脑暴请求', async () => {
    const requests = mockRequests()
    const savedState = seedDraft()
    const view = render(<CreationPanel />)
    fireEvent.click(screen.getByRole('checkbox', { name: /试点一千条样本/ }))
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(useAppStore.getState().creationDraft.brainstormPaused).toBe(true)
    view.unmount()
    render(<CreationPanel />)
    expect(screen.queryByText(currentQuestion.prompt)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /试点一千条样本/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '继续脑暴' }))
    const actions = screen.getByRole('group', { name: '最新脑暴操作' })
    expect(within(actions).getByText(currentQuestion.prompt)).toBeInTheDocument()
    expect(screen.getAllByRole('checkbox', { name: /试点一千条样本/ })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: '先生成一版' })).toHaveLength(1)
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(requests.agentPayloads).toHaveLength(1)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    expect(useAppStore.getState().creationDraft.brainstormPaused).toBe(false)
  })

  it.each(['提交按钮', 'Enter'] as const)('生成后可通过%s提交文档修改要求，完成后仍暂停问题交互', async (submitMethod) => {
    const requests = mockRequests()
    const savedState = seedDraft()
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    await screen.findByRole('button', { name: '继续脑暴' })
    const input = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    expect(input).toBeEnabled()
    await userEvent.setup().type(input, '请补充执行步骤，并保留已确认的流程。')
    expect(input).toHaveValue('请补充执行步骤，并保留已确认的流程。')
    if (submitMethod === 'Enter') await userEvent.setup().type(input, '{Enter}')
    else await userEvent.setup().click(screen.getByRole('button', { name: '提交' }))
    await waitFor(() => expect(requests.agentPayloads).toHaveLength(2))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.agentPayloads[1]).toMatchObject({
      user_prompt: '请补充执行步骤，并保留已确认的流程。',
      current_document: draftDocument, creation_brief: savedState, root_request: rootRequest,
    })
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(screen.queryByText(currentQuestion.prompt)).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormPaused).toBe(true)
  })

  it('恢复旧版已生成记录并同步最新脑暴状态后仍暂停，显式继续才显示最新原题', async () => {
    const requests = mockRequests()
    const defaultFetch = fetch
    const savedState = makeState()
    const latestState: CreationBrainstormState = {
      ...savedState, revision: 2,
      current_question: { ...currentQuestion, id: 'delivery.updated', prompt: '历史会话保留的最新待答问题？' },
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [{
          id: 95, prompt: '先生成一版。', root_request: rootRequest, session_id: sessionId,
          generated_content: draftDocument, creation_mode: 'brainstorm', lifecycle_status: 'completed',
          creation_brief_json: JSON.stringify(savedState),
          conversation_json: JSON.stringify([{ id: 'root-instruction', role: 'user', content: rootRequest, createdAt: 1 }]),
          agent_trace_json: '[]', created_at: 1,
        }], total: 1 })
      }
      if (path === '/api/creation/brainstorm/turn') {
        requests.brainstormPayloads.push(JSON.parse(String(init?.body)))
        return Response.json(latestState)
      }
      return defaultFetch(input, init)
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: /创作记录/ }))
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(rootRequest) }))
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.revision).toBe(2))
    expect(screen.queryByText(latestState.current_question!.prompt)).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: '最新脑暴操作' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /试点一千条样本/ })).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(draftDocument)
    fireEvent.click(screen.getByRole('button', { name: '继续脑暴' }))
    expect(screen.getByRole('group', { name: '最新脑暴操作' })).toHaveTextContent(latestState.current_question!.prompt)
    expect(requests.brainstormPayloads).toHaveLength(1)
    expect(requests.brainstormPayloads[0]).toMatchObject({ action: 'start', session_id: sessionId })
    expect(requests.agentPayloads).toHaveLength(0)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(latestState)
  })

  it('已有文档时携带原正文，要求按已提交脑暴回答更新文档', async () => {
    const requests = mockRequests()
    const existingDocument = '# 原有方案\n\n保留这段正文，补充当前已确认的内容。'
    const savedState = seedDraft(makeState(), existingDocument)
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    await waitFor(() => expect(requests.agentPayloads).toHaveLength(1))
    expect(requests.agentPayloads[0]).toMatchObject({
      current_document: existingDocument, creation_brief: savedState,
      user_prompt: expect.stringContaining('在当前文档基础上更新一版文档'),
    })
    expect(requests.agentPayloads[0].user_prompt).not.toContain('生成一版')
    await waitFor(() => expect(screen.getByRole('button', { name: '继续脑暴' })).toBeEnabled())
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
  })

  it('首版后继续脑暴再成文只要求更新当前文档，仍只使用已提交回答', async () => {
    const requests = mockRequests()
    const savedState = seedDraft()
    const user = userEvent.setup()
    render(<CreationPanel />)
    await user.click(screen.getByRole('button', { name: '先生成一版' }))
    await user.click(await screen.findByRole('button', { name: '继续脑暴' }))
    await user.click(screen.getByRole('checkbox', { name: /试点一千条样本/ }))
    await user.click(screen.getByRole('button', { name: '先生成一版' }))

    await waitFor(() => expect(requests.agentPayloads).toHaveLength(2))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.agentPayloads[0].user_prompt).toContain('生成一版文档。')
    expect(requests.agentPayloads[1]).toMatchObject({
      current_document: draftDocument, creation_brief: savedState, root_request: rootRequest,
      user_prompt: expect.stringContaining('在当前文档基础上更新一版文档。'),
    })
    expect(requests.agentPayloads[1].user_prompt).not.toContain('生成一版')
    expect(requests.agentPayloads[1].user_prompt).toContain('已提交的回答')
    expect(requests.agentPayloads[1].user_prompt).toContain('缺失内容标注待确认，不编造事实')
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    expect(useAppStore.getState().creationDraft.brainstormPaused).toBe(true)
  })

  it('请求期间防止重复生成，失败保留脑暴与已回答历史并允许重试', async () => {
    let finishRun!: (response: Response) => void
    const pendingRun = new Promise<Response>(resolve => { finishRun = resolve })
    const requests = mockRequests(attempt => attempt === 1 ? pendingRun : successResponse(attempt))
    const savedState = seedDraft()
    render(<CreationPanel />)
    const generate = screen.getByRole('button', { name: '先生成一版' })
    fireEvent.click(generate)
    fireEvent.click(generate)
    await waitFor(() => expect(requests.agentPayloads).toHaveLength(1))
    expect(screen.getByText('已按当前回答发起文档生成')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '先生成一版' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /试点一千条样本/ })).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)

    await act(async () => finishRun(Response.json({ message: '本轮生成失败' }, { status: 500 })))
    const retry = await screen.findByRole('button', { name: '先生成一版' })
    expect(retry).toBeEnabled()
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    fireEvent.click(retry)
    await waitFor(() => expect(requests.agentPayloads).toHaveLength(2))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(requests.agentPayloads[1].creation_brief).toEqual(savedState)
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(draftDocument)
  })

  it('原始要求、附件与已回答历史完整传入首次提前生成', async () => {
    const requests = mockRequests()
    useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', sessionId })
    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: rootRequest } })
    const attachment = new File(['original-image-bytes'], '原始需求图.png', { type: 'image/png' })
    fireEvent.paste(input, { clipboardData: { files: [attachment] } })
    await waitFor(() => expect(input).toHaveValue(`${rootRequest} @原始需求图.png `))
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await screen.findByText(currentQuestion.prompt)
    const originalRoot = useAppStore.getState().creationDraft.rootRequest
    expect(originalRoot).toContain(rootRequest)
    expect(originalRoot).toContain('原始需求图.png')
    const savedState = useAppStore.getState().creationDraft.brainstormState
    const callsBeforeGeneration = requests.brainstormPayloads.length
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.brainstormPayloads).toHaveLength(callsBeforeGeneration)
    expect(requests.agentPayloads[0]).toMatchObject({
      root_request: originalRoot, creation_brief: savedState,
      attachments: [{ name: '原始需求图.png', type: 'image/png', data_url: expect.stringMatching(/^data:image\/png;base64,/) }],
    })
    expect(useAppStore.getState().creationDraft.rootRequest).toBe(originalRoot)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
  })

  it('Agent 接口不可用时不降级到丢失脑暴上下文的旧接口，保留正文并允许重试', async () => {
    const requests = mockRequests(attempt => attempt === 1
      ? Response.json({ message: 'Agent 接口不可用' }, { status: 404 })
      : successResponse(attempt))
    const existingDocument = '# 已有草稿\n\n应在失败时保留的原文。'
    const savedState = seedDraft(makeState(), existingDocument)
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    await waitFor(() => expect(requests.agentPayloads).toHaveLength(1))
    const retry = await screen.findByRole('button', { name: '先生成一版' })
    await waitFor(() => expect(retry).toBeEnabled())
    expect(vi.mocked(fetch).mock.calls.some(([input]) => new URL(String(input)).pathname === '/api/creation/generate')).toBe(false)
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(existingDocument)
    expect(useAppStore.getState().creationDraft.rootRequest).toBe(rootRequest)

    fireEvent.click(retry)
    await waitFor(() => expect(useAppStore.getState().creationDraft.generatedContent).toBe(draftDocument))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.agentPayloads).toHaveLength(2)
    expect(requests.agentPayloads[1]).toMatchObject({ creation_brief: savedState, current_document: existingDocument })
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
  })

  it('从创作记录恢复无正文的失败初稿时保留只读脑暴依据，最新问题只有一个重试入口', async () => {
    const requests = mockRequests()
    const defaultFetch = fetch
    const savedState = makeState()
    const latestState: CreationBrainstormState = {
      ...savedState, revision: 2,
      current_question: { ...currentQuestion, id: 'delivery.updated', prompt: '恢复后继续确认试点规模？' },
    }
    const failedEvent = {
      schema_version: 'creation.agent.v1', event_id: 'original-draft-failed',
      session_id: sessionId, run_id: 'original-draft', sequence: 1, timestamp: 3,
      type: 'run.failed', status: 'failed',
      actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
      summary: '初稿生成失败，尚未生成正文', environment_patch: {}, data: {},
    }
    const historyItem = {
      id: 95, prompt: '先生成一版。', root_request: rootRequest, session_id: sessionId,
      generated_content: '', creation_mode: 'brainstorm', lifecycle_status: 'failed',
      creation_brief_json: JSON.stringify(savedState),
      conversation_json: JSON.stringify([
        { id: 'root-instruction', role: 'user', content: rootRequest, createdAt: 1 },
        { id: 'original-draft-instruction', role: 'user', content: '先生成一版。', createdAt: 2, runId: 'original-draft' },
      ]),
      agent_trace_json: JSON.stringify([failedEvent]), created_at: 1,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [historyItem], total: 1 })
      }
      if (path === '/api/creation/brainstorm/turn') {
        requests.brainstormPayloads.push(JSON.parse(String(init?.body)))
        return Response.json(latestState)
      }
      return defaultFetch(input, init)
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: /创作记录/ }))
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(rootRequest) }))
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.revision).toBe(2))

    const latestActions = screen.getByRole('group', { name: '最新脑暴操作' })
    const snapshot = screen.getByText('已按当前回答发起文档生成').closest('.creation-brainstorm-card') as HTMLElement
    expect(snapshot).toHaveTextContent('本版文档的脑暴依据')
    expect(within(snapshot).queryByRole('button', { name: '先生成一版' })).not.toBeInTheDocument()
    expect(within(snapshot).queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.getAllByText(latestState.current_question!.prompt)).toHaveLength(1)
    expect(within(latestActions).getByText(latestState.current_question!.prompt)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '先生成一版' })).toHaveLength(1)
    expect(requests.brainstormPayloads).toHaveLength(1)
    expect(requests.brainstormPayloads[0]).toMatchObject({ action: 'start', session_id: sessionId })
    expect(useAppStore.getState().creationDraft.generatedContent).toBe('')

    const retry = within(latestActions).getByRole('button', { name: '先生成一版' })
    expect(retry).toBeEnabled()
    fireEvent.click(retry)
    await waitFor(() => expect(useAppStore.getState().creationDraft.generatedContent).toBe(draftDocument))
    await screen.findByRole('button', { name: '继续脑暴' })
    expect(requests.agentPayloads).toHaveLength(1)
    expect(requests.agentPayloads[0]).toMatchObject({ creation_brief: latestState, current_document: '', root_request: rootRequest })
    expect(requests.brainstormPayloads).toHaveLength(1)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(latestState)
  })

  it('简报编辑未保存时提示先保存，保留编辑且不发起生成', async () => {
    const requests = mockRequests()
    const savedState = seedDraft()
    useAppStore.getState().setCreationDraft({
      briefEditDraft: { sessionId, values: { 'outcome.primary': '尚未保存的目标修订' } },
    })
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '先生成一版' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('请先保存创作简报修改')
    expect(screen.getByLabelText('简报：目标与决策')).toHaveValue('尚未保存的目标修订')
    expect(requests.agentPayloads).toHaveLength(0)
    expect(requests.brainstormPayloads).toHaveLength(0)
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(savedState)
    expect(useAppStore.getState().creationDraft.briefEditDraft?.values).toEqual({ 'outcome.primary': '尚未保存的目标修订' })
  })

  it.each(['abandoned', 'session_end'] as const)('会话通过%s终止后隐藏提前生成入口', async (termination) => {
    const requests = mockRequests()
    seedDraft()
    if (termination === 'abandoned') {
      useAppStore.getState().setCreationDraft({ brainstormState: { ...makeState(), phase: 'abandoned' } })
    } else {
      useAppStore.getState().setCreationDraft({ conversation: [
        ...useAppStore.getState().creationDraft.conversation,
        { id: 'session-ended', role: 'user', content: '终止了当前会话', kind: 'session_end', createdAt: 2 },
      ] })
    }
    await act(async () => { render(<CreationPanel />) })
    expect(screen.queryByRole('button', { name: '先生成一版' })).not.toBeInTheDocument()
    expect(requests.agentPayloads).toHaveLength(0)
    expect(requests.brainstormPayloads).toHaveLength(0)
  })
})
