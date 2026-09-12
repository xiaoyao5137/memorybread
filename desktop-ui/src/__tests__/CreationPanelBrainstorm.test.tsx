import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'

const question = (id: string, dimension: string, prompt: string, whyNow = '这个决定会影响后续范围和内容深度。') => ({
  id,
  dimension,
  type: 'single_choice' as const,
  prompt,
  why_now: whyNow,
  required: true,
  allow_custom: true,
  options: [
    { id: 'recommended', label: '推荐方向', description: '先形成小范围闭环。', recommended: true },
    { id: 'alternative', label: '备选方向', description: '覆盖更广，但交付风险更高。' },
  ],
  answer_template: '补充你的实际约束。',
})

const brainstormState = (revision: number, currentQuestion: ReturnType<typeof question> | null, phase = 'exploring') => ({
  session_id: 'session-brainstorm-ui',
  phase,
  revision,
  current_question: currentQuestion,
  brief_markdown: revision
    ? '# 创作简报\n\n## 目标与决策\n- **已确认：** 推荐方向'
    : '# 创作简报\n\n## 待决定\n- 创作目标',
  answered_count: revision,
  depth: revision,
  can_continue_brainstorm: phase === 'ready',
  open_flags: phase === 'ready' ? ['成功标准待补充'] : ['创作目标'],
  readiness_reason: phase === 'ready' ? '所有横向维度（10 项）均已覆盖且无高影响歧义；剩余待确认事项为低层级的具体参数与假设验证，不影响主方向收敛' : '仍有关键分支需要确认。',
  continuation_directions: phase === 'ready' ? [
    {
      id: 'challenge_assumptions',
      label: '挑战关键假设',
      description: '检查当前方向最可能失败的前提。',
      recommended: true,
    },
    {
      id: 'delivery_path',
      label: '补强落地路径',
      description: '继续细化交付节奏和责任边界。',
      recommended: false,
    },
  ] : [],
  invalidated_question_ids: [],
  history: revision ? [{
    question: question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？'),
    answer: {
      selected_option_ids: ['recommended'],
      custom_text: '',
      source: 'user',
    },
  }] : [],
  decisions: revision ? [{
    question_id: 'outcome.primary',
    dimension: '目标与决策',
    summary: '推荐方向',
    source: 'user',
  }] : [],
})

const installedBrainstormSkill = {
  id: 67,
  client_skill_key: 'microservice-solution',
  cloud_skill_id: null,
  source_kind: 'manual',
  source_id: 'microservice-solution',
  title: '微服务模块技术方案文档',
  summary: '覆盖业务流程、数据所有权、组织保障和上线验收。',
  category_id: null,
  skill_description: {
    purpose: '形成可评审、可落地的完整技术方案。',
    document_types: ['微服务模块技术方案'],
    problems: ['只讨论技术实现，缺少业务与组织闭环'],
    domains: ['微服务架构'],
    deliverables: ['业务、数据、技术与落地方案'],
  },
  execution_steps: [
    {
      id: 'business-scope',
      title: '需求背景与范围',
      objective: '明确业务目标、角色、上下游、范围和成功指标。',
      output: '业务范围与假设清单',
      agents: [],
      skills: [],
      tools: [],
    },
    {
      id: 'architecture',
      title: '总体方案与系统边界',
      objective: '确认能力分层、组件职责、上下游和关键架构取舍。',
      output: '总体架构方案',
      agents: [],
      skills: [],
      tools: [],
    },
    {
      id: 'feature-mechanism',
      title: '核心功能机制',
      objective: '确认核心能力的输入、处理阶段、可控参数和失败兜底。',
      output: '核心机制方案',
      agents: [],
      skills: [],
      tools: [],
    },
  ],
  common_titles: [],
  title_style: '',
  text_style: '先业务后技术。',
  diagram_style: '',
  writing_guidelines: ['不得虚构指标。'],
  distinctive_sections: [],
  section_headings: {
    common_titles: '标题设计风格',
    title_style: '标题设计风格',
    text_style: '行文设计思路',
    diagram_style: '图片生成方式',
    writing_guidelines: '话术表达风格',
  },
  field_examples: {
    common_titles: [],
    title_style: [],
    text_style: [],
    diagram_style: [],
    writing_guidelines: [],
  },
  example_document: '',
  package_files: [],
  status: 'saved',
  installed: true,
  published: false,
  created_at: 1,
  updated_at: 2,
}

const sse = (events: object[]) => new Response(
  events.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
  { headers: { 'Content-Type': 'text/event-stream' } },
)

