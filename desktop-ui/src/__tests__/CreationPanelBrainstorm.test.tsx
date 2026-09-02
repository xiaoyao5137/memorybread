import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  readiness_reason: phase === 'ready' ? '主方向已经清晰，仍可继续细化。' : '仍有关键分支需要确认。',
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

  afterEach(() => {
    vi.unstubAllGlobals()
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
    expect(thinkingState).toHaveTextContent('正在梳理你的创作目标')
    expect(thinkingState).toHaveTextContent('找出最值得先确认的关键方向')
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
    expect(screen.getByText('1 项已确认')).toBeInTheDocument()
    expect(screen.getByLabelText('已确认决定')).toHaveTextContent('目标与决策')
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

  it('提前收敛时确认开放假设，并把结构化简报交给现有 Agent', async () => {
    const agentPayloads: any[] = []
    let gatewayPayload: any = null
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
        if (body.action === 'finish') {
          expect(body.accept_assumptions).toBe(true)
          return Response.json(brainstormState(1, null, 'ready'))
        }
        return Response.json(brainstormState(0, question('outcome.primary', '目标与决策', '这次创作最需要推动什么结果？')))
      }
      if (url.pathname === '/v1/gateway/chat') {
        gatewayPayload = JSON.parse(String(init?.body || '{}'))
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
        if (agentPayload.user_prompt?.includes('基于最新脑暴结论继续完善当前文档')) {
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
    fireEvent.click(screen.getByRole('button', { name: '基于当前简报生成' }))

    const ready = await screen.findByText('关键方向已经收敛，可以开始生成')
    const generateFromBriefButton = within(ready.closest('.creation-brainstorm-card') as HTMLElement)
      .getByRole('button', { name: /按此生成/ })
    fireEvent.click(generateFromBriefButton)

    await waitFor(() => expect(agentPayloads).toHaveLength(2))
    const userTurn = screen.getByLabelText('用户消息')
    const brainstormTurn = ready.closest('.creation-brainstorm-turn') as HTMLElement
    const executionTrace = await screen.findByLabelText('Agent 执行情况')
    expect(brainstormTurn).toHaveTextContent('脑暴步骤')
    expect(userTurn.nextElementSibling).toBe(brainstormTurn)
    expect(brainstormTurn.nextElementSibling).toBe(executionTrace)
    expect(executionTrace.compareDocumentPosition(screen.getByLabelText('最新脑暴操作')) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(generateFromBriefButton).toBeDisabled()
    expect(historyStartPayloads).toHaveLength(2)
    expect(historyStartPayloads[0]).toMatchObject({
      creation_mode: 'brainstorm',
      creation_brief: null,
      brainstorm_revision: null,
    })
    expect(historyStartPayloads[1]).toMatchObject({
      creation_mode: 'brainstorm',
      creation_brief: { phase: 'ready', revision: 1 },
      brainstorm_revision: 1,
    })
    expect(agentPayloads[0].creation_mode).toBe('brainstorm')
    expect(agentPayloads[0].creation_brief).toMatchObject({ phase: 'ready', revision: 1 })
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

    const latestBrainstormAction = screen.getByLabelText('最新脑暴操作')
    fireEvent.click(within(latestBrainstormAction)
      .getByRole('button', { name: '继续生成文档内容' }))
    await waitFor(() => expect(agentPayloads).toHaveLength(3))
    expect(agentPayloads[2]).toMatchObject({
      user_prompt: expect.stringContaining('基于最新脑暴结论继续完善当前文档'),
      current_document: '# 最终方案\n\n已按简报生成。',
      creation_mode: 'brainstorm',
      creation_brief: { phase: 'ready', revision: 1 },
    })
    expect(await screen.findByText('已补充新增脑暴结论。')).toBeInTheDocument()
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
    expect(screen.getByText('关键方向已经收敛，可以开始生成').closest('.creation-brainstorm-turn'))
      .toHaveAccessibleName('Agent 消息')

    fireEvent.click(screen.getByRole('button', { name: /继续脑暴/ }))
    expect(screen.getByRole('radiogroup', { name: '继续脑暴方向' }))
      .toHaveClass('creation-brainstorm-options--continuation')
    expect(screen.getByRole('radio', { name: /挑战关键假设/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /补强落地路径/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /自定义脑暴方向/ })).toBeInTheDocument()
    const directionOptions = screen.getAllByRole('radio')
    expect(directionOptions[directionOptions.length - 1]).toHaveAccessibleName(/自定义脑暴方向/)
    fireEvent.click(screen.getByRole('button', { name: /按此方向继续/ }))
    expect(await screen.findByText('私有化部署首先适配哪一种基础设施？')).toBeInTheDocument()
    expect(screen.getByText('私有化部署下钻', { exact: false })).toBeInTheDocument()
    expect(requests[requests.length - 1]).toMatchObject({
      action: 'continue_brainstorm',
      continuation_direction_id: 'challenge_assumptions',
      focus_hint: '',
    })
  })

  it('文档生成后保留原脑暴卡片位置，仅将继续脑暴入口放到最新对话', async () => {
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
    const latestBrainstormAction = screen.getByLabelText('最新脑暴操作')
    expect(trace).toBeInTheDocument()
    expect(within(latestBrainstormAction).queryByText('创作 Agent')).not.toBeInTheDocument()
    expect(within(latestBrainstormAction).getAllByText('继续脑暴', { exact: true })).toHaveLength(1)
    expect(originalBrainstormCard.compareDocumentPosition(trace) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(trace.compareDocumentPosition(latestBrainstormAction) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(within(originalBrainstormCard).queryByRole('button', { name: /继续脑暴/ }))
      .not.toBeInTheDocument()
    const continueButton = within(latestBrainstormAction).getByRole('button', { name: /继续脑暴/ })
    expect(latestBrainstormAction.querySelector('.creation-brainstorm-card')).not.toBeInTheDocument()
    expect(continueButton)
      .toBeInTheDocument()
    expect(within(latestBrainstormAction).getByRole('button', { name: '继续生成文档内容' }))
      .toBeInTheDocument()
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView
    const scrollIntoView = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    })
    fireEvent.click(continueButton)
    expect(latestBrainstormAction.querySelector('.creation-brainstorm-card')).not.toBeInTheDocument()
    expect(within(latestBrainstormAction).getByRole('radiogroup', { name: '继续脑暴方向' }))
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
    expect(within(originalBrainstormCard).queryByRole('radiogroup', { name: '继续脑暴方向' }))
      .not.toBeInTheDocument()
    fireEvent.click(within(latestBrainstormAction).getByRole('button', { name: /按此方向继续/ }))
    expect(await within(latestBrainstormAction).findByText('下一步优先验证哪个失败场景？'))
      .toBeInTheDocument()
    expect(within(originalBrainstormCard).getByText('关键方向已经收敛，可以开始生成'))
      .toBeInTheDocument()
    expect(within(originalBrainstormCard).queryByText('下一步优先验证哪个失败场景？'))
      .not.toBeInTheDocument()

    fireEvent.click(within(latestBrainstormAction).getByRole('radio', { name: /推荐方向/ }))
    fireEvent.click(within(latestBrainstormAction).getByRole('button', { name: /确认并继续/ }))
    await waitFor(() => {
      expect(within(latestBrainstormAction).getByRole('radiogroup', { name: '继续脑暴方向' }))
        .toBeInTheDocument()
    })
    expect(within(latestBrainstormAction).getByRole('button', { name: '继续生成文档内容' }))
      .toBeInTheDocument()
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

    const latestBrainstormAction = screen.getByLabelText('最新脑暴操作')
    fireEvent.click(within(latestBrainstormAction).getByRole('button', { name: /继续脑暴/ }))
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

    const ready = screen.getByText('关键方向已经收敛，可以开始生成')
    fireEvent.click(within(ready.closest('.creation-brainstorm-card') as HTMLElement)
      .getByRole('button', { name: /按此生成/ }))

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
    fireEvent.click(screen.getByRole('radio', { name: /自定义脑暴方向/ }))
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
})
