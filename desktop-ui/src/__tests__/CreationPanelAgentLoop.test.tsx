import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'

const goal = {
  objective: '生成 Agent 架构方案',
  status: 'active',
  revision: 2,
  remaining_steps: ['文档撰写 Agent'],
  outcome: '',
}

const event = (
  type: string,
  sequence: number,
  summary: string,
  actor = { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
  data: Record<string, unknown> = {},
  environment_patch: Record<string, unknown> = {},
) => ({
  schema_version: 'creation.agent.v1',
  event_id: `event-${sequence}`,
  session_id: 'session-agent-test',
  run_id: `run-${sequence < 20 ? 1 : 2}`,
  sequence,
  timestamp: 1_720_000_000_000 + sequence,
  type,
  status: type.endsWith('.completed') || type === 'document.replaced' ? 'completed' : 'running',
  actor,
  summary,
  goal,
  environment_patch,
  data,
})

const sse = (events: object[]) => new Response(
  events.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
  { headers: { 'Content-Type': 'text/event-stream' } },
)

const installedStyleSkill = {
  id: 31,
  client_skill_key: 'agent-architecture-style',
  cloud_skill_id: null,
  source_kind: 'bake_document',
  source_id: 'doc-agent-style',
  title: 'Agent 架构方案风格',
  summary: '适合生成 Agent 架构方案并复刻技术文档写法。',
  category_id: null,
  common_titles: ['子标题使用“对象＋如何＋动作”的问句结构'],
  title_style: '子标题使用“对象＋如何＋动作”的问句结构',
  text_style: '先解释为什么需要，再说明方案如何落地，最后收束风险与验证。',
  diagram_style: '推荐工具：PlantUML。使用组件图表达 Agent、Tool 与 Skill 的调用关系。',
  structure_pattern: ['问题与原因', '方案设计', '风险与验证'],
  writing_guidelines: ['习惯用“基于此”承接依据并转入方案。'],
  section_headings: {
    common_titles: '标题设计风格',
    title_style: '标题设计风格',
    text_style: '行文设计思路',
    diagram_style: '图片生成方式',
    structure_pattern: '章节组织骨架',
    writing_guidelines: '话术表达风格',
  },
  field_examples: {
    common_titles: ['能力如何落到执行'],
    title_style: ['能力如何落到执行'],
    text_style: ['先界定问题，再逐层展开方案。'],
    diagram_style: ['PlantUML 组件图：用箭头标注调用动作。'],
    structure_pattern: ['问题与原因 → 方案设计 → 风险与验证'],
    writing_guidelines: ['基于此，相关角色开始验证关键路径。'],
  },
  example_document: '# 示例架构方案\n\n## 为什么需要调整\n\n先界定问题。\n\n## 方案如何落地\n\n再说明方案。\n\n## 风险与验证\n\n最后完成验证。',
  status: 'saved',
  installed: true,
  published: false,
  created_at: 1,
  updated_at: 2,
}

describe('创作 Agent 多轮 Loop', () => {
  beforeEach(() => {
    window.localStorage.clear()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  it('Chrome 扩展连接后默认开启浏览器爬虫，并把粘贴图片转成 @附件', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') {
        return Response.json({ connected: true, extension_version: '0.1.0' })
      }
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    const crawler = await screen.findByRole('menuitemcheckbox', { name: /浏览器爬虫/ })
    await waitFor(() => expect(crawler).toHaveAttribute('aria-checked', 'true'))
    expect(screen.queryByText('Chrome 扩展后台读取')).not.toBeInTheDocument()
    expect(screen.getByText('文件')).toBeInTheDocument()
    expect(screen.getByText('文件夹')).toBeInTheDocument()
    fireEvent.click(crawler)
    expect(crawler).toHaveAttribute('aria-checked', 'false')
    expect(window.localStorage.getItem('memory-bread_creation_browser_crawler_enabled')).toBe('false')

    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    const image = new File(['image-bytes'], '经营看板.png', { type: 'image/png' })
    fireEvent.paste(input, { clipboardData: { files: [image] } })

    await waitFor(() => expect(input).toHaveValue('@经营看板.png '))
    expect(screen.getByText('经营看板.png')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /预览参考/ })).not.toBeInTheDocument()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('输入法确认候选词时不启动创作', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '生成一份方案' } })
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input, { data: '方案' })
    const defaultAllowed = fireEvent.keyDown(input, {
      key: 'Enter',
      code: 'Enter',
      keyCode: 229,
      isComposing: false,
    })

    expect(defaultAllowed).toBe(true)
    await waitFor(() => {
      const agentCalls = fetchMock.mock.calls.filter(([request]) =>
        new URL(String(request)).pathname === '/api/creation/agent/run'
      )
      expect(agentCalls).toHaveLength(0)
    })
  })

  it('将模型决定的执行链路逐项换行展示', async () => {
    useAppStore.getState().setCreationDraft({
      sessionId: 'session-agent-test',
      conversation: [{
        id: 'message-routing-summary',
        role: 'user',
        content: '生成一份周报',
        createdAt: 1_720_000_000_000,
        runId: 'run-1',
      }],
      agentEvents: [event(
        'agent.completed',
        2,
        '已由模型决定执行链路：周报 Skill、记忆搜索 Tool、文档撰写 Agent',
      )] as any,
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    // 行标题为动作描述，展开后才显示逐项换行的执行链路。
    const routeRow = await screen.findByRole('button', { name: /已由模型决定执行链路/ })
    fireEvent.click(routeRow)
    const summary = screen.getByText('已由模型决定执行链路')
    const routeSummary = summary.closest('.creation-agent-route-summary')
    expect(routeSummary).not.toBeNull()
    expect(routeSummary?.querySelectorAll('.creation-agent-route-summary__steps > span')).toHaveLength(3)
    expect(routeSummary).toHaveTextContent('周报 Skill')
    expect(routeSummary).toHaveTextContent('记忆搜索')
    expect(routeSummary).not.toHaveTextContent('记忆搜索 Tool')
    expect(routeSummary).toHaveTextContent('文档撰写 Agent')
  })

  it('清理旧历史中的主 Agent 控制空壳并保留具体执行动作', async () => {
    useAppStore.getState().setCreationDraft({
      sessionId: 'session-agent-test',
      conversation: [{
        id: 'message-legacy-control-event',
        role: 'user',
        content: '生成一份周报',
        createdAt: 1_720_000_000_000,
        runId: 'run-1',
      }],
      agentEvents: [
        event('agent.started', 1, '创作 Agent 开始执行'),
        event('agent.started', 2, '创作 Agent 正在组装周报章节'),
        event(
          'agent.started',
          3,
          '文档撰写 Agent 开始执行',
          { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
        ),
      ] as any,
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    const trace = await screen.findByLabelText('Agent 执行情况')
    expect(trace).not.toHaveTextContent('创作 Agent 开始执行')
    expect(trace).toHaveTextContent('创作 Agent 正在组装周报章节')
    expect(trace).toHaveTextContent('文档撰写 Agent 开始执行')

    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    const capabilityLabels = Array.from(
      trace.querySelectorAll('.creation-agent-event__capability'),
    ).map(node => node.textContent)
    expect(capabilityLabels).toContain('创作 Agent')
    expect(capabilityLabels).not.toContain('创作 Agent · Agent')
    expect(capabilityLabels).toContain('文档撰写 Agent · Agent')
  })

  it('在执行轨迹中展示后台浏览器缩略预览且完成后保留最终截图', async () => {
    const browserTool = { kind: 'tool', id: 'webpage_scrape', name: '网页爬取 Tool' }
    const previewId = '2d870d80-e2a2-4424-a732-069e174f2796'
    const started = event(
      'browser.preview.started',
      3,
      '已在后台打开 1 个数据页面，前台操作不会被切走',
      browserTool,
      {
        previews: [{
          id: previewId,
          source_id: 7,
          title: '经营看板',
          image_url: `/api/creation/browser-previews/${previewId}/image`,
        }],
      },
    )
    const completed = event(
      'browser.preview.completed',
      4,
      '后台页面采集已结束，缩略预览保留在执行记录中',
      browserTool,
      {
        previews: [{
          id: previewId,
          source_id: 7,
          title: '经营看板',
          image_url: `/api/creation/evidence/${previewId}/image`,
          status: 'completed',
          browser: 'chrome',
          interaction_mode: 'background_browser_window',
        }],
      },
    )
    useAppStore.getState().setCreationDraft({
      sessionId: 'session-agent-test',
      conversation: [{
        id: 'message-preview',
        role: 'user',
        content: '生成经营周报',
        createdAt: 1_720_000_000_000,
        runId: 'run-1',
      }],
      agentEvents: [started, completed] as any,
    })
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<CreationPanel />)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))

    // 已完成的动作默认折叠为一行，点开后才能看到浏览器缩略预览。
    fireEvent.click(screen.getByRole('button', { name: /后台页面采集已结束/ }))

    const image = screen.getByRole('img', { name: '经营看板网页证据缩略图' })
    expect(image).toHaveAttribute(
      'src',
      `http://localhost:7070/api/creation/evidence/${previewId}/image`,
    )
    expect(screen.getByText('采集完成')).toBeInTheDocument()
    expect(screen.getByText('chrome · 完整长图 · 点击查看')).toBeInTheDocument()
    expect(screen.getByTitle('打开完整网页长截图')).toHaveAttribute(
      'href',
      expect.stringContaining('/api/creation/evidence/'),
    )
    expect(screen.getAllByRole('img', { name: '经营看板网页证据缩略图' })).toHaveLength(1)
  })

  it('在当前 Agent 执行行下方展示浏览器实时画面', async () => {
    window.localStorage.setItem('memory-bread_creation_browser_crawler_enabled', 'true')
    useAppStore.getState().setCreationDraft({
      sessionId: 'session-agent-test',
      conversation: [{
        id: 'message-browser-live',
        role: 'user',
        content: '读取经营看板',
        createdAt: 1_720_000_000_000,
        runId: 'run-1',
      }],
      agentEvents: [event(
        'tool.started',
        1,
        '正在读取经营看板',
        { kind: 'tool', id: 'webpage_scrape', name: '网页读取 Tool' },
      )] as any,
    })
    const jobId = '57a36a47-9594-401d-ac34-1592be3bcfc6'
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/browser-integration/status') {
        return Response.json({
          connected: true,
          active_job_count: 1,
          queued_job_count: 0,
          jobs: [{
            browser_job_id: jobId,
            url: 'https://example.com/dashboard',
            title: '经营看板',
            status: 'running',
            stage: 'reading',
            updated_at: Date.now(),
            has_preview: true,
            preview_revision: 4,
          }],
        })
      }
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    const liveDock = await screen.findByLabelText('浏览器现场')
    const activeLine = screen.getByText('正在读取经营看板').closest('.creation-agent-event')
    expect(activeLine).toContainElement(liveDock)
    expect(screen.getByLabelText('生成内容')).not.toContainElement(liveDock)
    expect(liveDock).toHaveTextContent('读取页面数据')
    expect(liveDock).not.toHaveTextContent('后台标签 · 不抢焦点')
    expect(within(liveDock).getByText('经营看板')).toHaveClass('creation-browser-live__title')
    expect(screen.getByRole('img', { name: '经营看板后台浏览器实时画面' })).toHaveAttribute(
      'src',
      `http://localhost:7070/api/browser-integration/jobs/${jobId}/preview?revision=4`,
    )
  })

  it('为运行中的 Agent、Tool 和 Skill 展示呼吸状态灯', async () => {
    useAppStore.getState().setCreationDraft({
      sessionId: 'session-agent-test',
      conversation: [{
        id: 'message-running-capabilities',
        role: 'user',
        content: '生成一份产品方案',
        createdAt: 1_720_000_000_000,
        runId: 'run-1',
      }],
      agentEvents: [
        event('agent.started', 1, '创作 Agent 正在规划'),
        event(
          'tool.started',
          2,
          '记忆搜索 Tool 正在检索',
          { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
        ),
        event(
          'skill.started',
          3,
          '产品方案 Skill 正在执行',
          { kind: 'skill', id: 'product-plan', name: '产品方案 Skill' },
        ),
      ] as any,
    })
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<CreationPanel />)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    const trace = screen.getByLabelText('Agent 执行情况')
    const runningEvents = trace.querySelectorAll('.creation-agent-event.is-running')
    expect(runningEvents).toHaveLength(3)
    runningEvents.forEach((item) => {
      expect(item.querySelector('.creation-agent-event__activity')).toBeInTheDocument()
      expect(item.querySelector('.spin')).not.toBeInTheDocument()
    })
  })

  it('开始创作后展示 Agent、Tool、Skill 轨迹，并基于当前文档继续多轮优化', async () => {
    const agentPayloads: any[] = []
    const savedHistories: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([installedStyleSkill])
      if (url.pathname === '/api/data/sources/7') {
        return Response.json({
          id: 7,
          title: '经营看板',
          source_kind: 'report_url',
          access_mode: 'direct_http',
          refresh_policy: 'on_demand',
          realtime_level: 'live',
          tags: [],
          first_seen_at: 1,
          last_seen_at: 2,
          status: 'active',
          latest_snapshot: {
            id: 70,
            source_id: 7,
            collected_at: 1_720_000_000_000,
            observed_at: 1_720_000_000_000,
            collector: 'http',
            content_text: '国内单位 Token 成本为 0.018 元。',
            structured_data: {
              title: '单位 Token 成本对比',
              summary: '成本优化后，国内单位 Token 成本下降 28%。',
              metric_rows: [{
                dimension: '国内',
                metric: '单位 Token 成本',
                value: '0.018 元',
                note: '较基线下降 28%',
              }],
            },
            content_hash: 'hash-7',
            freshness_ttl_seconds: 3600,
            provenance: {},
            source_capture_ids: [],
            source_timeline_ids: [],
            status: 'active',
          },
        })
      }
      if (url.pathname === '/api/data/sources/9') {
        return Response.json({
          id: 9,
          title: '历史经营周报',
          source_kind: 'work_memory',
          access_mode: 'memory_only',
          refresh_policy: 'never',
          realtime_level: 'observed',
          tags: [],
          first_seen_at: 1,
          last_seen_at: 2,
          status: 'active',
          latest_snapshot: null,
        })
      }
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 1 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const payload = JSON.parse(String(init?.body || '{}'))
        agentPayloads.push(payload)
        const secondTurn = agentPayloads.length === 2
        const document = secondTurn
          ? '# Agent 架构方案\n\n## 目标\n\n**目标驱动**并支持持续优化。\n\n## Loop\n\n本轮补充质量门禁。\n\n| 能力 | 职责 |\n| --- | --- |\n| Harness | 动态调用能力 |\n| Reviewer | 反馈质量问题 |\n\n## 质量门禁\n\n修订必须通过结构和语义检查。\n\n## 验证\n\n覆盖多轮对话和联动修改。'
          : '# Agent 架构方案\n\n## 目标\n\n**目标驱动**。\n\n## Loop\n\n动态调用能力。\n\n| 能力 | 职责 |\n| --- | --- |\n| Harness | 动态规划 |\n\n## 验证\n\n覆盖完整链路。'
        const offset = secondTurn ? 20 : 1
        const mutationEvent = secondTurn
          ? {
            ...event(
              'document.patch.applied',
              offset + 6,
              '已按本轮指令完成 3 处调整，涉及目标、Loop、质量门禁、验证',
              { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
              {
                content: document,
                patch: {
                  operation: 'revise_document',
                  target_sections: ['目标', 'Loop', '质量门禁', '验证'],
                  requested_sections: ['质量门禁'],
                  change_count: 3,
                  changes: [
                    {
                      change_type: 'modified',
                      section_title: 'Loop',
                      start_line: 9,
                      end_line: 9,
                      summary: '修改“Loop”中的内容',
                    },
                    {
                      change_type: 'added',
                      section_title: '质量门禁',
                      start_line: 11,
                      end_line: 13,
                      summary: '新增“质量门禁”中的内容',
                    },
                    {
                      change_type: 'modified',
                      section_title: '验证',
                      start_line: 15,
                      end_line: 17,
                      summary: '修改“验证”中的内容',
                    },
                  ],
                  preserved_untouched: true,
                  summary: '已按本轮指令完成 3 处调整，涉及 Loop、质量门禁、验证',
                },
              },
            ),
            status: 'completed',
          }
          : event(
            'document.replaced',
            offset + 6,
            '文档撰写 Agent 已提交完整文档版本',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
            { content: document, operation: 'create_document' },
          )
        return sse([
          event('run.started', offset, '创作 Agent 已接管目标'),
          event(
            'intent.interpreted',
            offset + 1,
            secondTurn ? '理解为围绕“质量门禁”联动修订完整文档' : '理解为新建完整文档',
            undefined,
            {
              operation: secondTurn ? 'revise_document' : 'create_document',
              target_sections: secondTurn ? ['质量门禁'] : [],
              root_request: '设计创作功能的 Agent 架构方案',
              current_instruction: secondTurn ? '补充质量门禁和多轮测试' : '设计创作功能的 Agent 架构方案',
              reasoning_summary: secondTurn
                ? '目标章节仅作为改动线索，并检查全文联动。'
                : '当前没有既有文档。',
            },
          ),
          event(
            'tool.completed',
            offset + 2,
            '记忆搜索完成，召回 2 条本地资料',
            { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
            {
              result_count: 2,
              skill_step_title: 'AIGC进度总结',
              query: '当前步骤：AIGC进度总结 用@记忆搜索 Tool 工具获取本周AIGC共建项目的进展，以及AIGC共建项目周报里关于推理性能优化相关的进展，并以列表形式展示，最多10行文字\n整体创作背景：请使用@GPU成本优化周报创作法完成本周周报',
            },
            {
              references: [{
                id: 8,
                source_id: 2285,
                source_type: 'knowledge',
                title: '既有架构决策',
                doc_type: '架构方案',
                final_weight: 0.88,
                relevance_score: 0.9,
                quality_score: 0.8,
                completeness_score: 0.8,
                usage_score: 0.5,
                format_score: 0.7,
                freshness_score: 0.9,
                usage_count: 3,
                reason: '主题高度相关',
              }, {
                id: 60,
                source_id: 60,
                source_type: 'data',
                title: '会议录制: GPU 成本优化专项周会',
                doc_type: 'data',
                final_weight: 0.72,
                relevance_score: 0.7,
                quality_score: 0.9,
                completeness_score: 0.97,
                usage_score: 0,
                format_score: 0.55,
                freshness_score: 0.8,
                usage_count: 0,
                reason: '命中关键词：GPU、成本优化',
              }],
            },
          ),
          event(
            'tool.completed',
            offset + 2.1,
            '数据检索完成，召回 2 个来源，其中 1 个需要刷新',
            { kind: 'tool', id: 'data_search', name: '数据检索 Tool' },
            { result_count: 2, refresh_required_count: 1 },
            {
              data_sources: [
                {
                  source_id: 7,
                  title: '经营看板',
                  source_kind: 'report_url',
                  freshness_class: 'fresh',
                  refresh_required: false,
                  can_use: true,
                  ignored_secret: '不会写入历史',
                },
                {
                  source_id: 9,
                  title: '历史经营周报',
                  source_kind: 'memory_snapshot',
                  freshness_class: 'stale',
                  refresh_required: true,
                  can_use: true,
                },
              ],
            },
          ),
          {
            ...event(
              'harness.decision',
              offset + 3,
              'Harness 根据 data_search 反馈跳过不必要的数据步骤',
              undefined,
              {
                trigger: 'data_search',
                trigger_status: 'completed',
                reason_code: 'no_matching_data',
                result_count: 0,
                refreshable_count: 0,
                analyzable_count: 0,
                scheduled: [],
                error_code: null,
              },
            ),
            status: 'completed',
          },
          event(
            'skill.completed',
            offset + 4,
            '已把架构方案模板 Skill 写入环境',
            { kind: 'skill', id: 'architecture-solution-template', name: '架构方案模板 Skill' },
          ),
          event(
            'agent.completed',
            offset + 5,
            '方案设计 Agent 已完成',
            { kind: 'agent', id: 'solution_design_agent', name: '方案设计 Agent' },
          ),
          mutationEvent,
          event(
            'agent.started',
            offset + 7,
            '质量审校 Agent 开始执行',
            { kind: 'agent', id: 'quality_review_agent', name: '质量审校 Agent' },
          ),
          {
            ...event(
              'agent.completed',
              offset + 8,
              '质量检查通过',
              { kind: 'agent', id: 'quality_review_agent', name: '质量审校 Agent' },
            ),
            status: 'completed',
          },
          {
            ...event(
              'harness.decision',
              offset + 9,
              '质检通过，Harness 结束本轮优化循环',
              undefined,
              {
                trigger: 'quality_review_agent',
                trigger_status: 'completed',
                reason_code: 'quality_gate_passed',
                issue_count: 0,
                issue_codes: [],
                scheduled: [],
              },
            ),
            status: 'completed',
          },
          {
            ...event('run.completed', offset + 10, '本轮创作完成'),
            status: 'completed',
          },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    const divider = screen.getByRole('separator', { name: '调整生成内容和创作对话的宽度' })
    const workspace = divider.closest('main') as HTMLElement
    vi.spyOn(workspace, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      width: 1000,
      height: 700,
      top: 0,
      right: 1000,
      bottom: 700,
      left: 0,
      toJSON: () => ({}),
    })
    expect(workspace.style.getPropertyValue('--creation-left-pane')).toBe('60%')
    fireEvent.keyDown(divider, { key: 'ArrowLeft' })
    expect(workspace.style.getPropertyValue('--creation-left-pane')).toBe('58%')

    await screen.findByRole('button', { name: '技能 (1)' })
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    // 自动推荐已下线，技能必须通过显式 @ 引入。
    fireEvent.change(input, { target: { value: '@Agent 架构方案风格 设计创作功能的 Agent 架构方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    await screen.findByRole('heading', { name: 'Agent 架构方案' })
    expect(screen.getByLabelText('用户消息')).toHaveTextContent('设计创作功能')
    expect(screen.getByLabelText('Agent 执行情况')).toHaveTextContent('架构方案模板 Skill')
    expect(screen.getByLabelText('Agent 执行情况')).toHaveTextContent('方案设计 Agent')
    // 细节默认折叠，展开全部后才可见能力描述、判断摘要等详情。
    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    const capabilities = Array.from(
      screen.getByLabelText('Agent 执行情况').querySelectorAll('.creation-agent-event__capability'),
    )
    const memoryCapability = capabilities.find(node => node.textContent?.includes('记忆搜索'))
    expect(memoryCapability?.textContent).toContain('Tool')
    expect(screen.getByLabelText('Agent 执行情况')).toHaveTextContent('原始需求')
    expect(screen.getByLabelText('Agent 执行情况')).toHaveTextContent('当前没有既有文档')

    const memoryResultLink = screen.getByRole('button', { name: '召回 2 条本地资料，打开参考资料' })
    const scrollIntoView = vi.fn()
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView
    HTMLElement.prototype.scrollIntoView = scrollIntoView
    fireEvent.click(memoryResultLink)
    expect(screen.getByRole('tab', { name: '参考资料 (2)' })).toHaveAttribute('aria-selected', 'true')
    const referenceGroup = screen.getByLabelText('AIGC进度总结参考资料组')
    await waitFor(() => expect(referenceGroup).toHaveFocus())
    expect(referenceGroup.querySelector('.creation-reference-group__query')).toHaveTextContent(
      '当前步骤：AIGC进度总结 用@记忆搜索 Tool 工具获取本周AIGC共建项目的进展，以及AIGC共建项目周报里关于推理性能优化相关的进展，并以列表形式展示，最多10行文字 整体创作背景：请使用@GPU成本优化周报创作法完成本周周报',
    )
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    HTMLElement.prototype.scrollIntoView = originalScrollIntoView
    expect(screen.getByText('既有架构决策')).toBeInTheDocument()
    const openedReferences: Array<Record<string, unknown>> = []
    const handleReferenceOpen = (event: Event) => {
      openedReferences.push((event as CustomEvent<Record<string, unknown>>).detail)
    }
    window.addEventListener('view-rag-reference', handleReferenceOpen)
    const sourceButtons = within(referenceGroup).getAllByRole('button', { name: '资料来源' })
    fireEvent.click(sourceButtons[0])
    expect(openedReferences).toContainEqual(expect.objectContaining({
      type: 'bake_knowledge',
      artifactId: 2285,
    }))
    // 数据记忆域的参考资料没有外链时，也必须能跳转到记忆力的数据页，
    // 不能因为缺少 source_url 就静默无响应。
    fireEvent.click(sourceButtons[1])
    expect(openedReferences).toContainEqual(expect.objectContaining({
      type: 'data',
      dataSourceId: 60,
    }))
    window.removeEventListener('view-rag-reference', handleReferenceOpen)

    const dataResultLink = screen.getByRole('button', { name: '召回 2 个来源，打开参考数据' })
    fireEvent.click(dataResultLink)
    expect(screen.getByRole('tab', { name: '参考数据 (2)' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('经营看板')).toBeInTheDocument()
    expect(screen.getByText('历史经营周报')).toBeInTheDocument()
    expect(await screen.findByText('单位 Token 成本对比')).toBeInTheDocument()
    expect(screen.getByText('成本优化后，国内单位 Token 成本下降 28%。')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '指标' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: '单位 Token 成本' })).toBeInTheDocument()
    expect(screen.getByText('0.018 元')).toBeInTheDocument()
    expect(screen.getByText('较基线下降 28%')).toBeInTheDocument()
    expect(screen.getByText('该来源尚未采集到数据快照。')).toBeInTheDocument()
    expect(screen.getByText('需要刷新')).toBeInTheDocument()

    const followUp = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    fireEvent.change(followUp, { target: { value: '补充质量门禁和多轮测试' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => expect(agentPayloads).toHaveLength(2))
    expect(agentPayloads[0].selected_skills[0]).toMatchObject({
      workflowRole: 'primary',
      strictStructure: true,
      titleDesignStyle: installedStyleSkill.common_titles,
      writingDesign: installedStyleSkill.text_style,
      imageGeneration: installedStyleSkill.diagram_style,
      voiceStyle: installedStyleSkill.writing_guidelines,
      fieldExamples: {
        titleDesignStyle: installedStyleSkill.field_examples.common_titles,
        voiceStyle: installedStyleSkill.field_examples.writing_guidelines,
      },
    })
    expect(agentPayloads[0].selected_skills[0].skillInstructions).toContain('## 执行工作流')
    expect(agentPayloads[0].selected_skills[0]).not.toHaveProperty('exampleDocument')
    expect(agentPayloads[1].current_document).toContain('动态调用能力')
    expect(agentPayloads[1].root_request).toBe('设计创作功能的 Agent 架构方案')
    expect(agentPayloads[1].conversation.map((item: any) => item.role)).toEqual([
      'user',
      'assistant',
      'user',
    ])
    await screen.findByText('本轮补充质量门禁。')
    const emphasized = screen.getByText('目标驱动')
    expect(emphasized.tagName).toBe('STRONG')
    expect(emphasized).toHaveStyle({ color: 'var(--mb-brand-strong)', textDecoration: 'underline' })
    expect(screen.getByRole('columnheader', { name: '能力' })).toHaveStyle({
      background: 'var(--mb-brand-soft)',
      color: 'var(--mb-brand-text)',
    })
    expect(screen.getByText('本轮补充质量门禁。')).toHaveClass('creation-latest-change')
    expect(screen.getByText(/本轮改动 3 处/)).toBeInTheDocument()
    expect(screen.getByLabelText('本轮改动')).toHaveTextContent('修改 · Loop')
    expect(screen.getByLabelText('本轮改动')).toHaveTextContent('新增 · 质量门禁')
    const userMessages = screen.getAllByLabelText('用户消息')
    const assistantMessages = screen.getAllByLabelText('Agent 消息')
    const executionTraces = screen.getAllByLabelText('Agent 执行情况')
    expect(executionTraces).toHaveLength(2)
    fireEvent.click(within(executionTraces[1]).getByRole('button', { name: '展开全部' }))
    expect(executionTraces[0]).toHaveTextContent('当前没有既有文档')
    expect(executionTraces[0]).not.toHaveTextContent('目标章节仅作为改动线索')
    expect(executionTraces[1]).toHaveTextContent('目标章节仅作为改动线索')
    expect(executionTraces[1]).not.toHaveTextContent('当前没有既有文档')
    expect(executionTraces[1]).toHaveTextContent('创作 Agent')
    expect(executionTraces[1]).not.toHaveTextContent('创作主 Agent')
    const round2Capabilities = Array.from(
      executionTraces[1].querySelectorAll('.creation-agent-event__capability'),
    )
    expect(round2Capabilities.some(node => (
      node.textContent?.includes('数据检索') && node.textContent?.includes('Tool')
    ))).toBe(true)
    expect(executionTraces[1]).toHaveTextContent('没有找到匹配的数据来源')
    expect(executionTraces[1]).toHaveTextContent('已根据反馈保留当前处理计划')
    expect(executionTraces[1]).not.toHaveTextContent('Harness 根据 data_search')
    expect(executionTraces[1]).not.toHaveTextContent('no_matching_data')
    expect(executionTraces[1]).toHaveTextContent('质量审校 Agent · 已完成')
    expect(executionTraces[1]).toHaveTextContent('质量要求已满足')
    expect(executionTraces[1]).toHaveTextContent('质量检查通过')
    expect(executionTraces[1]).not.toHaveTextContent('Harness 结束本轮优化循环')
    expect(executionTraces[1]).not.toHaveTextContent('quality_review_agent')
    expect(executionTraces[1]).not.toHaveTextContent('quality_gate_passed')
    expect(executionTraces[1]).toHaveTextContent('追加能力')
    expect(executionTraces[1]).toHaveTextContent('无，继续现有计划')
    // 能力信息只在展开细节里出现：主 Agent 对应 3 个动作分组。
    expect(round2Capabilities.filter(node => node.textContent?.startsWith('创作 Agent'))).toHaveLength(3)
    const mainAgentStarted = within(executionTraces[1]).getByText('创作 Agent 已接管目标')
    const mainAgentIntent = within(executionTraces[1]).getByText('理解为围绕“质量门禁”联动修订完整文档')
    const mainAgentNode = mainAgentStarted.closest('.creation-agent-event')
    expect(mainAgentNode).toBe(mainAgentIntent.closest('.creation-agent-event'))
    expect(mainAgentNode?.querySelectorAll('.creation-agent-event__icon')).toHaveLength(1)
    expect(mainAgentNode?.querySelectorAll('.creation-agent-event__update')).toHaveLength(2)
    expect(round2Capabilities.filter(node => node.textContent?.startsWith('质量审校 Agent'))).toHaveLength(1)
    expect(userMessages[1].compareDocumentPosition(executionTraces[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(executionTraces[1].compareDocumentPosition(assistantMessages[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(within(executionTraces[1]).getByRole('button', { name: /执行过程/ }))
    expect(within(executionTraces[1]).queryByText('目标章节仅作为改动线索，并检查全文联动。')).not.toBeInTheDocument()
    expect(within(executionTraces[0]).getByText('当前没有既有文档。')).toBeInTheDocument()
    expect(savedHistories).toHaveLength(2)
    expect(savedHistories[0].session_id).toBe('session-agent-test')
    expect(savedHistories[1].session_id).toBe(savedHistories[0].session_id)
    expect(savedHistories[0].history_id).toBeNull()
    expect(savedHistories[1].history_id).toBe(1)
    expect(savedHistories[1].conversation).toHaveLength(4)
    expect(savedHistories[1].agent_trace.length).toBeGreaterThan(savedHistories[0].agent_trace.length)
    expect(savedHistories[1].root_request).toBe('设计创作功能的 Agent 架构方案')
    expect(savedHistories[1].edit_operation).toBe('revise_document')
    expect(savedHistories[1].document_patch.target_sections).toEqual(['目标', 'Loop', '质量门禁', '验证'])
    expect(savedHistories[1].document_patch.changes).toHaveLength(3)
    const storedIntent = savedHistories[1].agent_trace.find((item: any) => item.type === 'intent.interpreted')
    const storedPatch = savedHistories[1].agent_trace.find((item: any) => item.type === 'document.patch.applied')
    const storedHarnessDecision = savedHistories[1].agent_trace.find((item: any) => item.type === 'harness.decision')
    const storedDataSearch = savedHistories[1].agent_trace.find((item: any) => item.actor?.id === 'data_search')
    expect(storedDataSearch.environment_patch.data_sources).toEqual([
      {
        source_id: 7,
        title: '经营看板',
        source_kind: 'report_url',
        freshness_class: 'fresh',
        refresh_required: false,
        can_use: true,
      },
      {
        source_id: 9,
        title: '历史经营周报',
        source_kind: 'memory_snapshot',
        freshness_class: 'stale',
        refresh_required: true,
        can_use: true,
      },
    ])
    expect(storedDataSearch.data).toMatchObject({ result_count: 2, refresh_required_count: 1 })
    expect(storedIntent.data).not.toHaveProperty('root_request')
    expect(storedIntent.data).not.toHaveProperty('current_instruction')
    expect(storedPatch.data).not.toHaveProperty('content')
    expect(storedHarnessDecision.data).toEqual({
      trigger: 'data_search',
      trigger_status: 'completed',
      reason_code: 'no_matching_data',
      result_count: 0,
      refreshable_count: 0,
      analyzable_count: 0,
      scheduled: [],
      error_code: null,
    })
    expect(storedHarnessDecision.environment_patch).toEqual({})
    expect(savedHistories[1].agent_trace.every((item: any) => !item.goal?.objective)).toBe(true)
  })

  it('首版生成中的 Agent 内部润色仍属于首次创作，不展示本轮改动', async () => {
    const initialDocument = '# GPU 利用率治理方案\n\n## 背景\n\n初始内容。\n\n## 方案\n\n初始表述。'
    const polishedDocument = '# GPU 利用率治理方案\n\n## 背景\n\n首版内容更自然。\n\n## 方案\n\n完善首版表述。'
    const polishPatch = {
      operation: 'quality_polish:anti_ai_style_agent',
      target_sections: ['背景', '方案'],
      change_count: 2,
      changes: [{
        change_type: 'modified',
        section_title: '背景',
        start_line: 5,
        end_line: 5,
        summary: '修改“背景”中的内容',
      }],
      summary: '已按本轮指令完成 2 处调整',
    }
    const savedHistories: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 33 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event(
            'intent.interpreted',
            2,
            '理解为新建文档，将按完整需求生成首版内容',
            undefined,
            { operation: 'create_document' },
          ),
          event(
            'document.replaced',
            3,
            '文档撰写 Agent 已提交完整文档版本',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
            { content: initialDocument, operation: 'create_document' },
          ),
          {
            ...event(
              'document.patch.applied',
              4,
              '去 AI 味 Agent 已应用内部润色',
              { kind: 'agent', id: 'anti_ai_style_agent', name: '去 AI 味 Agent' },
              { content: polishedDocument, patch: polishPatch },
            ),
            status: 'completed',
          },
          {
            ...event(
              'run.completed',
              5,
              '本轮创作完成',
              undefined,
              { document: polishedDocument },
            ),
            status: 'completed',
          },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '创作一份治理 GPU 利用率的方案文档' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    const polishedText = await screen.findByText('首版内容更自然。')
    await waitFor(() => expect(savedHistories).toHaveLength(1))

    expect(polishedText).not.toHaveClass('creation-latest-change')
    expect(screen.queryByLabelText('本轮改动')).not.toBeInTheDocument()
    expect(screen.queryByText(/本轮改动 2 处/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('Agent 消息')).toHaveTextContent('首版文档已生成')
    const fullscreenButton = screen.getByRole('button', { name: '全屏查看文档' })
    fireEvent.click(fullscreenButton)
    expect(screen.getByLabelText('生成内容')).toHaveClass('creation-panel-fullscreen')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByLabelText('生成内容')).not.toHaveClass('creation-panel-fullscreen')
    await waitFor(() => expect(fullscreenButton).toHaveFocus())
    expect(savedHistories[0].edit_operation).toBe('create_document')
    expect(savedHistories[0].document_patch).toEqual(polishPatch)
    expect(savedHistories[0].agent_trace).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'document.patch.applied' }),
    ]))
  })

  it('严格 Skill 步骤生成时实时展示临时文档且不把预览写入历史轨迹', async () => {
    const encoder = new TextEncoder()
    const savedHistories: any[] = []
    let releaseFinal: (() => void) | undefined
    const previewDocument = '# GPU成本优化周报\n\n## AIGC进度总结\n\n- 推理性能优化已完成首轮验证。'
    const finalDocument = `${previewDocument}\n\n## GPU算力数据\n\n总卡数为 1803.59 卡。`
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 91 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(
              `data: ${JSON.stringify(event(
                'document.preview',
                1,
                '正在生成「AIGC进度总结」内容',
                undefined,
                {
                  content: previewDocument,
                  section_title: 'AIGC进度总结',
                  progress_chars: 28,
                },
              ))}\n\n`,
            ))
            releaseFinal = () => {
              controller.enqueue(encoder.encode(
                `data: ${JSON.stringify({
                  ...event('run.completed', 2, '本轮创作完成', undefined, { document: finalDocument }),
                  status: 'completed',
                })}\n\n`,
              ))
              controller.close()
            }
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '请生成本周 GPU 成本优化周报' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    await waitFor(() => expect(
      useAppStore.getState().creationDraft.generatedContent,
    ).toContain('推理性能优化已完成首轮验证。'))
    expect(savedHistories).toHaveLength(0)
    releaseFinal?.()

    await waitFor(() => expect(
      useAppStore.getState().creationDraft.generatedContent,
    ).toContain('总卡数为 1803.59 卡。'))
    await waitFor(() => expect(savedHistories).toHaveLength(1))
    expect(savedHistories[0].agent_trace).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ type: 'document.preview' })]),
    )
  })

  it('浏览器即时刷新完成后重新读取来源快照并保存最新可用状态', async () => {
    const sourceTitle = '电商GPU信息平台 - GPU项目用量管理'
    const browserTool = { kind: 'tool', id: 'webpage_scrape', name: '网页爬取 Tool' }
    const savedHistories: any[] = []
    let sourceRequestCount = 0
    let releaseScrape!: () => void
    const encoder = new TextEncoder()

    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 44 })
      }
      if (url.pathname === '/api/data/sources/1584') {
        sourceRequestCount += 1
        return Response.json({
          id: 1584,
          title: sourceTitle,
          source_kind: 'report_url',
          access_mode: 'browser_session',
          refresh_policy: 'on_demand',
          realtime_level: 'live',
          tags: [],
          first_seen_at: 1,
          last_seen_at: 2,
          status: 'active',
          latest_snapshot: sourceRequestCount > 1 ? {
            id: 6978,
            source_id: 1584,
            collected_at: 1_786_271_177_838,
            observed_at: 1_786_271_177_838,
            collector: 'browser_attach',
            content_text: '总卡数（X40 折算）为 1803.59 卡。',
            structured_data: {
              title: 'GPU 项目用量',
              summary: '后台浏览器已完成即时采集。',
              metric_rows: [{
                dimension: '电商 GPU 项目',
                metric: '总卡数（X40 折算）',
                value: '1803.59 卡',
                note: '即时快照',
              }],
            },
            content_hash: 'snapshot-6978',
            freshness_ttl_seconds: 3600,
            provenance: {},
            source_capture_ids: [],
            source_timeline_ids: [],
            status: 'active',
          } : null,
        })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const firstEvents = [
          event('run.started', 1, '创作 Agent 已接管目标'),
          event(
            'tool.completed',
            2,
            '数据检索完成，召回 1 个来源，其中 1 个需要刷新',
            { kind: 'tool', id: 'data_search', name: '数据检索 Tool' },
            { result_count: 1, refresh_required_count: 1 },
            {
              data_sources: [{
                source_id: 1584,
                title: sourceTitle,
                source_kind: 'report_url',
                freshness_class: 'missing',
                refresh_required: true,
                can_use: false,
              }],
            },
          ),
        ]
        const finalDocument = '# GPU成本优化周报\n\n总卡数（X40 折算）为 1803.59 卡。'
        const finalEvents = [
          event(
            'tool.completed',
            3,
            '浏览器访问 1 个报表，1 个来源的数据与截图通过校验',
            browserTool,
            { result_count: 1, failed_count: 0 },
            {
              data_sources: [{
                source_id: 1584,
                title: sourceTitle,
                source_kind: 'report_url',
                freshness_class: 'fresh',
                refresh_required: false,
                can_use: true,
              }],
            },
          ),
          event(
            'document.replaced',
            4,
            '创作 Agent 已提交完整文档版本',
            undefined,
            { content: finalDocument, operation: 'create_document' },
          ),
          {
            ...event('run.completed', 5, '本轮创作完成', undefined, { document: finalDocument }),
            status: 'completed',
          },
        ]
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(
              firstEvents.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
            ))
            releaseScrape = () => {
              controller.enqueue(encoder.encode(
                finalEvents.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
              ))
              controller.close()
            }
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '请使用 GPU 成本周报技能创作本周周报' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    // 参考数据链接在展开细节里：先点开数据检索动作行。
    const dataRow = await screen.findByRole('button', { name: /召回 1 个来源/ })
    fireEvent.click(dataRow)
    const dataResultLink = await screen.findByRole('button', { name: '召回 1 个来源，打开参考数据' })
    fireEvent.click(dataResultLink)
    expect(await screen.findByText('该来源尚未采集到数据快照。')).toBeInTheDocument()
    expect(screen.getByText('需要刷新')).toBeInTheDocument()

    releaseScrape()

    const refreshedSummaries = await screen.findAllByText('后台浏览器已完成即时采集。')
    expect(refreshedSummaries).toHaveLength(2)
    expect(screen.getByLabelText('数据检索参考数据组')).toBeInTheDocument()
    const scrapeGroup = screen.getByLabelText('网页即时采集参考数据组')
    expect(scrapeGroup).toHaveTextContent('网页爬取')
    expect(within(scrapeGroup).getByRole('rowheader', { name: '总卡数（X40 折算）' })).toBeInTheDocument()
    expect(within(scrapeGroup).getByText('1803.59 卡')).toBeInTheDocument()
    expect(screen.queryByText('当前可用')).not.toBeInTheDocument()
    expect(screen.queryByText('可用于创作')).not.toBeInTheDocument()
    expect(within(scrapeGroup).queryByText('该来源尚未采集到数据快照。')).not.toBeInTheDocument()
    await waitFor(() => expect(savedHistories).toHaveLength(1))
    expect(sourceRequestCount).toBe(2)
    const storedScrape = savedHistories[0].agent_trace.find(
      (item: any) => item.type === 'tool.completed' && item.actor?.id === 'webpage_scrape',
    )
    expect(storedScrape.environment_patch.data_sources).toEqual([{
      source_id: 1584,
      title: sourceTitle,
      source_kind: 'report_url',
      freshness_class: 'fresh',
      refresh_required: false,
      can_use: true,
    }])
  })

  it('同一次创作多次召回数据时在参考数据中展示全部来源', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 45 })
      }
      if (url.pathname === '/api/data/sources/21' || url.pathname === '/api/data/sources/22') {
        const sourceId = Number(url.pathname.split('/').pop())
        return Response.json({
          id: sourceId,
          title: sourceId === 21 ? '销售经营看板' : '渠道转化周报',
          source_kind: 'report_url',
          access_mode: 'direct_http',
          refresh_policy: 'on_demand',
          realtime_level: 'live',
          tags: [],
          first_seen_at: 1,
          last_seen_at: 2,
          status: 'active',
          latest_snapshot: null,
        })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const document = '# 经营复盘\n\n已汇总销售和渠道数据。'
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event(
            'tool.completed',
            2,
            '第一次数据检索完成，召回 1 个来源',
            { kind: 'tool', id: 'data_search', name: '数据检索 Tool' },
            { result_count: 1 },
            { data_sources: [{
              source_id: 21,
              title: '销售经营看板',
              source_kind: 'report_url',
              freshness_class: 'fresh',
              refresh_required: false,
              can_use: true,
            }] },
          ),
          event(
            'tool.completed',
            3,
            '第二次数据检索完成，召回 1 个来源',
            { kind: 'tool', id: 'data_search', name: '数据检索 Tool' },
            { result_count: 1 },
            { data_sources: [{
              source_id: 22,
              title: '渠道转化周报',
              source_kind: 'memory_snapshot',
              freshness_class: 'recent',
              refresh_required: false,
              can_use: true,
            }] },
          ),
          event(
            'document.replaced',
            4,
            '创作 Agent 已提交完整文档版本',
            undefined,
            { content: document, operation: 'create_document' },
          ),
          { ...event('run.completed', 5, '本轮创作完成', undefined, { document }), status: 'completed' },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '汇总经营数据' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    const dataTab = await screen.findByRole('tab', { name: '参考数据 (2)' })
    fireEvent.click(dataTab)

    expect(await screen.findByText('销售经营看板')).toBeInTheDocument()
    expect(screen.getByText('渠道转化周报')).toBeInTheDocument()
    expect(screen.queryByText('当前可用')).not.toBeInTheDocument()
    expect(screen.queryByText('可用于创作')).not.toBeInTheDocument()
  })

  it('明确展示证据未通过原因且不把快照标成当前可用', async () => {
    useAppStore.getState().setCreationDraft({
      generatedContent: '# GPU成本优化周报',
      dataReferences: [{
        source_id: 1584,
        title: '电商GPU信息平台 - GPU项目用量管理',
        source_kind: 'report_url',
        freshness_class: 'unverified',
        refresh_required: true,
        can_use: false,
        evidence_status: 'rejected',
        evidence_reason: 'no_verified_metric',
        unavailable_reason: 'evidence_rejected',
      }],
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/data/sources/1584') {
        return Response.json({
          id: 1584,
          title: '电商GPU信息平台 - GPU项目用量管理',
          source_kind: 'report_url',
          access_mode: 'browser_session',
          refresh_policy: 'on_demand',
          realtime_level: 'live',
          tags: [],
          first_seen_at: 1,
          last_seen_at: 2,
          status: 'active',
          latest_snapshot: {
            id: 6978,
            source_id: 1584,
            collected_at: 1_786_273_349_226,
            observed_at: 1_786_273_349_226,
            collector: 'browser_attach',
            content_text: '在用项目数 102，总卡数 1803.59。',
            structured_data: { title: 'GPU 项目用量', summary: '页面 DOM 已采集。' },
            content_hash: 'snapshot-6978',
            freshness_ttl_seconds: 3600,
            provenance: {},
            source_capture_ids: [],
            source_timeline_ids: [],
            status: 'active',
          },
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('tab', { name: '参考数据 (1)' }))

    expect(await screen.findByText('已采集，证据未通过')).toBeInTheDocument()
    expect(screen.getByText('截图中未识别到可核验指标，该快照不会用于本轮创作。')).toBeInTheDocument()
    expect(screen.getByText('证据未通过')).toBeInTheDocument()
    expect(screen.queryByText('当前可用')).not.toBeInTheDocument()
  })

  it('把校验通过的即时截图显示在引用处并写入创作历史', async () => {
    const savedHistories: any[] = []
    const evidence = {
      id: 'evidence-1',
      source_url: 'https://bi.example.com/dashboard/gpu',
      page_title: 'GPU 实时看板',
      captured_at: 1_770_000_000_000,
      image_url: '/api/creation/evidence/evidence-1/image',
      validation_status: 'verified',
      validation: {
        verified_claims: [{ label: '国内 GPU 利用率', value: '42%' }],
      },
    }
    const document = [
      '# GPU 治理方案',
      '',
      '国内 GPU 利用率为 42%。',
      '',
      '![证据截图：GPU 实时看板](/api/creation/evidence/evidence-1/image)',
      '',
      '> 证据截图 · 来源：[GPU 实时看板](<https://bi.example.com/dashboard/gpu>) · 页面数据与截图文字已通过一致性校验',
    ].join('\n')
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 42 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event(
            'document.replaced',
            2,
            '文档撰写 Agent 已提交完整文档版本',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
            { content: '# GPU 治理方案\n\n国内 GPU 利用率为 42%。', operation: 'create_document' },
          ),
          {
            ...event('document.evidence.applied', 3, '已应用即时截图', undefined, { content: document, evidence: [evidence] }),
            status: 'completed',
          },
          {
            ...event('run.completed', 4, '本轮创作完成', undefined, { document, evidence: [evidence] }),
            status: 'completed',
          },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '根据最新 GPU 看板创作治理方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    const image = await screen.findByAltText('证据截图：GPU 实时看板')
    expect(image).toHaveAttribute(
      'src',
      'http://localhost:7070/api/creation/evidence/evidence-1/image',
    )
    await waitFor(() => expect(savedHistories).toHaveLength(1))
    expect(savedHistories[0].evidence).toEqual([evidence])
    expect(savedHistories[0].generated_content).toContain('/api/creation/evidence/evidence-1/image')
  })

  it('Agent 要求确认时暂停，用户确认后从同一目标继续', async () => {
    const payloads: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 1 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const payload = JSON.parse(String(init?.body || '{}'))
        payloads.push(payload)
        if (!payload.confirmed) {
          return sse([
            event('run.started', 1, '创作 Agent 已接管目标'),
            {
              ...event(
                'confirmation.required',
                2,
                '需要确认后才能继续',
                undefined,
                {
                  question: '当前要求较简略。是否按现有信息继续？',
                  request_id: 'confirm-1',
                },
              ),
              status: 'waiting',
            },
            {
              ...event('run.paused', 3, '正在等待用户确认', undefined, { reason: 'user_confirmation' }),
              status: 'waiting',
            },
          ])
        }
        return sse([
          event(
            'document.replaced',
            4,
            '文档撰写 Agent 已提交完整文档版本',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
            { content: '# 方案\n\n## 目标\n\n补全合理假设。\n\n## 内容\n\n继续执行。\n\n## 验证\n\n人工确认。' },
          ),
          { ...event('run.completed', 5, '本轮创作完成'), status: 'completed' },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '写方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    const confirmation = await screen.findByRole('group', { name: 'Agent 请求确认' })
    expect(confirmation).toHaveTextContent('当前要求较简略')
    fireEvent.click(screen.getByRole('button', { name: '按当前信息继续' }))

    await waitFor(() => expect(payloads).toHaveLength(2))
    expect(payloads[1].confirmed).toBe(true)
    expect(payloads[1].conversation).toHaveLength(1)
    await screen.findByRole('heading', { name: '方案' })
  })

  it('品牌模型推理通过暂停与恢复续跑，客户端历史不保存模型提示和恢复状态', async () => {
    const agentPayloads: any[] = []
    const gatewayPayloads: any[] = []
    const savedHistories: any[] = []
    useAppStore.getState().setAuthSession({
      access_token: 'test-token',
      expires_at: '2099-01-01T00:00:00Z',
      user: {
        id: 'user-agent-test',
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
      as_of: '2026-07-26T00:00:00Z',
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
            as_of: '2026-07-26T00:00:00Z',
          },
        })
      }
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 1 })
      }
      if (url.pathname === '/v1/gateway/chat') {
        gatewayPayloads.push(JSON.parse(String(init?.body || '{}')))
        return Response.json({ content: '方案设计结论' })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const payload = JSON.parse(String(init?.body || '{}'))
        agentPayloads.push(payload)
        if (!payload.resume_state) {
          return sse([
            event(
              'model.request',
              1,
              '方案设计 Agent 请求品牌模型推理',
              { kind: 'agent', id: 'solution_design_agent', name: '方案设计 Agent' },
              {
                request_id: 'model-request-1',
                messages: [
                  { role: 'system', content: '只输出方案结论' },
                  { role: 'user', content: '设计 Agent Loop' },
                ],
              },
            ),
            {
              ...event(
                'run.paused',
                2,
                '等待品牌模型返回',
                undefined,
                { reason: 'external_model', continuation: { cursor: 3, token: 'resume-secret' } },
              ),
              status: 'waiting',
            },
          ])
        }
        return sse([
          event(
            'document.replaced',
            3,
            '文档撰写 Agent 已提交完整文档版本',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
            { content: '# 外部续跑方案\n\n## 目标\n\n保持品牌模型抽象。\n\n## Loop\n\n暂停后恢复。\n\n## 验证\n\n不泄露内部提示。' },
          ),
          { ...event('run.completed', 4, '本轮创作完成'), status: 'completed' },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '设计支持暂停恢复的创作 Agent Loop' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    await screen.findByRole('heading', { name: '外部续跑方案' })
    expect(screen.getByLabelText('用户消息')).toHaveTextContent('小麦')
    expect(screen.getByLabelText('用户消息')).not.toHaveTextContent('你')
    expect(agentPayloads).toHaveLength(2)
    expect(agentPayloads[0].model_mode).toBe('external')
    expect(agentPayloads[0]).not.toHaveProperty('creation_model')
    expect(agentPayloads[0]).not.toHaveProperty('creation_base_url')
    expect(agentPayloads[1].resume_state).toEqual({ cursor: 3, token: 'resume-secret' })
    expect(agentPayloads[1].model_result).toBe('方案设计结论')
    expect(gatewayPayloads).toHaveLength(1)
    expect(gatewayPayloads[0]).toMatchObject({
      brand_model_id: 'mbcd-plus-v1',
      caller: 'creation',
      privacy: { content_logging: false, client_scrubbed: true },
    })
    expect(gatewayPayloads[0]).not.toHaveProperty('provider')

    expect(savedHistories).toHaveLength(1)
    const storedModelRequest = savedHistories[0].agent_trace.find((item: any) => item.type === 'model.request')
    const storedPause = savedHistories[0].agent_trace.find((item: any) => item.type === 'run.paused')
    expect(storedModelRequest.data).toEqual({ request_id: 'model-request-1' })
    expect(storedPause.data).toEqual({ reason: 'external_model' })
  })

  it('完成事件可恢复最终文档，避免中间文档事件缺失后误报失败', async () => {
    const completedDocument = '# 行业调研方案\n\n## 背景\n\n补充行业现状。\n\n## 调研结论\n\n形成可核验结论。\n\n## 后续动作\n\n持续更新数据。'
    const savedHistories: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 1 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event('goal.updated', 2, '已生成满足当前验收条件的文档'),
          {
            ...event(
              'run.completed',
              3,
              '本轮创作完成',
              undefined,
              { document: completedDocument },
            ),
            status: 'completed',
          },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '增加行业调研' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    await screen.findByRole('heading', { name: '行业调研方案' })
    await waitFor(() => expect(savedHistories).toHaveLength(1))
    expect(savedHistories[0].generated_content).toBe(completedDocument)
    expect(screen.queryByText('生成失败，请稍后重试')).not.toBeInTheDocument()
  })

  it('运行结束前不把中间 patch 标成已完成改动', async () => {
    const intermediateDocument = '# 周年礼物指南\n\n## 原则\n\n中间版本内容。\n\n## 礼物池\n\n等待审校。\n\n## 执行\n\n等待完成。'
    let releaseStream = () => {}
    const encoder = new TextEncoder()
    useAppStore.getState().setCreationDraft({
      generatedContent: '# 周年礼物指南\n\n## 原则\n\n旧版本。\n\n## 礼物池\n\n旧内容。\n\n## 执行\n\n旧流程。',
      sessionId: 'session-agent-test',
      rootRequest: '写一份周年员工的礼物指南',
      conversation: [{
        id: 'user-root',
        role: 'user',
        content: '写一份周年员工的礼物指南',
        createdAt: 1,
      }],
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 1 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const patch = {
          operation: 'revise_document',
          target_sections: ['礼物池'],
          change_count: 1,
          changes: [{
            change_type: 'modified',
            section_title: '礼物池',
            start_line: 7,
            end_line: 9,
            summary: '修改“礼物池”中的内容',
          }],
          summary: '已完成 1 处调整',
        }
        const firstEvents = [
          event('run.started', 20, '创作 Agent 已接管目标'),
          event(
            'agent.started',
            21,
            '文档撰写 Agent 开始执行',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
          ),
          {
            ...event(
              'document.patch.applied',
              22,
              '已完成 1 处调整',
              { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
              { content: intermediateDocument, patch },
            ),
            status: 'completed',
          },
          event(
            'agent.started',
            23,
            '质量审校 Agent 开始执行',
            { kind: 'agent', id: 'quality_review_agent', name: '质量审校 Agent' },
          ),
        ]
        const finalEvents = [
          {
            ...event(
              'agent.completed',
              24,
              '质量检查完成',
              { kind: 'agent', id: 'quality_review_agent', name: '质量审校 Agent' },
            ),
            status: 'completed',
          },
          {
            ...event(
              'run.completed',
              25,
              '本轮创作完成',
              undefined,
              { document: intermediateDocument },
            ),
            status: 'completed',
          },
        ]
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(
              firstEvents.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
            ))
            releaseStream = () => {
              controller.enqueue(encoder.encode(
                finalEvents.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
              ))
              controller.close()
            }
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)
    fireEvent.change(input, { target: { value: '参考示例公司员工周年礼物方案' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    const intermediateText = await screen.findByText('中间版本内容。')
    expect(screen.queryByLabelText('本轮改动')).not.toBeInTheDocument()
    expect(intermediateText).not.toHaveClass('creation-latest-change')

    releaseStream()

    await screen.findByLabelText('本轮改动')
    expect(screen.getByText('等待审校。')).toHaveClass('creation-latest-change')
    expect(screen.getByLabelText('本轮改动')).toHaveTextContent('参考示例公司员工周年礼物方案')
  })

  it('深度思考中展示呼吸灯，完成后折叠为“深度思考 · Ns”', async () => {
    let releaseStream = () => {}
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        const thinkingStarted = {
          ...event('thinking.started', 2, '深度思考中：理解本轮要求', undefined, { stage: 'intent' }),
          status: 'running',
        }
        const thinkingCompleted = {
          ...event('thinking.completed', 5, '深度思考完成：理解本轮要求', undefined, {
            stage: 'intent',
            reasoning: '判断为新场景，直接创建文档',
          }),
          timestamp: 1_720_000_003_002,
          status: 'completed',
        }
        const firstEvents = [
          event('run.started', 1, '创作 Agent 已接管目标'),
          thinkingStarted,
          event(
            'intent.interpreted',
            3,
            '理解为新建完整文档',
            undefined,
            { operation: 'create_document', reasoning_summary: '判断为新场景' },
          ),
        ]
        const finalEvents = [
          thinkingCompleted,
          { ...event('run.completed', 6, '本轮创作完成'), status: 'completed' },
        ]
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(
              firstEvents.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
            ))
            releaseStream = () => {
              controller.enqueue(encoder.encode(
                finalEvents.map(item => `data: ${JSON.stringify(item)}\n\n`).join(''),
              ))
              controller.close()
            }
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '生成一份方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    // 进行中：呼吸灯与“深度思考中”提示。
    const runningTitle = await screen.findByText('深度思考中：理解本轮要求')
    const thinkingNode = runningTitle.closest('.creation-trace-thinking')
    expect(thinkingNode).toHaveClass('is-running')
    expect(thinkingNode?.querySelector('.creation-trace-thinking__dot')).toBeTruthy()

    releaseStream()

    // 完成后：标题为“深度思考”，时长与阶段标签置灰，reasoning 一行可见。
    const finishedMeta = await screen.findByText('3s · 理解本轮要求')
    const finishedNode = finishedMeta.closest('.creation-trace-thinking')
    expect(finishedNode).toHaveTextContent('深度思考')
    expect(finishedNode).not.toHaveClass('is-running')
    expect(finishedNode).toHaveTextContent('判断为新场景，直接创建文档')
    // 步骤计数不包含思考块本身（思考块内的动作仍计入步骤）。
    expect(screen.getByRole('button', { name: /执行过程/ })).toHaveTextContent('3 个步骤')
  })

  it('已完成的动作默认只展示一行概述，点击后才展开细节', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event(
            'intent.interpreted',
            2,
            '理解为新建完整文档',
            undefined,
            { operation: 'create_document' },
          ),
          event(
            'tool.started',
            3,
            '记忆搜索开始执行',
            { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
          ),
          event(
            'tool.completed',
            4,
            '记忆搜索完成，召回 3 条本地资料',
            { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
            { result_count: 3 },
          ),
          { ...event('run.completed', 5, '本轮创作完成'), status: 'completed' },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '生成一份方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    // 折叠行：能力名 + 最新摘要一行可见。
    const trace = await screen.findByLabelText('Agent 执行情况')
    await screen.findByText(/召回 3 条本地资料/)
    const collapsedRow = Array.from(
      trace.querySelectorAll<HTMLElement>('.creation-agent-event__row'),
    ).find(row => row.textContent?.includes('记忆搜索完成'))
    expect(collapsedRow).toBeTruthy()
    expect(collapsedRow).toHaveAttribute('aria-expanded', 'false')
    // 细节（dl）默认不可见。
    expect(trace.querySelector('dl')).toBeNull()
    expect(screen.queryByText('结果')).not.toBeInTheDocument()

    // 点击展开后细节可见，再次点击收起。
    fireEvent.click(collapsedRow as HTMLElement)
    expect(trace.querySelector('dl')).toBeTruthy()
    expect(screen.getByText('结果')).toBeInTheDocument()
    expect(screen.getByText('3 条')).toBeInTheDocument()
    fireEvent.click(collapsedRow as HTMLElement)
    expect(trace.querySelector('dl')).toBeNull()
  })

  it('无思考事件的历史轨迹按原有动作行渲染', async () => {
    useAppStore.getState().setCreationDraft({
      sessionId: 'session-agent-test',
      conversation: [{
        id: 'message-legacy-trace',
        role: 'user',
        content: '生成一份周报',
        createdAt: 1_720_000_000_000,
        runId: 'run-1',
      }],
      agentEvents: [
        event('run.started', 1, '创作 Agent 已接管目标'),
        event(
          'intent.interpreted',
          2,
          '理解为新建完整文档',
          undefined,
          { operation: 'create_document', root_request: '生成一份周报' },
        ),
        event(
          'tool.completed',
          3,
          '记忆搜索完成，召回 2 条本地资料',
          { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
          { result_count: 2 },
        ),
        { ...event('run.completed', 4, '本轮创作完成'), status: 'completed' },
      ] as any,
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    const trace = await screen.findByLabelText('Agent 执行情况')
    // 无思考卡片，全部为动作行。
    expect(trace.querySelector('.creation-trace-thinking')).toBeNull()
    expect(screen.queryByText(/深度思考/)).not.toBeInTheDocument()
    expect(trace.querySelectorAll('.creation-agent-event')).toHaveLength(3)
    // 标题主次拆分后逗号不再保留，主标题与灰色次级结果分开断言。
    expect(trace).toHaveTextContent('记忆搜索完成')
    expect(trace).toHaveTextContent('召回 2 条本地资料')
    // 头部步骤数等于动作分组数。
    expect(screen.getByRole('button', { name: /执行过程/ })).toHaveTextContent('3 个步骤')
  })

  it('phase 事件把执行过程分成顶层阶段与下层动作', async () => {
    const savedHistories: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        savedHistories.push(JSON.parse(String(init.body || '{}')))
        return Response.json({ id: 68 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event('phase.started', 2, '开始阶段：检索本地记忆资料', undefined, {
            phase_id: 'step:memory_search',
            phase_title: '检索本地记忆资料',
            phase_kind: 'plan_step',
          }),
          event(
            'tool.completed',
            3,
            '检索「本周进度」相关资料，召回 2 条本地资料',
            { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
            { result_count: 2 },
          ),
          event('phase.completed', 4, '阶段完成：检索本地记忆资料', undefined, {
            phase_id: 'step:memory_search',
            phase_title: '检索本地记忆资料',
            phase_kind: 'plan_step',
          }),
          event('phase.started', 5, '开始阶段：生成文档内容', undefined, {
            phase_id: 'step:document_writer_agent',
            phase_title: '生成文档内容',
            phase_kind: 'plan_step',
          }),
          event(
            'document.replaced',
            6,
            '文档撰写 Agent 已提交完整文档版本',
            { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
            { content: '# 方案', operation: 'create_document' },
          ),
          event('phase.completed', 7, '阶段完成：生成文档内容', undefined, {
            phase_id: 'step:document_writer_agent',
            phase_title: '生成文档内容',
            phase_kind: 'plan_step',
          }),
          { ...event('run.completed', 8, '本轮创作完成'), status: 'completed' },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '生成一份方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    // 顶层阶段行：序号 + 标题，完成阶段用绿色小圆点标记。
    const firstPhaseTitle = await screen.findByText('1. 检索本地记忆资料')
    const firstPhase = firstPhaseTitle.closest('.creation-trace-phase')
    expect(firstPhase).toHaveClass('is-completed')
    expect(firstPhase?.querySelector('.creation-trace-phase__dot.is-done')).toBeTruthy()
    await screen.findByText('2. 生成文档内容')

    // 完成的阶段默认折叠，点击后展开下层动作与竖线容器。
    const firstPhaseHeader = firstPhase?.querySelector('.creation-trace-phase__header')
    expect(firstPhaseHeader).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/召回 2 条本地资料/)).not.toBeInTheDocument()
    fireEvent.click(firstPhaseHeader as HTMLElement)
    await screen.findByText(/召回 2 条本地资料/)
    expect(firstPhase?.querySelector('.creation-trace-phase__body')).toBeTruthy()

    // 头部同时统计阶段数与步骤数（阶段外的生命周期行也计入步骤）。
    expect(screen.getByRole('button', { name: /执行过程/ }))
      .toHaveTextContent('2 个阶段 · 4 个步骤')

    // 持久化保留阶段信息，历史记录重新打开时仍能分出三层结构。
    await waitFor(() => expect(savedHistories.length).toBeGreaterThan(0))
    const storedPhase = savedHistories[savedHistories.length - 1].agent_trace
      .find((item: any) => (
        item.type === 'phase.completed'
        && item.data?.phase_id === 'step:document_writer_agent'
      ))
    expect(storedPhase.data).toEqual({
      phase_id: 'step:document_writer_agent',
      phase_title: '生成文档内容',
      phase_kind: 'plan_step',
    })
  })

  it('报表校验未通过时步骤及所属阶段显示黄色警告圆点', async () => {
    const warningEvent = {
      ...event(
        'tool.completed',
        3,
        '浏览器访问 1 个报表，但没有指标通过页面结构校验，本轮不采用这些页面的数值',
        { kind: 'tool', id: 'webpage_scrape', name: '网页爬取 Tool' },
        { result_count: 0, failed_count: 1, attempted_count: 1 },
      ),
      status: 'warning',
    }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 99 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return sse([
          event('run.started', 1, '创作 Agent 已接管目标'),
          event('phase.started', 2, '阶段开始：刷新 Token 数据', undefined, {
            phase_id: 'step:webpage_scrape',
            phase_title: '刷新 Token 数据',
            phase_kind: 'plan_step',
          }),
          warningEvent,
          event('phase.completed', 4, '阶段完成：刷新 Token 数据', undefined, {
            phase_id: 'step:webpage_scrape',
            phase_title: '刷新 Token 数据',
            phase_kind: 'plan_step',
          }),
          { ...event('run.completed', 5, '本轮创作完成', undefined, { document: '# Token 摘要' }), status: 'completed' },
        ])
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '生成本周 Token 数据摘要' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    const phaseTitle = await screen.findByText('1. 刷新 Token 数据')
    const phase = phaseTitle.closest('.creation-trace-phase')
    expect(phase).toHaveClass('is-warning')
    expect(phase?.querySelector('.creation-trace-phase__dot.is-warning')).toBeTruthy()
    expect(phase).toHaveTextContent('有警告')

    fireEvent.click(phase?.querySelector('.creation-trace-phase__header') as HTMLElement)
    const warningSummary = await screen.findByText(/没有指标通过页面结构校验/)
    const warningStep = warningSummary.closest('.creation-agent-event')
    expect(warningStep).toHaveClass('is-warning')
    expect(warningStep?.querySelector('.creation-agent-event__dot.is-warning')).toBeTruthy()
  })

  it('用户中止后在对话中记录行为，并把进行中的执行轨迹标记为结束', async () => {
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode([
              event('run.started', 1, '创作 Agent 已接管目标'),
              event('phase.started', 2, '阶段开始：生成文档内容', undefined, {
                phase_id: 'step:document_writer_agent',
                phase_title: '生成文档内容',
                phase_kind: 'plan_step',
              }),
              event('thinking.started', 3, '深度思考中：生成文档内容', undefined, {
                stage: 'generation',
              }),
            ].map(item => `data: ${JSON.stringify(item)}\n\n`).join('')))
            init?.signal?.addEventListener('abort', () => {
              controller.error(new DOMException('The operation was aborted.', 'AbortError'))
            }, { once: true })
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    const input = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(input, { target: { value: '生成一份产品研发周报' } })
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    const phaseTitle = await screen.findByText('1. 生成文档内容')
    expect(phaseTitle.closest('.creation-trace-phase')).toHaveTextContent('进行中')

    fireEvent.click(within(screen.getByLabelText('创作对话')).getByRole('button', { name: '中止' }))

    const abortMessage = await screen.findByLabelText('用户中止消息')
    expect(abortMessage).toHaveTextContent('中止了本次创作')
    expect(abortMessage).toHaveTextContent('已结束')
    expect(phaseTitle.closest('.creation-trace-phase')).toHaveTextContent('已结束')
    expect(phaseTitle.closest('.creation-trace-phase')).not.toHaveTextContent('进行中')
    expect(useAppStore.getState().creationDraft.agentEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'run.cancelled', status: 'cancelled' }),
    ]))
    expect(screen.queryByText('已中止本次创作')).not.toBeInTheDocument()
  })
})