describe('创作页脑暴模式', () => {
  beforeEach(() => {
    window.localStorage.clear()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  it.each(['direct', 'brainstorm'] as const)('%s 追加指令提交即清空输入并显示用户消息，失败后仍保留对话', async (creationMode) => {
    let finishRun!: (response: Response) => void
    const pendingRun = new Promise<Response>(resolve => { finishRun = resolve })
    const instruction = '去除“实施步骤与资源规划”章节'
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname.endsWith('/progress')) return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/skills/match') return Response.json({ matches: [] })
      if (url.pathname === '/api/creation/agent/run') {
        expect(JSON.parse(String(init?.body)).user_prompt).toBe(instruction)
        return pendingRun
      }
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    useAppStore.getState().setCreationDraft({
      creationMode,
      sessionId: 'session-brainstorm-ui',
      rootRequest: '设计企业知识库方案',
      generatedContent: '# 企业知识库方案\n\n待修改正文',
      brainstormState: creationMode === 'brainstorm' ? brainstormState(1, null, 'ready') : null,
      conversation: [{ id: 'root', role: 'user', content: '设计企业知识库方案', createdAt: 0 }],
    })
    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    fireEvent.change(input, { target: { value: instruction } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(input).toHaveValue('')
    expect(within(screen.getByRole('region', { name: '创作对话' })).getByText(instruction)).toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.conversation.filter(message => message.content === instruction)).toHaveLength(1)
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/api/creation/agent/run'))).toBe(true))
    expect(input).toHaveValue('')

    finishRun(Response.json({ message: '本轮生成失败' }, { status: 500 }))
    await waitFor(() => expect(input).toBeEnabled())
    expect(input).toHaveValue('')
    expect(useAppStore.getState().creationDraft.conversation.filter(message => message.content === instruction)).toHaveLength(1)
    expect(screen.getByText(instruction)).toBeInTheDocument()
  })

  it.each([false, true])('失败后再次生成追加到同一执行过程（历史显式关联：%s）', async (linked) => {
    const event = (runId: string, sequence: number, type: string, summary: string) => ({
      schema_version: 'creation.agent.v1' as const,
      event_id: `${runId}-${sequence}`,
      session_id: 'session-brainstorm-ui',
      run_id: runId,
      sequence,
      timestamp: sequence,
      type,
      status: type === 'run.failed' ? 'failed' : 'running',
      actor: { kind: 'agent' as const, id: 'creation_main_agent', name: '创作 Agent' },
      summary,
      environment_patch: {},
      data: {},
    })
    let attempts = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname.endsWith('/progress')) return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/skills/match') return Response.json({ matches: [] })
      if (url.pathname === '/api/creation/agent/run') {
        attempts += 1
        return sse([
          event(`retry-${attempts}`, 1, 'run.started', `第 ${attempts} 次重试已开始`),
          event(`retry-${attempts}`, 2, 'tool.completed', `第 ${attempts} 次重试检索记录`),
          event(`retry-${attempts}`, 3, 'run.failed', `第 ${attempts} 次重试已失败`),
        ])
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(1, null, 'ready'),
      conversation: [{
        id: 'root-instruction', role: 'user', content: '设计企业知识库方案', createdAt: 0,
        ...(linked ? { runIds: ['original'] } : {}),
      }],
      agentEvents: [
        event('original', 1, 'run.started', '原始执行已开始'),
        event('original', 2, 'tool.completed', '原始执行检索记录'),
        event('original', 3, 'run.failed', '原始执行已失败'),
      ],
    })
    const view = render(<CreationPanel />)
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      fireEvent.click(screen.getByRole('button', { name: /开始创作/ }))
      await waitFor(() => expect(screen.getByRole('button', { name: /开始创作/ })).toBeEnabled())
      expect(attempts).toBe(attempt)
      expect(screen.getAllByLabelText('Agent 执行情况')).toHaveLength(1)
      const runIds = useAppStore.getState().creationDraft.conversation[0].runIds
      expect(runIds).toEqual(['original', ...Array.from({ length: attempt }, (_, index) => `retry-${index + 1}`)])
    }
    const trace = screen.getByLabelText('Agent 执行情况')
    fireEvent.click(within(trace).getByRole('button', { name: '展开全部' }))
    const original = within(trace).getByText('原始执行检索记录')
    const firstRetry = within(trace).getByText('第 1 次重试检索记录')
    const secondRetry = within(trace).getByText('第 2 次重试检索记录')
    expect(original.compareDocumentPosition(firstRetry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(firstRetry.compareDocumentPosition(secondRetry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    view.unmount()
    render(<CreationPanel />)
    expect(screen.getAllByLabelText('Agent 执行情况')).toHaveLength(1)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it.each(['direct', 'brainstorm'] as const)('仅脑暴模式在右下角显示继续脑暴：%s', async (creationMode) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    useAppStore.getState().setCreationDraft({
      creationMode,
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(1, null, 'ready'),
    })
    render(<CreationPanel />)
    const actions = screen.getByRole('group', { name: '创作操作' })
    if (creationMode === 'brainstorm') {
      const continueButton = within(actions).getByRole('button', { name: '继续脑暴' })
      expect(continueButton.nextElementSibling).toBe(within(actions).getByRole('button', { name: '开始创作' }))
      expect(screen.getAllByRole('button', { name: '开始创作' })).toHaveLength(1)
    } else {
      expect(screen.queryByRole('button', { name: '继续脑暴' })).not.toBeInTheDocument()
    }
  })

  it('展示后端校验的记忆依据与未命中提示', async () => {
    const grounded = question('memory', '流程', '接下来如何改善审核？', '本轮已检索历史记忆。')
    grounded.options[0].description = '建议保留审核。记忆依据：《项目决策》 · 2026-09-05 · document:1「先降低人工修改成本」'
    grounded.options[1].description = '待验证推演：暂无直接记忆依据。'
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') return Response.json(brainstormState(0, grounded))
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), { target: { value: '设计方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    expect(await screen.findByText(/记忆依据：《项目决策》/)).toBeInTheDocument()
    expect(screen.getByText('本轮已检索历史记忆。')).toBeInTheDocument()
    expect(screen.getByText('待验证推演：暂无直接记忆依据。')).toBeInTheDocument()
  })

  it('脑暴 SSE 启动截断展示具体原因，保留输入并允许原会话成功重试', async () => {
    const payloads: Record<string, unknown>[] = []
    const reason = '脑暴问题生成达到长度上限，已保留当前输入，请缩小本轮讨论范围后重试'
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        expect(new Headers(init?.headers).get('Accept')).toBe('text/event-stream')
        payloads.push(JSON.parse(String(init?.body)))
        return new Response(payloads.length === 1
          ? `event: brainstorm.failed\ndata: ${JSON.stringify({ code: 'BRAINSTORM_MODEL_OUTPUT_TRUNCATED', message: reason, retryable: false, status: 502 })}\n\n`
          : `event: brainstorm.completed\ndata: ${JSON.stringify({ state: brainstormState(0, question('goal', '目标', '这次希望推动什么结果？')) })}\n\n`,
        { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '设计方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    expect(await screen.findByText(reason)).toBeInTheDocument()
    expect(screen.queryByText('脑暴启动失败，请重试')).not.toBeInTheDocument()
    expect(input).toHaveValue('设计方案')
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    expect(await screen.findByText('这次希望推动什么结果？')).toBeInTheDocument()
    expect(screen.queryByText(reason)).not.toBeInTheDocument()
    expect(payloads).toHaveLength(2)
    expect(payloads[1]).toEqual(payloads[0])
    expect(useAppStore.getState().creationDraft.conversation.filter(item => item.role === 'user')).toHaveLength(1)
    expect(input).toHaveValue('')
  })

  it('首题只收到 started 和保活时继续等待，断流后保留原输入并提示连接中断', async () => {
    let stream!: ReadableStreamDefaultController<Uint8Array>
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/start') return Response.json({ id: 1 })
      if (path === '/api/creation/brainstorm/turn') return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          stream = controller
          controller.enqueue(new TextEncoder().encode('event: brainstorm.started\ndata: {}\n\n: keep-alive\n\n'))
        },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '保留这条创作需求' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await waitFor(() => expect(stream).toBeDefined())
    expect(screen.getByRole('status', { name: '正在准备第一条脑暴问题' })).toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState).toBeNull()
    await act(async () => { stream.close() })
    expect(await screen.findByText('脑暴连接中断，已保留当前输入和进度，请重试')).toBeInTheDocument()
    expect(input).toHaveValue('保留这条创作需求')
    expect(useAppStore.getState().creationDraft.conversation.filter(item => item.role === 'user')).toHaveLength(1)
  })

  it('该方向不重要提交独立的排除动作', async () => {
    const payloads: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const payload = JSON.parse(String(init?.body))
        payloads.push(payload)
        return Response.json(brainstormState(payload.action === 'exclude' ? 1 : 0, question(payload.action === 'exclude' ? 'next' : 'cost', '范围', payload.action === 'exclude' ? '下一方向？' : '是否展开预算？')))
      }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), { target: { value: '设计方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    fireEvent.click(await screen.findByRole('button', { name: '该方向不重要' }))
    await screen.findByText('下一方向？')
    expect(payloads[payloads.length - 1]).toMatchObject({ action: 'exclude', revision: 0, question_id: 'cost' })
    expect(payloads[payloads.length - 1]).not.toHaveProperty('answer')
  })

  it('第一条选项返回前立即展示思考状态，并在问题就绪后收起', async () => {
    let brainstormRequestCount = 0
    let resolveBrainstormTurn: (response: Response) => void = () => undefined
    const pendingBrainstormTurn = new Promise<Response>((resolve) => {
      resolveBrainstormTurn = resolve
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        brainstormRequestCount += 1
        return pendingBrainstormTurn
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, {
      target: { value: '设计数据治理平台建设方案' },
    })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))

    const thinkingState = await screen.findByRole('status', { name: '正在准备第一条脑暴问题' })
    expect(thinkingState).toHaveTextContent('正在生成第一条脑暴问题')
    expect(thinkingState).toHaveTextContent('正在等待模型返回第一条问题')
    expect(thinkingState).toHaveTextContent('已等待 0 秒')
    expect(screen.getByRole('button', { name: '正在梳理' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '开启新会话' })).toBeDisabled()
    expect(screen.getByLabelText('Agent 正在思考')).toBeInTheDocument()
    expect(input).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    expect(brainstormRequestCount).toBe(1)

    const internalOrchestrationHint = '根据 next_question_goal 要求，必须优先明确使用者角色、触发时机和业务流程，这是构建后续技术方案的基础前提。'
    resolveBrainstormTurn(Response.json(
      brainstormState(0, question(
        'outcome.primary',
        '目标与决策',
        '这次创作最需要推动什么结果？',
        internalOrchestrationHint,
      )),
    ))

    expect(await screen.findByText('这次创作最需要推动什么结果？')).toBeInTheDocument()
    expect(screen.queryByText(internalOrchestrationHint)).not.toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByRole('status', { name: '正在准备第一条脑暴问题' })).not.toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: '请回答上方问题' })).toBeDisabled()
  })

  it('首题等待显示真实请求阶段和累计时长，失败后停止等待并允许重试', async () => {
    let finishMatch!: (response: Response) => void
    let finishHistory!: (response: Response) => void
    let finishQuestion!: (response: Response) => void
    const match = new Promise<Response>(resolve => { finishMatch = resolve })
    const history = new Promise<Response>(resolve => { finishHistory = resolve })
    const firstQuestion = new Promise<Response>(resolve => { finishQuestion = resolve })
    const requestSignals: (AbortSignal | null | undefined)[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([installedBrainstormSkill])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/skills/match') { requestSignals.push(init?.signal); return match }
      if (path === '/api/creation/history/start') { requestSignals.push(init?.signal); return history }
      if (path === '/api/creation/brainstorm/turn') { requestSignals.push(init?.signal); return firstQuestion }
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '@微服务' } })
    await screen.findByRole('option', { name: /微服务模块技术方案文档/ })
    fireEvent.change(input, { target: { value: '设计广告诊断接口方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    const status = await screen.findByRole('status', { name: '正在准备第一条脑暴问题' })
    expect(status).toHaveTextContent('正在匹配创作技能')
    expect(status).toHaveTextContent('已等待 0 秒')
    const now = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(now + 31000)
    await waitFor(() => expect(status).toHaveTextContent('已等待 31 秒'), { timeout: 2500 })
    expect(status).toHaveTextContent('本次等待较久')
    await act(async () => { finishMatch(Response.json({ skill_ids: [67], source: 'model' })) })
    expect(status).toHaveTextContent('正在保存创作会话')
    expect(status).toHaveTextContent('已等待 31 秒')
    await act(async () => { finishHistory(Response.json({ id: 1 })) })
    expect(status).toHaveTextContent('正在生成第一条脑暴问题')
    expect(requestSignals).toHaveLength(3)
    expect(requestSignals[0]).toBeInstanceOf(AbortSignal)
    expect(requestSignals.every(signal => signal === requestSignals[0])).toBe(true)
    await act(async () => { finishQuestion(Response.json({ message: '脑暴服务暂时不可用' }, { status: 503 })) })
    expect(await screen.findByRole('alert')).toHaveTextContent('脑暴服务暂时不可用')
    expect(screen.queryByRole('status', { name: '正在准备第一条脑暴问题' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始梳理' })).toBeEnabled()
  })

  it.each(['match', 'history', 'question'] as const)('在 %s 阶段终止后，迟到响应不能推进旧请求或清除新会话等待状态', async (cancelStage) => {
    let finishOldRequest!: (response: Response) => void
    let oldSignal: AbortSignal | null | undefined
    const pendingOldRequest = new Promise<Response>(resolve => { finishOldRequest = resolve })
    const requests: string[] = []
    let startingNewSession = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([installedBrainstormSkill])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      const stage = path === '/api/creation/skills/match' ? 'match'
        : path === '/api/creation/history/start' ? 'history'
          : path === '/api/creation/brainstorm/turn' ? 'question' : null
      if (stage) {
        requests.push(`${startingNewSession ? 'new' : 'old'}:${stage}`)
        if (startingNewSession) return new Promise<Response>(() => undefined)
        if (stage === cancelStage) { oldSignal = init?.signal; return pendingOldRequest }
        if (stage === 'match') return Response.json({ skill_ids: [67], source: 'model' })
        if (stage === 'history') return Response.json({ id: 1 })
      }
      if (path.endsWith('/progress')) return new Response(null, { status: 204 })
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '@微服务' } })
    await screen.findByRole('option', { name: /微服务模块技术方案文档/ })
    fireEvent.change(input, { target: { value: '旧创作要求' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await waitFor(() => expect(requests).toContain(`old:${cancelStage}`))
    expect(oldSignal?.aborted).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '终止当前会话' }))
    expect(oldSignal?.aborted).toBe(true)
    expect(screen.queryByRole('status', { name: '正在准备第一条脑暴问题' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '开启新会话' }))
    startingNewSession = true
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), { target: { value: '新的创作要求' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await waitFor(() => expect(requests).toContain('new:match'))
    const requestCount = requests.length
    const newSessionId = useAppStore.getState().creationDraft.sessionId
    // 故意模拟忽略 AbortSignal 的迟到成功响应，保证隔离不只依赖 fetch 抛错。
    await act(async () => {
      finishOldRequest(Response.json(cancelStage === 'match'
        ? { skill_ids: [67], source: 'model' }
        : cancelStage === 'history' ? { id: 999 }
          : brainstormState(0, question('old.question', '旧方向', '不应出现的旧问题'))))
    })
    expect(requests).toHaveLength(requestCount)
    expect(useAppStore.getState().creationDraft).toMatchObject({ sessionId: newSessionId, rootRequest: '新的创作要求', brainstormState: null })
    expect(screen.getByRole('status', { name: '正在准备第一条脑暴问题' })).toHaveTextContent('正在匹配创作技能')
    expect(screen.getByRole('button', { name: '正在梳理' })).toBeDisabled()
    expect(screen.queryByText('不应出现的旧问题')).not.toBeInTheDocument()
  })

  it.each(['completed', 'cancelled'] as const)('恢复未保存首题的会话时显示计时，%s 后正确清理且隔离迟到响应', async (outcome) => {
    let finishRestore!: (response: Response) => void
    let restoreSignal: AbortSignal | null | undefined
    const pendingRestore = new Promise<Response>(resolve => { finishRestore = resolve })
    let brainstormRequests = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/41') return Response.json({
        id: 41,
        prompt: '恢复原来的创作目标',
        root_request: '恢复原来的创作目标',
        session_id: 'session-brainstorm-ui',
        generated_content: '',
        creation_mode: 'brainstorm',
        creation_brief_json: null,
        created_at: 1,
      })
      if (path === '/api/creation/history/start') return Response.json({ id: 42 })
      if (path.endsWith('/progress')) return new Response(null, { status: 204 })
      if (path === '/api/creation/brainstorm/turn') {
        brainstormRequests += 1
        if (brainstormRequests === 1) { restoreSignal = init?.signal; return pendingRestore }
        return new Promise<Response>(() => undefined)
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationHistoryOpenTarget(41)
    render(<CreationPanel />)
    const status = await screen.findByRole('status', { name: '正在准备第一条脑暴问题' })
    expect(status).toHaveTextContent('正在恢复脑暴进度')
    expect(status).toHaveTextContent('已请求恢复进度；尚未保存首题时会继续生成。')
    expect(status).toHaveTextContent('已等待 0 秒')
    const now = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(now + 31000)
    await waitFor(() => expect(status).toHaveTextContent('已等待 31 秒'), { timeout: 2500 })
    expect(status).toHaveTextContent('本次等待较久')

    if (outcome === 'completed') {
      await act(async () => { finishRestore(Response.json(brainstormState(0, question('restored.question', '目标', '恢复出的第一条问题')))) })
      expect(await screen.findByText('恢复出的第一条问题')).toBeInTheDocument()
      expect(screen.queryByRole('status', { name: '正在准备第一条脑暴问题' })).not.toBeInTheDocument()
      return
    }

    fireEvent.click(screen.getByRole('button', { name: '终止当前会话' }))
    expect(restoreSignal?.aborted).toBe(true)
    expect(screen.queryByRole('status', { name: '正在准备第一条脑暴问题' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '开启新会话' }))
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), { target: { value: '新的创作目标' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await waitFor(() => expect(brainstormRequests).toBe(2))
    const newSessionId = useAppStore.getState().creationDraft.sessionId
    await act(async () => { finishRestore(Response.json(brainstormState(0, question('old.question', '旧目标', '不应出现的恢复问题')))) })
    expect(useAppStore.getState().creationDraft).toMatchObject({ sessionId: newSessionId, rootRequest: '新的创作目标', brainstormState: null })
    const newStatus = screen.getByRole('status', { name: '正在准备第一条脑暴问题' })
    expect(newStatus).toHaveTextContent('正在生成第一条脑暴问题')
    expect(newStatus).toHaveTextContent('已等待 0 秒')
    expect(screen.getByRole('button', { name: '正在梳理' })).toBeDisabled()
    expect(screen.queryByText('不应出现的恢复问题')).not.toBeInTheDocument()
  })

  it('横向切换已准备的同层方向时显示真实父分支并清空上题选项，回看仍保持作答顺序', async () => {
    const requests: any[] = []
    const root = { ...question('root', '总体方向', '先考虑哪些方向？'), type: 'multi_choice' as const,
      options: [
        { id: 'a', label: '方向 A', description: '讨论 A。' },
        { id: 'b', label: '方向 B', description: '讨论 B。' },
      ],
    }
    const branchA = { ...question('branch-a', 'A 的解法', '方向 A 如何实施？'),
      type: 'multi_choice' as const, parent_question_id: 'root', parent_option_id: 'a' }
    const branchB = { ...question('branch-b', 'B 的解法', '方向 B 如何实施？'),
      type: 'multi_choice' as const, parent_question_id: 'root', parent_option_id: 'b' }
    const first = { ...brainstormState(1, null), current_question: branchA, history: [{ question: root,
      answer: { selected_option_ids: ['a', 'b'], custom_text: '', source: 'user' } }],
      decisions: [{ question_id: 'root', dimension: '总体方向', summary: '方向 A；方向 B', source: 'user' }],
    }
    const next = { ...first, revision: 2, depth: 2, answered_count: 2, current_question: branchB,
      history: [...first.history, { question: branchA,
        answer: { selected_option_ids: ['recommended'], custom_text: '', source: 'user' } }],
    }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        requests.push(JSON.parse(String(init?.body || '{}')))
        return Response.json(next)
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', rootRequest: '设计方案',
      sessionId: first.session_id, brainstormState: first })
    render(<CreationPanel />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：方向 A')
    expect(screen.getByText('可多选 · 先逐一讨论同层方向，再展开下一层')).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /推荐方向/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))
    expect(await screen.findByText('方向 B 如何实施？')).toBeInTheDocument()
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：方向 B')
    expect(screen.getByText('第 3 / 3 题')).toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /推荐方向/ })).not.toBeChecked()
    expect(requests).toHaveLength(1)
    expect(requests[0]).toMatchObject({ action: 'answer', question_id: 'branch-a', revision: 1,
      answer: { selected_option_ids: ['recommended'] } })
    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    expect(screen.getByText('方向 A 如何实施？')).toBeInTheDocument()
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：方向 A')
    expect(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /推荐方向/ })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '下一题' }))
    expect(screen.getByText('方向 B 如何实施？')).toBeInTheDocument()
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：方向 B')
    expect(requests).toHaveLength(1)
  })

  it('逐次展示一个问题、保存答案后更新创作简报', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        requests.push(body)
        if (body.action === 'start') {
          return Response.json(brainstormState(0, question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？')))
        }
        return Response.json(brainstormState(1, question('audience.primary', '目标读者', '这份内容首先写给谁看？')))
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const modelRow = screen.getByRole('button', { name: '选择创作生成模型' }).closest('.creation-model-row')
    const modeSelect = screen.getByRole('button', { name: '选择创作模式' })
    expect(modelRow).toContainElement(modeSelect)
    expect(modeSelect).toHaveTextContent('直出模式')
    fireEvent.click(modeSelect)
    expect(screen.getByRole('option', { name: /直出模式.*适合方向明确的需求/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式.*形成创作简报后再生成/ }))
    expect(modeSelect).toHaveTextContent('脑暴模式')
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '设计数据治理平台建设方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))

    const firstCard = await screen.findByText('这次创作最需要推动什么结果？')
    expect(firstCard).toBeInTheDocument()
    const userTurn = screen.getByLabelText('用户消息')
    const brainstormTurn = firstCard.closest('.creation-brainstorm-turn') as HTMLElement
    expect(userTurn).toHaveTextContent('设计数据治理平台建设方案')
    expect(brainstormTurn).toHaveAccessibleName('Agent 消息')
    expect(brainstormTurn).toHaveTextContent('创作 Agent')
    expect(userTurn.compareDocumentPosition(brainstormTurn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText(/第 1 轮模型追问/)).toBeInTheDocument()
    expect(screen.queryByText('这份内容首先写给谁看？')).not.toBeInTheDocument()
    const option = screen.getByRole('radio', { name: /推荐方向/ })
    fireEvent.click(option)
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))

    expect(await screen.findByText('这份内容首先写给谁看？')).toBeInTheDocument()
    expect(screen.getByText(/第 2 轮模型追问/)).toBeInTheDocument()
    expect(screen.getByText('已确认 1 项决定')).toBeInTheDocument()
    expect(screen.queryByLabelText('已确认决定')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '简报：目标与决策' })).toHaveValue('推荐方向')
    expect(requests[1]).toMatchObject({
      action: 'answer',
      revision: 0,
      question_id: 'outcome.primary',
      answer: { selected_option_ids: ['recommended'] },
    })

    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    expect(screen.getByText('这次创作最需要推动什么结果？')).toBeInTheDocument()
    expect(screen.getByText('第 1 / 2 题')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /推荐方向/ })).toBeChecked()
    expect(screen.getByRole('button', { name: /保存修改/ })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /下一题/ }))
    expect(screen.getByText('这份内容首先写给谁看？')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    fireEvent.click(screen.getByRole('radio', { name: /备选方向/ }))
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }))

    await waitFor(() => expect(requests[2]).toMatchObject({
      action: 'revise_answer',
      revision: 1,
      question_id: 'outcome.primary',
      answer: { selected_option_ids: ['alternative'] },
    }))
  })

  it('长期脑暴仅在左侧简报保留历史决定，当前题与回看题均不重复插入全部选择', async () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(2000)
    const history = Array.from({ length: 28 }, (_, index) => ({
      question: question(`history.${index}`, `历史维度 ${index + 1}`, `历史问题 ${index + 1}？`),
      answer: { selected_option_ids: ['recommended'], custom_text: '', source: 'user' },
    }))
    const decisions = history.map((turn, index) => ({
      question_id: turn.question.id,
      dimension: turn.question.dimension,
      summary: `仅在简报展示的历史决定 ${index + 1}`,
      source: index === 0 ? 'agent_assumption' : 'user',
    }))
    const state = {
      ...brainstormState(28, question('current', '落地执行安排', '具体执行安排是什么？')),
      history,
      decisions,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm', sessionId: state.session_id,
      rootRequest: '制定落地执行方案', brainstormState: state,
    })
    await act(async () => { render(<CreationPanel />) })
    const card = screen.getByText('具体执行安排是什么？').closest('.creation-brainstorm-card') as HTMLElement
    const timeline = card.closest('.creation-chat-timeline') as HTMLElement
    const briefViewport = screen.getByRole('textbox', { name: '简报：历史维度 1' }).closest('.creation-document-content') as HTMLElement
    expect(timeline.scrollTop).toBe(0)
    expect(briefViewport.scrollTop).toBe(0)
    expect(within(card).getByText('第 29 / 29 题')).toBeInTheDocument()
    expect(within(card).getByRole('radiogroup', { name: '答案选项' })).toBeInTheDocument()
    expect(within(card).queryByLabelText('已确认决定')).not.toBeInTheDocument()
    expect(card).not.toHaveTextContent('仅在简报展示的历史决定')
    for (const decision of decisions) {
      expect(screen.getByRole('textbox', { name: `简报：${decision.dimension}` })).toHaveValue(decision.summary)
    }
    timeline.scrollTop = 700
    vi.spyOn(timeline, 'getBoundingClientRect').mockReturnValue({ top: 100 } as DOMRect)
    vi.spyOn(card, 'getBoundingClientRect').mockReturnValue({ top: -400 } as DOMRect)
    fireEvent.click(within(card).getByRole('button', { name: '上一题' }))
    expect(timeline.scrollTop).toBe(200)
    expect(within(card).getByText('历史问题 28？')).toBeInTheDocument()
    expect(within(card).getByRole('radio', { name: /推荐方向/ })).toBeChecked()
    expect(card).not.toHaveTextContent('仅在简报展示的历史决定')
    fireEvent.click(within(card).getByRole('button', { name: '返回当前问题' }))
    expect(within(card).getByText('具体执行安排是什么？')).toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState).toEqual(state)
  })

  it('将自定义答案作为互斥独立选项，并仅在填写后提交裁剪后的内容', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        requests.push(JSON.parse(String(init?.body || '{}')))
        return Response.json(brainstormState(1, null, 'ready'))
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(
        0,
        question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？'),
      ),
    })

    render(<CreationPanel />)

    expect(screen.queryByRole('textbox', { name: '具体内容' })).not.toBeInTheDocument()
    const recommendedOption = screen.getByRole('radio', { name: /推荐方向/ })
    const customOption = screen.getByRole('radio', { name: /自定义答案/ })
    fireEvent.click(recommendedOption)
    expect(recommendedOption).toBeChecked()

    fireEvent.click(customOption)
    expect(customOption).toBeChecked()
    expect(recommendedOption).not.toBeChecked()
    const customAnswer = screen.getByRole('textbox', { name: '具体内容' })
    const submitButton = screen.getByRole('button', { name: /确认并继续/ })
    expect(submitButton).toBeDisabled()

    fireEvent.change(customAnswer, { target: { value: '  优先验证一线员工的检索效率  ' } })
    expect(submitButton).toBeEnabled()
    fireEvent.click(submitButton)

    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({
      action: 'answer',
      revision: 0,
      question_id: 'outcome.primary',
      answer: {
        selected_option_ids: [],
        custom_text: '优先验证一线员工的检索效率',
      },
    })
  })

  it('从已输入的自定义答案切回普通选项后不提交隐藏内容', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        requests.push(JSON.parse(String(init?.body || '{}')))
        return Response.json(brainstormState(1, null, 'ready'))
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(
        0,
        question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？'),
      ),
    })

    render(<CreationPanel />)

    const customOption = screen.getByRole('radio', { name: /自定义答案/ })
    fireEvent.click(customOption)
    fireEvent.change(screen.getByRole('textbox', { name: '具体内容' }), {
      target: { value: '这段自定义内容不应被提交' },
    })

    const alternativeOption = screen.getByRole('radio', { name: /备选方向/ })
    fireEvent.click(alternativeOption)
    expect(alternativeOption).toBeChecked()
    expect(customOption).not.toBeChecked()
    expect(screen.queryByRole('textbox', { name: '具体内容' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))

    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({
      action: 'answer',
      answer: {
        selected_option_ids: ['alternative'],
        custom_text: '',
      },
    })
    expect(requests[0].answer.custom_text).not.toContain('这段自定义内容')
  })

  it('编辑旧历史的混合答案时无损转换为仅自定义答案', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        requests.push(JSON.parse(String(init?.body || '{}')))
        return Response.json(brainstormState(
          2,
          question('audience.primary', '目标读者', '这份内容首先写给谁看？'),
        ))
      }
      return new Response('{}', { status: 404 })
    }))
    const legacyState = brainstormState(
      1,
      question('audience.primary', '目标读者', '这份内容首先写给谁看？'),
    )
    legacyState.history[0].answer = {
      selected_option_ids: ['recommended'],
      custom_text: '原补充文本',
      source: 'user',
    }
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: legacyState,
    })

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '上一题' }))

    expect(screen.getByRole('radio', { name: /自定义答案/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /推荐方向/ })).not.toBeChecked()
    const customAnswer = screen.getByRole('textbox', { name: '具体内容' })
    expect(customAnswer).toHaveValue('推荐方向；原补充文本')
    const saveButton = screen.getByRole('button', { name: /保存修改/ })
    expect(saveButton).toBeDisabled()

    fireEvent.change(customAnswer, {
      target: { value: '推荐方向；原补充文本（已核实）' },
    })
    expect(saveButton).toBeEnabled()
    fireEvent.click(saveButton)

    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({
      action: 'revise_answer',
      revision: 1,
      question_id: 'outcome.primary',
      answer: {
        selected_option_ids: [],
        custom_text: '推荐方向；原补充文本（已核实）',
      },
    })
  })

  it('脑暴过程中可随时终止会话并同步放弃状态', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 8 })
      if (url.pathname === '/api/creation/history/8/progress') return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        requests.push(body)
        return Response.json(body.action === 'abandon'
          ? brainstormState(1, null, 'abandoned')
          : brainstormState(0, question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？')))
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), {
      target: { value: '设计数据治理平台建设方案' },
    })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    expect(await screen.findByText('这次创作最需要推动什么结果？')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '终止当前会话' }))

    expect(await screen.findByText('本次脑暴已停止')).toBeInTheDocument()
    expect(screen.getByText(/已确认的决定和简报仍保留/)).toBeInTheDocument()
    expect(screen.getByLabelText('会话终止消息')).toHaveTextContent('终止了当前会话')
    expect(screen.queryByRole('radio', { name: /推荐方向/ })).not.toBeInTheDocument()
    await waitFor(() => expect(requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ action: 'abandon', revision: 0 }),
    ])))
  })

  it('启动脑暴时把显式选择的 Skill 业务规则一并发送', async () => {
    let startPayload: any = null
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([installedBrainstormSkill])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        startPayload = JSON.parse(String(init?.body || '{}'))
        return Response.json(brainstormState(
          0,
          question('business_outcome', '业务目标与预期决策', '广告诊断首先要推动什么业务动作？'),
        ))
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/) as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: '@微服务' } })
    fireEvent.click(await screen.findByRole('option', { name: /微服务模块技术方案文档/ }))
    fireEvent.change(input, {
      target: { value: `${input.value}设计广告诊断接口方案` },
    })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))

    expect(await screen.findByText('广告诊断首先要推动什么业务动作？')).toBeInTheDocument()
    expect(startPayload.selected_skills[0]).toMatchObject({
      id: 'microservice-solution',
      title: '微服务模块技术方案文档',
      summary: '覆盖业务流程、数据所有权、组织保障和上线验收。',
      workflowRole: 'primary',
    })
    expect(startPayload.selected_skills[0].executionSteps[0].objective).toContain('业务目标')
  })

  it('非显式 @ 的脑暴也先完成执行 Skill 路由再建立章节覆盖上下文', async () => {
    const requestOrder: string[] = []
    let matchPayload: any = null
    let startPayload: any = null
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([installedBrainstormSkill])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/skills/match') {
        requestOrder.push('match')
        matchPayload = JSON.parse(String(init?.body || '{}'))
        return Response.json({ skill_ids: [67], source: 'model', reasoning: '匹配技术方案' })
      }
      if (url.pathname === '/api/creation/history/start') {
        requestOrder.push('history')
        return Response.json({ id: 1 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        requestOrder.push('brainstorm')
        startPayload = JSON.parse(String(init?.body || '{}'))
        return Response.json(brainstormState(
          0,
          question('business_outcome', '业务目标与预期决策', '广告诊断首先要推动什么业务动作？'),
        ))
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    // 先等待已安装 Skill 完成加载，但不选择它，确保本例走提交后的模型路由。
    fireEvent.change(input, { target: { value: '@微服务' } })
    await screen.findByRole('option', { name: /微服务模块技术方案文档/ })
    fireEvent.change(input, { target: { value: '设计广告诊断接口方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))

    expect(await screen.findByText('广告诊断首先要推动什么业务动作？')).toBeInTheDocument()
    expect(requestOrder).toEqual(['match', 'history', 'brainstorm'])
    expect(matchPayload.prompt).toBe('设计广告诊断接口方案')
    expect(startPayload.selected_skills[0]).toMatchObject({
      id: 'microservice-solution',
      title: '微服务模块技术方案文档',
    })
    expect(startPayload.selected_skills[0].executionSteps.map((step: any) => step.title)).toEqual([
      '需求背景与范围',
      '总体方案与系统边界',
      '核心功能机制',
    ])
  })

  it.each(['brief', 'click', 'enter'] as const)('模型收敛后把结构化简报交给现有 Agent，并支持文档追问（%s）', async (submission) => {
    const agentPayloads: any[] = []
    let gatewayPayload: any = null
    let finishGeneration!: () => void
    const generationPending = new Promise<void>((resolve) => { finishGeneration = resolve })
    const historyStartPayloads: any[] = []
    useAppStore.getState().setAuthSession({
      access_token: 'test-token',
      expires_at: '2099-01-01T00:00:00Z',
      user: {
        id: 'user-brainstorm-test',
        nickname: '小麦',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-01-01T00:00:00Z',
      },
    })
    useAppStore.getState().setCloudBalance({
      available: '100.0000',
      reserved: '0.0000',
      currency: 'CREDIT',
      as_of: '2026-08-31T00:00:00Z',
    })
    useAppStore.getState().setCreationModelConfig('mbcd-plus-v1', { enabled: true })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/v1/billing/balance') {
        return Response.json({
          data: {
            available: '100.0000',
            reserved: '0.0000',
            currency: 'CREDIT',
            as_of: '2026-08-31T00:00:00Z',
          },
        })
      }
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') {
        historyStartPayloads.push(JSON.parse(String(init?.body || '{}')))
        return Response.json({ id: 1 })
      }
      if (url.pathname.endsWith('/progress')) return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/skills/match') return Response.json({ matches: [] })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        if (body.action === 'edit_brief') {
          expect(body).toMatchObject({ revision: 1, brief_edits: { 'outcome.primary': '人工调整后的方案目标' } })
          return Response.json({ ...brainstormState(1, null, 'ready'), revision: 2,
            brief_edits: body.brief_edits, brief_markdown: '# 创作简报\n人工调整后的方案目标' })
        }
        if (body.action === 'answer') {
          return Response.json(brainstormState(1, null, 'ready'))
        }
        return Response.json(brainstormState(0, question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？')))
      }
      if (url.pathname === '/v1/gateway/chat') {
        gatewayPayload = JSON.parse(String(init?.body || '{}'))
        await generationPending
        return sse([
          { type: 'delta', text: '已按简报' },
          { type: 'done', answer: '已按简报完成外部推理。' },
        ])
      }
      if (url.pathname === '/api/creation/agent/run') {
        const agentPayload = JSON.parse(String(init?.body || '{}'))
        agentPayloads.push(agentPayload)
        const base = {
          schema_version: 'creation.agent.v1',
          event_id: 'event-1',
          session_id: 'session-brainstorm-ui',
          run_id: 'run-brainstorm-ui',
          sequence: 1,
          timestamp: Date.now(),
          status: 'completed',
          actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
          summary: '完成',
          environment_patch: {},
          data: {},
        }
        if (agentPayload.user_prompt?.includes('基于最新脑暴结论继续完善当前文档') || agentPayload.user_prompt === '请补充实施步骤和验收标准') {
          return sse([
            {
              ...base,
              event_id: 'event-follow-up-patch',
              run_id: 'run-brainstorm-follow-up',
              type: 'document.patch.applied',
              data: {
                patch: { summary: '已将新增脑暴结论写入文档', target_sections: ['实施方案'] },
                document: '# 最终方案\n\n已补充新增脑暴结论。',
              },
            },
            {
              ...base,
              event_id: 'event-follow-up-completed',
              run_id: 'run-brainstorm-follow-up',
              sequence: 2,
              type: 'run.completed',
              data: { document: '# 最终方案\n\n已补充新增脑暴结论。' },
            },
          ])
        }
        if (!agentPayload.resume_state) {
          return sse([
            {
              ...base,
              type: 'model.request',
              status: 'waiting',
              data: {
                request_id: 'model-brainstorm-ready-1',
                messages: [
                  { role: 'system', content: '严格遵守脑暴简报。' },
                  { role: 'user', content: '生成最终产品方案。' },
                ],
              },
            },
            {
              ...base,
              event_id: 'event-2',
              sequence: 2,
              type: 'run.paused',
              status: 'waiting',
              data: {
                reason: 'external_model',
                continuation: { cursor: 2, token: 'brainstorm-resume' },
              },
            },
          ])
        }
        return sse([
          { ...base, event_id: 'event-3', sequence: 3, type: 'document.replaced', data: { content: '# 最终方案\n\n已按简报生成。' } },
          { ...base, event_id: 'event-4', sequence: 4, type: 'run.completed', data: { document: '# 最终方案\n\n已按简报生成。' } },
        ])
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') return Response.json({ id: 1 })
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '选择创作模式' }))
    fireEvent.click(screen.getByRole('option', { name: /脑暴模式/ }))
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), {
      target: { value: '设计产品方案' },
    })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await screen.findByText('这次创作最需要推动什么结果？')
    fireEvent.click(screen.getByRole('radio', { name: /推荐方向/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))

    const ready = await screen.findByText('关键方向已经收敛，可以开始生成')
    expect(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)).toBeDisabled()
    fireEvent.change(screen.getByLabelText('简报：目标与决策'), { target: { value: '人工调整后的方案目标' } })
    fireEvent.click(screen.getByRole('button', { name: '保存简报修改' }))
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.revision).toBe(2))
    const generateFromBriefButton = within(screen.getByRole('group', { name: '创作操作' }))
      .getByRole('button', { name: /开始创作/ })
    expect(screen.getAllByRole('button', { name: /开始创作/ })).toHaveLength(1)
    fireEvent.click(generateFromBriefButton)

    await waitFor(() => expect(gatewayPayload).toBeTruthy())
    expect(screen.getByLabelText('Agent 执行情况')).toBeInTheDocument()
    expect(screen.queryByLabelText('最新脑暴操作')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续脑暴' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '提交' })).not.toBeInTheDocument()
    finishGeneration()

    await waitFor(() => expect(agentPayloads).toHaveLength(2))
    const userTurn = screen.getByLabelText('用户消息')
    const brainstormTurn = ready.closest('.creation-brainstorm-turn') as HTMLElement
    const executionTrace = await screen.findByLabelText('Agent 执行情况')
    expect(brainstormTurn).toHaveTextContent('脑暴步骤')
    expect(userTurn.nextElementSibling).toBe(brainstormTurn)
    expect(brainstormTurn.nextElementSibling).toBe(executionTrace)
    expect(screen.queryByLabelText('最新脑暴操作')).not.toBeInTheDocument()
    expect(generateFromBriefButton).toBeEnabled()
    expect(historyStartPayloads).toHaveLength(2)
    expect(historyStartPayloads[0]).toMatchObject({
      creation_mode: 'brainstorm',
      creation_brief: null,
      brainstorm_revision: null,
    })
    expect(historyStartPayloads[1]).toMatchObject({
      creation_mode: 'brainstorm',
      creation_brief: { phase: 'ready', revision: 2, brief_edits: { 'outcome.primary': '人工调整后的方案目标' } },
      brainstorm_revision: 2,
    })
    expect(agentPayloads[0].creation_mode).toBe('brainstorm')
    expect(agentPayloads[0].creation_brief).toMatchObject({ phase: 'ready', revision: 2, brief_edits: { 'outcome.primary': '人工调整后的方案目标' } })
    expect(agentPayloads[0].model_mode).toBe('external')
    expect(agentPayloads[1]).toMatchObject({
      resume_state: { cursor: 2, token: 'brainstorm-resume' },
      model_result: '已按简报完成外部推理。',
      creation_mode: 'brainstorm',
    })
    expect(gatewayPayload).toMatchObject({
      request_id: 'model-brainstorm-ready-1',
      stream: true,
      caller: 'creation',
    })
    expect(await screen.findByText('最终方案')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑创作简报' }))
    expect(screen.getByLabelText('简报：目标与决策')).toHaveValue('人工调整后的方案目标')
    expect(screen.queryByRole('button', { name: '复制' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看创作文档' }))

    const followUp = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    expect(followUp).toBeEnabled()
    if (submission !== 'brief') {
      fireEvent.change(followUp, { target: { value: '请补充实施步骤和验收标准' } })
      expect(followUp).toHaveValue('请补充实施步骤和验收标准')
    }
    if (submission === 'enter') {
      fireEvent.keyDown(followUp, { key: 'Enter', shiftKey: true })
      expect(agentPayloads).toHaveLength(2)
      fireEvent.keyDown(followUp, { key: 'Enter' })
    } else {
      fireEvent.click(within(screen.getByRole('group', { name: '创作操作' }))
        .getByRole('button', { name: '提交' }))
    }
    await waitFor(() => expect(agentPayloads).toHaveLength(3))
    expect(agentPayloads[2]).toMatchObject({
      user_prompt: submission === 'brief'
        ? expect.stringContaining('基于最新脑暴结论继续完善当前文档')
        : '请补充实施步骤和验收标准',
      current_document: '# 最终方案\n\n已按简报生成。',
      creation_mode: 'brainstorm',
      creation_brief: { phase: 'ready', revision: 2, brief_edits: { 'outcome.primary': '人工调整后的方案目标' } },
    })
    expect(await screen.findByText('已补充新增脑暴结论。')).toBeInTheDocument()
    await waitFor(() => expect(followUp).toBeEnabled())
    expect(followUp).toHaveValue('')
  })

  it('模型收敛后展示推荐方向和末项自定义方向，并可继续脑暴', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        requests.push(body)
        if (body.action === 'continue_brainstorm') {
          return Response.json(brainstormState(
            2,
            question('private.infrastructure', '私有化部署下钻', '私有化部署首先适配哪一种基础设施？'),
          ))
        }
        return Response.json(brainstormState(1, null, 'ready'))
      }
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(1, null, 'ready'),
    })
    render(<CreationPanel />)

    const restoredInstruction = screen.getByLabelText('用户消息')
    expect(restoredInstruction).toHaveTextContent('设计企业知识库方案')
    expect(screen.getByText('已确认 1 项决定。')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '待决定与补充' })).toHaveTextContent('成功标准待补充')
    expect(screen.queryByText(/其余.*开放假设/)).not.toBeInTheDocument()
    expect(screen.queryByText(brainstormState(1, null, 'ready').readiness_reason)).not.toBeInTheDocument()
    expect(screen.getByText('关键方向已经收敛，可以开始生成').closest('.creation-brainstorm-turn'))
      .toHaveAccessibleName('Agent 消息')

    fireEvent.click(screen.getByRole('button', { name: /继续脑暴/ }))
    expect(screen.getByRole('group', { name: '继续脑暴方向' }))
      .toHaveClass('creation-brainstorm-options--continuation')
    expect(screen.getByRole('checkbox', { name: /挑战关键假设/ })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /补强落地路径/ })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /自定义脑暴方向/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /补强落地路径/ }))
    const directionOptions = screen.getAllByRole('checkbox')
    expect(directionOptions[directionOptions.length - 1]).toHaveAccessibleName(/自定义脑暴方向/)
    fireEvent.click(screen.getByRole('button', { name: /按此方向继续/ }))
    expect(await screen.findByText('私有化部署首先适配哪一种基础设施？')).toBeInTheDocument()
    expect(screen.getAllByText('私有化部署下钻', { exact: false }).length).toBeGreaterThan(0)
    expect(requests[requests.length - 1]).toMatchObject({
      action: 'continue_brainstorm',
      continuation_direction_ids: ['challenge_assumptions', 'delivery_path'],
      focus_hint: '',
    })
  })

  it('问题进行中可换一个脑暴方向，并展示模型方向及末项自定义输入', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        requests.push(body)
        return Response.json({
          ...brainstormState(2, null, 'ready'),
          phase: 'choosing_direction',
          current_question: question('audience.primary', '目标读者', '这份内容首先写给谁看？'),
        })
      }
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(
        1,
        question('audience.primary', '目标读者', '这份内容首先写给谁看？'),
      ),
    })
    render(<CreationPanel />)

    expect(screen.queryByRole('button', { name: '基于当前简报生成' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /换一个脑暴方向/ }))

    await screen.findByRole('group', { name: '继续脑暴方向' })
    expect(requests[0]).toMatchObject({
      action: 'change_direction',
      revision: 1,
    })
    await waitFor(() => {
      const currentGroup = screen.getByRole('group', { name: '继续脑暴方向' })
      expect(currentGroup.parentElement).toHaveTextContent('选择继续脑暴的方向')
      expect(within(currentGroup).getByRole('checkbox', { name: /挑战关键假设/ })).toBeChecked()
      expect(within(currentGroup).getByRole('checkbox', { name: /补强落地路径/ })).toBeInTheDocument()
    })
    const directionGroup = screen.getByRole('group', { name: '继续脑暴方向' })
    const directionOptions = within(directionGroup).getAllByRole('checkbox')
    expect(directionOptions[directionOptions.length - 1]).toHaveAccessibleName(/自定义脑暴方向/)

    fireEvent.click(directionOptions[directionOptions.length - 1])
    expect(screen.getByLabelText('脑暴方向')).toBeInTheDocument()
  })

  it('文档生成后保留原脑暴卡片，继续脑暴和唯一生成入口位于右下角', async () => {
    const continuedQuestion = question('continued-risk', '风险下钻', '下一步优先验证哪个失败场景？')
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as { action: string }
        if (body.action === 'answer') {
          const ready = brainstormState(3, null, 'ready')
          return Response.json({
            ...ready,
            history: [
              ...(brainstormState(1, null, 'ready').history || []),
              {
                question: continuedQuestion,
                answer: {
                  selected_option_ids: ['recommended'],
                  custom_text: '',
                  source: 'user',
                },
              },
            ],
          })
        }
        return Response.json(brainstormState(
          2,
          continuedQuestion,
        ))
      }
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      generatedContent: '# 已生成文档',
      brainstormState: brainstormState(1, null, 'ready'),
      conversation: [{
        id: 'brainstorm-root',
        role: 'user',
        content: '设计企业知识库方案',
        createdAt: 1,
        runId: 'run-generated',
      }, {
        id: 'brainstorm-root-duplicate',
        role: 'user',
        content: '设计企业知识库方案',
        createdAt: 2,
      }, {
        id: 'generated-assistant',
        role: 'assistant',
        content: '文档已生成。',
        createdAt: 3,
        runId: 'run-generated',
      }],
      agentEvents: [{
        schema_version: 'creation.agent.v1',
        event_id: 'run-generated-completed',
        session_id: 'session-brainstorm-ui',
        run_id: 'run-generated',
        sequence: 1,
        timestamp: 2,
        type: 'run.completed',
        status: 'completed',
        actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
        summary: '文档生成完成',
        environment_patch: {},
        data: {},
      }],
    })
    render(<CreationPanel />)

    const trace = await screen.findByLabelText('Agent 执行情况')
    const originalBrainstormCard = (await screen.findByText('关键方向已经收敛，可以开始生成'))
      .closest('.creation-brainstorm-turn') as HTMLElement
    const composerActions = screen.getByRole('group', { name: '创作操作' })
    const restoredFollowUp = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    expect(restoredFollowUp).toBeEnabled()
    expect(trace).toBeInTheDocument()
    expect(screen.queryByLabelText('最新脑暴操作')).not.toBeInTheDocument()
    expect(originalBrainstormCard.compareDocumentPosition(trace) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(within(originalBrainstormCard).queryByRole('button', { name: /继续脑暴|开始创作/ }))
      .not.toBeInTheDocument()
    const continueButton = within(composerActions).getByRole('button', { name: '继续脑暴' })
    const generateButton = within(composerActions).getByRole('button', { name: '提交' })
    expect(continueButton.nextElementSibling).toBe(generateButton)
    expect(screen.getAllByRole('button', { name: '提交' })).toHaveLength(1)
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView
    const scrollIntoView = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    })
    fireEvent.click(continueButton)
    const latestBrainstormAction = screen.getByLabelText('最新脑暴操作')
    expect(latestBrainstormAction.querySelector('.creation-brainstorm-card')).not.toBeInTheDocument()
    expect(within(latestBrainstormAction).getByRole('group', { name: '继续脑暴方向' }))
      .toBeInTheDocument()
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    })
    if (originalScrollIntoView) {
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
        configurable: true,
        writable: true,
        value: originalScrollIntoView,
      })
    } else {
      delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
    }
    expect(within(originalBrainstormCard).queryByRole('group', { name: '继续脑暴方向' }))
      .not.toBeInTheDocument()
    fireEvent.click(within(latestBrainstormAction).getByRole('button', { name: /按此方向继续/ }))
    expect(await within(latestBrainstormAction).findByText('下一步优先验证哪个失败场景？'))
      .toBeInTheDocument()
    expect(restoredFollowUp).toBeDisabled()
    expect(within(originalBrainstormCard).getByText('关键方向已经收敛，可以开始生成'))
      .toBeInTheDocument()
    expect(within(originalBrainstormCard).queryByText('下一步优先验证哪个失败场景？'))
      .not.toBeInTheDocument()

    fireEvent.click(within(latestBrainstormAction).getByRole('radio', { name: /推荐方向/ }))
    fireEvent.click(within(latestBrainstormAction).getByRole('button', { name: /确认并继续/ }))
    await waitFor(() => {
      expect(within(latestBrainstormAction).getByRole('group', { name: '继续脑暴方向' }))
        .toBeInTheDocument()
    })
    expect(within(latestBrainstormAction).queryByRole('button', { name: '提交' }))
      .not.toBeInTheDocument()
    expect(restoredFollowUp).toBeEnabled()
    expect(within(composerActions).getByRole('button', { name: '提交' })).toBeEnabled()
    expect(within(latestBrainstormAction).getByText('下一步优先验证哪个失败场景？'))
      .toBeInTheDocument()
    expect(within(latestBrainstormAction).getByLabelText('已完成的继续脑暴'))
      .toHaveTextContent('推荐方向')
    expect(within(originalBrainstormCard).queryByText('下一步优先验证哪个失败场景？'))
      .not.toBeInTheDocument()
  })

  it('继续脑暴遇到历史版本冲突时自动恢复实时问题', async () => {
    const actions: string[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as { action: string }
        actions.push(body.action)
        if (body.action === 'continue_brainstorm') {
          return Response.json({
            code: 'BRAINSTORM_REVISION_CONFLICT',
            message: '脑暴内容已更新，请刷新后重试',
          }, { status: 409 })
        }
        return Response.json(brainstormState(
          2,
          question('continued-live', '实时进度', '服务端已经生成的下一题是什么？'),
        ))
      }
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      generatedContent: '# 已生成文档',
      brainstormState: brainstormState(1, null, 'ready'),
      conversation: [{
        id: 'brainstorm-root',
        role: 'user',
        content: '设计企业知识库方案',
        createdAt: 1,
      }],
      agentEvents: [{
        schema_version: 'creation.agent.v1',
        event_id: 'run-conflict-completed',
        session_id: 'session-brainstorm-ui',
        run_id: 'run-conflict',
        sequence: 1,
        timestamp: 2,
        type: 'run.completed',
        status: 'completed',
        actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
        summary: '文档生成完成',
        environment_patch: {},
        data: {},
      }],
    })
    render(<CreationPanel />)

    fireEvent.click(within(screen.getByRole('group', { name: '创作操作' })).getByRole('button', { name: '继续脑暴' }))
    const latestBrainstormAction = screen.getByLabelText('最新脑暴操作')
    fireEvent.click(within(latestBrainstormAction).getByRole('button', { name: /按此方向继续/ }))

    expect(await within(latestBrainstormAction).findByText('服务端已经生成的下一题是什么？'))
      .toBeInTheDocument()
    expect(screen.queryByText('脑暴内容已更新，请刷新后重试')).not.toBeInTheDocument()
    expect(actions).toEqual(['continue_brainstorm', 'start'])
    expect(screen.getByText('关键方向已经收敛，可以开始生成')).toBeInTheDocument()
  })

  it('正式生成失败后继续脑暴成功会清除旧的通用失败提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 1 })
      if (url.pathname.endsWith('/progress')) return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/skills/match') return Response.json({ matches: [] })
      if (url.pathname === '/api/creation/agent/run') throw new TypeError('Failed to fetch')
      if (url.pathname === '/api/creation/brainstorm/turn') {
        return Response.json(brainstormState(
          2,
          question('problem.evidence', '现状问题与证据', '当前最显著的阻碍是什么？'),
        ))
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') return Response.json({ id: 1 })
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(1, null, 'ready'),
    })
    render(<CreationPanel />)

    fireEvent.click(screen.getByRole('button', { name: /开始创作/ }))

    expect(await screen.findByText('生成失败，请稍后重试')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /继续脑暴/ }))
    fireEvent.click(screen.getByRole('button', { name: /按此方向继续/ }))

    expect(await screen.findByText('当前最显著的阻碍是什么？')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByText('生成失败，请稍后重试')).not.toBeInTheDocument()
    })
  })

  it('继续脑暴的最后一项允许用户输入自定义方向', async () => {
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') return new Response('{}', { status: 404 })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        requests.push(body)
        return Response.json(brainstormState(
          2,
          question('migration.cost', '迁移成本', '哪类迁移成本最需要优先验证？'),
        ))
      }
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm',
      rootRequest: '设计企业知识库方案',
      sessionId: 'session-brainstorm-ui',
      brainstormState: brainstormState(1, null, 'ready'),
    })
    render(<CreationPanel />)

    fireEvent.click(screen.getByRole('button', { name: /继续脑暴/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /自定义脑暴方向/ }))
    const customDirection = screen.getByLabelText('脑暴方向')
    fireEvent.change(customDirection, { target: { value: '从真实用户迁移成本继续脑暴' } })
    fireEvent.click(screen.getByRole('button', { name: /按此方向继续/ }))

    await screen.findByText('哪类迁移成本最需要优先验证？')
    expect(requests[requests.length - 1]).toMatchObject({
      action: 'continue_brainstorm',
      continuation_direction_id: '__custom__',
      focus_hint: '从真实用户迁移成本继续脑暴',
    })
  })
  it('未保存简报在面板重开后保留，版本冲突刷新后可重试保存', async () => {
    let attempts = 0
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body || '{}'))
        requests.push(body)
        if (body.action === 'edit_brief' && attempts++ === 0) return Response.json({ code: 'BRAINSTORM_REVISION_CONFLICT', message: '版本已更新' }, { status: 409 })
        return Response.json({ ...brainstormState(1, null, 'ready'), revision: body.action === 'start' ? 2 : 3, brief_edits: body.brief_edits || {} })
      }
      return Response.json({}, { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', sessionId: 'session-brainstorm-ui', rootRequest: '测试方案', brainstormState: brainstormState(1, null, 'ready') })
    const first = render(<CreationPanel />)
    fireEvent.change(screen.getByLabelText('简报：目标与决策'), { target: { value: '尚未保存的人工想法' } })
    first.unmount()
    render(<CreationPanel />)
    expect(screen.getByLabelText('简报：目标与决策')).toHaveValue('尚未保存的人工想法')
    fireEvent.click(screen.getByRole('button', { name: '保存简报修改' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('已读取最新简报')
    expect(screen.getByLabelText('简报：目标与决策')).toHaveValue('尚未保存的人工想法')
    fireEvent.click(screen.getByRole('button', { name: '保存简报修改' }))
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.revision).toBe(3))
    expect(requests[requests.length - 1]).toMatchObject({ action: 'edit_brief', revision: 2, brief_edits: { 'outcome.primary': '尚未保存的人工想法' } })
  })

})
