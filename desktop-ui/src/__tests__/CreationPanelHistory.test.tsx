import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'
import { stageFixture } from '../../test/brainstorm-stage-fixture'

const historyResponse = (url: URL) => {
  const query = url.searchParams.get('q') || ''
  const offset = Number(url.searchParams.get('offset') || 0)
  const limit = Number(url.searchParams.get('limit') || 20)
  const searching = query === '年度规划'
  return {
    items: [{
      id: offset + 1,
      prompt: searching ? '补充年度预算' : '补充风险说明',
      root_request: searching ? '年度规划创作' : '最近创作',
      generated_content: searching ? '年度规划正文' : '最近创作正文',
      doc_type: '方案',
      audience: '管理层',
      reference_count: 0,
      references_json: '[]',
      model: 'mbcd-plus-v1',
      latency_ms: 1800,
      revision_no: 3,
      edit_operation: 'append_section',
      created_at: 1_720_000_000_000 + offset,
      updated_at: 1_720_000_100_000 + offset,
    }],
    total: searching ? 23 : 52,
    limit,
    offset,
  }
}

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input))
    if (url.pathname === '/api/creation/history') {
      return new Response(JSON.stringify(historyResponse(url)), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return new Response('{}', { status: 404 })
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('创作记录搜索与分页', () => {
  it('shows the latest failed chapter draft separately and lists every delivery gap', async () => {
    const state = stageFixture('answering', 'writing')
    state.phase = 'chapter_writing'
    state.current_question = null
    state.stage_workflow!.chapters[0].status = 'writing'
    state.stage_workflow!.chapters[0].instruction_id = 'chapter-instruction-188'
    const agent = { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' }
    const traceEvent = (runId: string, sequence: number, type: string, data: Record<string, unknown> = {}) => ({
      schema_version: 'creation.agent.v1', event_id: `${runId}-${sequence}`, session_id: state.session_id,
      run_id: runId, sequence, timestamp: 1_720_000_000_000 + sequence,
      type, status: type === 'run.failed' ? 'failed' : 'completed', actor: agent,
      summary: type === 'phase.started' ? '阶段开始：核对交付结果' : type,
      environment_patch: {}, data,
    })
    const finalDraft = '# GPU 成本优化周报\n\n## 最新未验收内容\n\n需补充当周会议纪要与 AIGC 进度。'
    const history = {
      id: 188, prompt: 'GPU 成本优化周报', root_request: 'GPU 成本优化周报',
      session_id: state.session_id, generated_content: '', lifecycle_status: 'failed',
      creation_mode: 'brainstorm', creation_brief_json: JSON.stringify(state),
      brainstorm_stage_summary: { version: 2, phase: 'chapter_writing', active_stage: 'writing',
        stage_status: 'answering', completed_chapters: 0, total_chapters: 2, active_chapter_title: '目标与评估范围' },
      agent_trace_json: JSON.stringify([
        traceEvent('old-run', 1, 'document.replaced', { content: '# 上一次失败的草稿' }),
        traceEvent('old-run', 2, 'run.failed', { error_code: 'CREATION_DOCUMENT_INVALID' }),
        traceEvent('final-run', 1, 'document.replaced', { content: '# 本轮较早修订' }),
        traceEvent('final-run', 2, 'document.replaced', { content: finalDraft }),
        traceEvent('final-run', 3, 'phase.started', {
          phase_id: 'step:delivery_validation', phase_title: '核对交付结果', phase_kind: 'plan_step',
        }),
        traceEvent('final-run', 4, 'delivery.checked', { review: { status: 'blocked', checks: [
          { id: 'meeting', passed: false, reason: '本周大模型性能成本优化周会会议纪要：目标周期内资料未确认' },
          { id: 'aigc', passed: false, reason: 'AIGC进度总结：目标周期内资料未确认' },
        ], failed_checks: [
          { id: 'aigc', reason: 'AIGC进度总结：目标周期内资料未确认' },
          { id: 'token', reason: 'LangBridge 六项 Token 指标：只有单日卡片，周汇总仍待核对' },
        ] } }),
        traceEvent('final-run', 5, 'run.failed', { error_code: 'CREATION_DELIVERY_INCOMPLETE' }),
      ]),
      conversation_json: '[]', references_json: '[]', created_at: 1_720_000_000_000, updated_at: 1_720_000_100_000,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/history') return Response.json({ items: [history], total: 1 })
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/brainstorm/turn') return Response.json(state)
      return Response.json({ connected: false })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    expect(screen.getByText('待核验草稿已保留，打开后可查看交付缺口')).toBeInTheDocument()
    fireEvent.click(screen.getByText('GPU 成本优化周报').closest('button')!)

    const preview = await screen.findByLabelText('待核验草稿')
    expect(preview).toHaveTextContent('最新未验收内容')
    expect(preview).not.toHaveTextContent('上一次失败的草稿')
    expect(preview).not.toHaveTextContent('本轮较早修订')
    expect(preview).toHaveTextContent('不是正式正文')
    expect(useAppStore.getState().creationDraft.generatedContent).toBe('')
    expect(screen.queryByRole('button', { name: '复制' })).not.toBeInTheDocument()
    const deliveryPhase = screen.getByText('1. 核对交付结果').closest('.creation-trace-phase')!
    expect(deliveryPhase).toHaveTextContent('待核验缺口（3）')
    expect(deliveryPhase).toHaveTextContent('本周大模型性能成本优化周会会议纪要：目标周期内资料未确认')
    expect(deliveryPhase).toHaveTextContent('AIGC进度总结：目标周期内资料未确认')
    expect(deliveryPhase).toHaveTextContent('LangBridge 六项 Token 指标：只有单日卡片，周汇总仍待核对')
    expect(screen.getByRole('button', { name: '继续本章创作' })).toBeEnabled()
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/api/creation/agent/run'))).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: '编辑创作简报' }))
    expect(screen.queryByText('最新未验收内容')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看待核验草稿' }))
    expect(screen.getByLabelText('待核验草稿')).toHaveTextContent('最新未验收内容')
  })

  it('restores a stale failed workflow through status synchronization without flashing its old failure', async () => {
    const cached = stageFixture('failed', 'writing')
    cached.stage_workflow!.stages[2].error = { code: 'BRAINSTORM_MODEL_OUTPUT_INVALID', message: '历史快照生成失败', retryable: true }
    const current = stageFixture('thinking', 'writing')
    current.revision = cached.revision + 1
    current.stage_workflow!.stages[2].generation_id = 'current-restored-generation'
    const requests: Record<string, unknown>[] = []
    let finishStatus!: () => void
    const pendingStatus = new Promise<void>(resolve => { finishStatus = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/history') return Response.json({ items: [{ id: 41, session_id: cached.session_id,
        root_request: '恢复旧脑暴会话', lifecycle_status: 'completed', generated_content: '', creation_mode: 'brainstorm',
        creation_brief_json: JSON.stringify(cached),
        brainstorm_stage_summary: { version: 2, active_stage: 'writing', phase: cached.phase, stage_status: 'failed', completed_chapters: 0, total_chapters: 2 },
      }], total: 1 })
      if (path === '/api/creation/brainstorm/turn') {
        requests.push(JSON.parse(String(init?.body || '{}')))
        await pendingStatus
        return Response.json(current)
      }
      if (path === '/api/creation/skills') return Response.json([])
      return Response.json({ connected: false })
    }))
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(screen.getByText('恢复旧脑暴会话').closest('button')!)
    expect(screen.getByText('正在同步创作行文进度')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('历史快照生成失败')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续本阶段思考' })).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.stages[2].status).toBe('failed')
    await act(async () => { finishStatus() })
    expect(await screen.findByText('创作行文阶段模型思考中')).toBeInTheDocument()
    expect(requests.every(item => item.action === 'status')).toBe(true)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual([])
  })

  it('shows current stage and chapter count independently of a completed chapter operation or stale brief', async () => {
    const staleBrief = stageFixture('completed', 'writing')
    staleBrief.phase = 'completed'
    const records = [
      { id: 21, root_request: '等待第二章设计', phase: 'ready', stage_status: 'ready', lifecycle_status: 'completed', completed_chapters: 1 },
      { id: 22, root_request: '正在创作第二章', phase: 'chapter_writing', stage_status: 'answering', lifecycle_status: 'completed', completed_chapters: 1 },
      { id: 23, root_request: '第二章待恢复', phase: 'chapter_writing', stage_status: 'answering', lifecycle_status: 'failed', completed_chapters: 1 },
      { id: 24, root_request: '全篇完成后的普通修改', phase: 'completed', stage_status: 'completed', lifecycle_status: 'failed', completed_chapters: 2 },
    ].map(item => ({ ...item, generated_content: '# 已保留的第一章正文', creation_mode: 'brainstorm',
      creation_brief_json: JSON.stringify(staleBrief),
      brainstorm_stage_summary: { version: 2, active_stage: 'writing', phase: item.phase, stage_status: item.stage_status,
        completed_chapters: item.completed_chapters, total_chapters: 2, active_chapter_title: item.phase === 'completed' ? null : '指标设计' },
    }))
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/history') return Response.json({ items: records, total: records.length })
      if (path === '/api/creation/skills') return Response.json([])
      return new Response('{}', { status: 404 })
    }))
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (4)' }))
    const waiting = within(screen.getByText('等待第二章设计').closest('article')!)
    expect(waiting.getByText('创作行文待确认')).toBeInTheDocument()
    expect(waiting.getByText('1 / 2 章已完成 · 当前章节：指标设计')).toBeInTheDocument()
    expect(waiting.queryByText('全部章节已完成')).not.toBeInTheDocument()
    expect(waiting.queryByText('进行中')).not.toBeInTheDocument()
    expect(within(screen.getByText('正在创作第二章').closest('article')!).getByText('章节创作中')).toBeInTheDocument()
    expect(within(screen.getByText('第二章待恢复').closest('article')!).getByText('章节创作待继续')).toBeInTheDocument()
    const completed = within(screen.getByText('全篇完成后的普通修改').closest('article')!)
    expect(completed.getByText('全部章节已完成')).toBeInTheDocument()
    expect(completed.getByText('失败')).toBeInTheDocument()
    expect(completed.getByText('2 / 2 章已完成')).toBeInTheDocument()
    expect(records[0].lifecycle_status).toBe('completed')
    expect(vi.mocked(fetch).mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/agent/run'))).toBe(false)
  })

  it('refreshes a thinking history row while open and on returning without turning user confirmation into a running operation', async () => {
    let stage_status = 'thinking'
    let active_stage = 'direction'
    const historyReads: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/history') {
        historyReads.push(stage_status)
        return Response.json({ items: [{ id: 31, root_request: '切出等待模型思考', lifecycle_status: 'completed',
          generated_content: '', creation_mode: 'brainstorm',
          brainstorm_stage_summary: { version: 2, phase: stage_status === 'thinking' ? 'preparing' : 'ready',
            active_stage, stage_status, completed_chapters: 0, total_chapters: 0 },
        }], total: 1 })
      }
      if (path === '/api/creation/skills') return Response.json([])
      return new Response('{}', { status: 404 })
    }))
    const view = render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    expect(screen.getByText('创作方向思考中')).toBeInTheDocument()
    await waitFor(() => expect(historyReads.length).toBeGreaterThanOrEqual(2))
    stage_status = 'ready'
    await waitFor(() => expect(screen.getByText('创作方向待确认')).toBeInTheDocument(), { timeout: 4500 })
    expect(screen.getByText('本阶段完整思路已准备好，点击继续确认。')).toBeInTheDocument()
    view.rerender(<CreationPanel active={false} />)
    active_stage = 'detail'
    stage_status = 'thinking'
    view.rerender(<CreationPanel active />)
    await waitFor(() => expect(screen.getByText('创作细节思考中')).toBeInTheDocument())
    expect(vi.mocked(fetch).mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/agent/run'))).toBe(false)
  })

  it('展示真实总数，并把搜索和分页参数传给服务端', async () => {
    render(<CreationPanel />)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '创作记录 (52)' })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: '创作记录 (52)' }))

    fireEvent.change(screen.getByLabelText('搜索创作记录'), {
      target: { value: '年度规划' },
    })

    await waitFor(() => {
      expect(screen.getByText('年度规划创作')).toBeInTheDocument()
      expect(screen.getByText(/完整会话/)).toBeInTheDocument()
      expect(screen.queryByText(/第 3 版/)).not.toBeInTheDocument()
      expect(screen.queryByText(/局部新增/)).not.toBeInTheDocument()
      expect(screen.getByText('找到 23 条')).toBeInTheDocument()
      expect(vi.mocked(fetch).mock.calls.some(([input]) => {
        const url = new URL(String(input))
        return url.searchParams.get('q') === '年度规划'
          && url.searchParams.get('offset') === '0'
          && url.searchParams.get('paged') === 'true'
      })).toBe(true)
    }, { timeout: 1500 })

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))

    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.some(([input]) => {
        const url = new URL(String(input))
        return url.searchParams.get('q') === '年度规划'
          && url.searchParams.get('offset') === '20'
      })).toBe(true)
      expect(screen.getByText('第 2 / 2 页')).toBeInTheDocument()
    })
  })

  it('不在创作记录列表展示直出或脑暴模式', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [
            {
              ...historyResponse(url).items[0],
              id: 1,
              prompt: '年度方案 A',
              root_request: '年度方案 A',
              creation_mode: 'brainstorm',
            },
            {
              ...historyResponse(url).items[0],
              id: 2,
              prompt: '年度方案 B',
              root_request: '年度方案 B',
              creation_mode: 'direct',
            },
          ],
          total: 2,
          limit: 20,
          offset: 0,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (2)' }))

    const historyList = document.querySelector('.creation-history__list')
    expect(historyList).not.toBeNull()
    expect(historyList).not.toHaveTextContent('脑暴')
    expect(historyList).not.toHaveTextContent('直出')
    expect(document.querySelector('.creation-history__mode')).toBeNull()
  })

  it('恢复历史时完整保留召回评分、匹配诊断和结构化检索计划', async () => {
    const diagnosticReference = {
      id: 18,
      source_id: 218,
      source_type: 'knowledge',
      title: '原创剧本与 Agent 协作方案',
      doc_type: '方案',
      final_weight: 0.91,
      relevance_score: 0.88,
      quality_score: 0.82,
      completeness_score: 0.79,
      usage_score: 0.4,
      format_score: 0.68,
      freshness_score: 0.76,
      usage_count: 2,
      reason: '覆盖原创剧本主目标及与 Agent 的结合关系',
      retrieval_tier: 'related',
      retrieval_paths: ['keyword', 'semantic'],
      matched_keywords: ['原创剧本', 'Agent'],
      matched_entities: ['Agent'],
      lexical_score: 0.63,
      semantic_score: 0.81,
      entity_score: 0.2,
      retrieval_mode: 'relational',
      primary_target: '原创剧本',
      matched_components: ['原创剧本', 'Agent'],
      matched_relations: ['结合'],
      relation_score: 0.86,
      intent_mode: 'relational',
      matched_concepts: ['原创剧本', 'Agent'],
      primary_target_score: 0.97,
      coverage: 0.83,
      relation_coverage: 0.8,
    }
    const entityContext = {
      primary_entities: [{ name: '原创剧本', confidence: 0.96 }],
      has_high_confidence_entity: true,
    }
    const retrievalPlan = {
      mode: 'relational',
      primary_target: '原创剧本',
      components: ['Agent'],
      relations: ['结合'],
    }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 18,
            prompt: '生成原创剧本和当前 Agent 的结合方案',
            root_request: '生成原创剧本和当前 Agent 的结合方案',
            generated_content: '# 原创剧本 Agent 方案',
            doc_type: '方案',
            audience: '创作团队',
            lifecycle_status: 'completed',
            references_json: JSON.stringify([diagnosticReference]),
            conversation_json: '[]',
            evidence_json: '[]',
            agent_trace_json: JSON.stringify([{
              schema_version: 'creation.agent.v1',
              event_id: 'history-memory-search',
              session_id: 'history-diagnostics-session',
              run_id: 'history-diagnostics-run',
              sequence: 1,
              timestamp: 1_720_000_000_000,
              type: 'tool.completed',
              status: 'completed',
              actor: { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
              summary: '记忆搜索完成，召回 1 条本地资料',
              environment_patch: { references: [diagnosticReference] },
              data: {
                result_count: 1,
                query: '原创剧本 Agent 结合方案',
                entity_context: entityContext,
                retrieval_plan: retrievalPlan,
              },
            }]),
            created_at: 1_720_000_000_000,
            updated_at: 1_720_000_100_000,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('生成原创剧本和当前 Agent 的结合方案'))

    await waitFor(() => {
      expect(useAppStore.getState().creationDraft.referencePreview?.references[0]).toMatchObject(
        diagnosticReference,
      )
    })
    const restoredMemorySearch = useAppStore.getState().creationDraft.agentEvents.find(
      event => event.actor?.id === 'memory_search',
    )
    expect(restoredMemorySearch?.data?.entity_context).toEqual(entityContext)
    expect(restoredMemorySearch?.data?.retrieval_plan).toEqual(retrievalPlan)
  })

  it('启动后自动恢复最近一条进行中的手动创作', async () => {
    const requestedPaths: string[] = []
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      requestedPaths.push(`${init?.method || 'GET'} ${url.pathname}`)
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 66 })
      if (url.pathname === '/api/creation/history/66/progress') return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/agent/run') {
        const completed = {
          schema_version: 'creation.agent.v1',
          event_id: 'recovered-completed',
          session_id: 'running-session',
          run_id: 'recovered-run',
          sequence: 1,
          timestamp: 1_720_000_200_000,
          type: 'run.completed',
          status: 'completed',
          actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
          summary: '重启后续跑完成',
          environment_patch: {},
          data: { document: '# 重启后完成的方案' },
        }
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(completed)}\n\n`))
            controller.close()
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 66 })
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 66,
            prompt: '生成后台创作方案',
            root_request: '生成后台创作方案',
            generated_content: '# 已完成的部分',
            session_id: 'running-session',
            lifecycle_status: 'running',
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: JSON.stringify([{
              schema_version: 'creation.agent.v1',
              event_id: 'running-phase',
              session_id: 'running-session',
              run_id: 'running-run',
              sequence: 1,
              timestamp: 1_720_000_000_000,
              type: 'phase.started',
              status: 'running',
              actor: { kind: 'agent', id: 'document_writer_agent', name: '文档撰写 Agent' },
              summary: '阶段开始：撰写方案',
              environment_patch: {},
              data: { phase_id: 'write', phase_title: '撰写方案', phase_kind: 'plan_step' },
            }]),
            created_at: 1_720_000_000_000,
            updated_at: 1_720_000_100_000,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    await waitFor(() => expect(requestedPaths).toContain('POST /api/creation/agent/run'))
    await waitFor(() => {
      expect(useAppStore.getState().creationDraft.generatedContent).toBe('# 重启后完成的方案')
    })
    expect(useAppStore.getState().creationDraft.sessionId).toBe('running-session')
    expect(useAppStore.getState().creationDraft.agentEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'run.failed',
        data: expect.objectContaining({ error_code: 'HISTORICAL_RUN_INTERRUPTED' }),
      }),
      expect.objectContaining({ type: 'run.completed', run_id: 'recovered-run' }),
    ]))
  })

  it('启动后自动恢复脑暴文档的未完成修订且不提交脑暴答案', async () => {
    const requestedBodies: Record<string, unknown>[] = []
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history/start') return Response.json({ id: 163 })
      if (url.pathname === '/api/creation/history/163/progress') return new Response(null, { status: 204 })
      if (url.pathname === '/api/creation/brainstorm') {
        throw new Error('自动恢复文档操作时不应提交脑暴答案')
      }
      if (url.pathname === '/api/creation/brainstorm/session') {
        return Response.json({
          session_id: 'brainstorm-running-session',
          revision: 2,
          phase: 'exploring',
          root_request: '设计招商方案',
          current_question: null,
          history: [],
          decisions: [],
          open_flags: ['成本仍待确认'],
          brief_markdown: '# 创作简报',
        })
      }
      if (url.pathname === '/api/creation/agent/run') {
        requestedBodies.push(JSON.parse(String(init?.body || '{}')))
        const completed = {
          schema_version: 'creation.agent.v1', event_id: 'brainstorm-recovered-completed',
          session_id: 'brainstorm-running-session', run_id: 'brainstorm-recovered-run',
          sequence: 24, timestamp: 1_720_000_200_000, type: 'run.completed', status: 'completed',
          actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
          summary: '脑暴文档修订续跑完成', environment_patch: {},
          data: { document: '# 方案\n\n## L0 商家定义\n\n已补充。' },
        }
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(completed)}\n\n`))
            controller.close()
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') return Response.json({ id: 163 })
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [{
          id: 163, prompt: '增加灵机L0商家的定义介绍', root_request: '设计招商方案',
          generated_content: '# 方案', session_id: 'brainstorm-running-session',
          lifecycle_status: 'running', creation_mode: 'brainstorm', references_json: '[]',
          creation_brief_json: JSON.stringify({
            session_id: 'brainstorm-running-session', revision: 2, phase: 'exploring',
            root_request: '设计招商方案', current_question: null, history: [], decisions: [],
            open_flags: ['成本仍待确认'], brief_markdown: '# 创作简报',
          }),
          conversation_json: JSON.stringify([{ id: 'instruction-l0', role: 'user', content: '增加灵机L0商家的定义介绍' }]),
          agent_trace_json: JSON.stringify([{
            schema_version: 'creation.agent.v1', event_id: 'brainstorm-tool-started',
            session_id: 'brainstorm-running-session', run_id: 'interrupted-run', sequence: 22,
            timestamp: 1_720_000_000_000, type: 'tool.started', status: 'running',
            actor: { kind: 'tool', id: 'memory_search', name: '记忆搜索 Tool' },
            summary: '记忆搜索 Tool 开始执行', environment_patch: {}, data: {},
          }]), created_at: 1_720_000_000_000, updated_at: 1_720_000_100_000,
        }], total: 1, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<StrictMode><CreationPanel /></StrictMode>)
    await waitFor(() => expect(requestedBodies).toHaveLength(1))
    expect(requestedBodies[0]).toMatchObject({
      session_id: 'brainstorm-running-session',
      creation_mode: 'brainstorm',
      user_prompt: '增加灵机L0商家的定义介绍',
      instruction_id: 'instruction-l0',
    })
    await waitFor(() => {
      expect(useAppStore.getState().creationDraft.generatedContent).toContain('L0 商家定义')
    })
  })

  it('不把等待用户回答的旧脑暴会话误当成中断文档执行', async () => {
    const requestedPaths: string[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      requestedPaths.push(`${init?.method || 'GET'} ${url.pathname}`)
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [{
          id: 148, prompt: '设计招商方案', root_request: '设计招商方案',
          generated_content: '', session_id: 'brainstorm-waiting-for-user',
          lifecycle_status: 'running', creation_mode: 'brainstorm', references_json: '[]',
          conversation_json: JSON.stringify([{ id: 'root-question', role: 'user', content: '设计招商方案' }]),
          creation_brief_json: JSON.stringify({
            session_id: 'brainstorm-waiting-for-user', revision: 1, phase: 'exploring',
            root_request: '设计招商方案', history: [], decisions: [], open_flags: [],
            current_question: { id: 'q1', prompt: '先确定目标用户？', options: [] },
          }),
          agent_trace_json: '[]', created_at: 1_720_000_000_000, updated_at: 1_720_000_100_000,
        }], total: 1, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    await waitFor(() => expect(requestedPaths).toContain('GET /api/creation/history'))
    await new Promise(resolve => window.setTimeout(resolve, 0))
    expect(requestedPaths).not.toContain('POST /api/creation/agent/run')
    expect(useAppStore.getState().creationDraft.sessionId).toBeNull()
  })

  it('恢复缺少终止事件的失败记录时按中断状态展示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 85,
            prompt: '旧失败创作',
            root_request: '旧失败创作',
            generated_content: '# 已生成部分',
            doc_type: '方案',
            audience: '研发',
            session_id: 'history-interrupted-session',
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: JSON.stringify([
              {
                schema_version: 'creation.agent.v1',
                event_id: 'history-run-started',
                session_id: 'history-interrupted-session',
                run_id: 'history-interrupted-run',
                sequence: 1,
                timestamp: 1_720_000_000_001,
                type: 'phase.started',
                status: 'running',
                actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
                summary: '阶段开始：生成文档内容',
                environment_patch: {},
                data: {
                  phase_id: 'step:document_writer_agent',
                  phase_title: '生成文档内容',
                  phase_kind: 'plan_step',
                },
              },
              {
                schema_version: 'creation.agent.v1',
                event_id: 'history-thinking-started',
                session_id: 'history-interrupted-session',
                run_id: 'history-interrupted-run',
                sequence: 2,
                timestamp: 1_720_000_000_002,
                type: 'thinking.started',
                status: 'running',
                actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
                summary: '深度思考中：生成文档内容',
                environment_patch: {},
                data: { stage: 'generation' },
              },
              {
                schema_version: 'creation.agent.v1',
                event_id: 'history-run-paused',
                session_id: 'history-interrupted-session',
                run_id: 'history-interrupted-run',
                sequence: 3,
                timestamp: 1_720_000_000_003,
                type: 'run.paused',
                status: 'waiting',
                actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
                summary: '等待品牌模型返回',
                environment_patch: {},
                data: { reason: 'external_model' },
              },
            ]),
            created_at: 1_720_000_000_000,
            updated_at: 1_720_000_100_000,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('旧失败创作'))

    const phaseTitle = await screen.findByText('1. 生成文档内容')
    expect(phaseTitle.closest('.creation-trace-phase')).toHaveTextContent('未完成')
    expect(phaseTitle.closest('.creation-trace-phase')).not.toHaveClass('is-running')
    expect(screen.queryByText(/深度思考中/)).not.toBeInTheDocument()
    expect(document.querySelector('.creation-trace-thinking.is-running')).toBeNull()
    expect(document.querySelector('.creation-agent-event__activity')).toBeNull()
    expect(useAppStore.getState().creationDraft.agentEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'run.failed',
        status: 'failed',
        data: expect.objectContaining({ error_code: 'HISTORICAL_RUN_INTERRUPTED' }),
      }),
    ]))
  })

  it('为未保存来源编号的旧创作记录重新匹配并展示具体数据', async () => {
    const dataSearchBodies: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 36,
            prompt: '写一篇单位Token成本优化的项目总结汇报文档',
            root_request: '写一篇单位Token成本优化的项目总结汇报文档',
            generated_content: '# 项目总结',
            doc_type: '项目总结',
            audience: '管理层',
            references_json: '[]',
            conversation_json: '[]',
            evidence_json: '[]',
            agent_trace_json: JSON.stringify([{
              schema_version: 'creation.agent.v1',
              event_id: 'legacy-data-search',
              session_id: 'legacy-session',
              run_id: 'legacy-run',
              sequence: 9,
              timestamp: 1_720_000_000_000,
              type: 'tool.completed',
              status: 'completed',
              actor: { kind: 'tool', id: 'data_search', name: '数据检索 Tool' },
              summary: '数据检索完成，召回 6 个来源，其中 0 个需要刷新',
              environment_patch: {},
              data: { result_count: 6 },
            }]),
            created_at: 1_720_000_000_000,
            updated_at: 1_720_000_100_000,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/tools/data-search') {
        dataSearchBodies.push(JSON.parse(String(init?.body || '{}')))
        return Response.json({
          schema_version: 'memorybread.data-search.v1',
          query: '单位Token成本优化',
          results: [{
            source_id: 12,
            title: 'Token 成本经营数据',
            source_kind: 'memory_snapshot',
            freshness_class: 'recent',
            refresh_required: false,
            can_use: true,
          }],
        })
      }
      if (url.pathname === '/api/data/sources/12') {
        return Response.json({
          id: 12,
          title: 'Token 成本经营数据',
          source_kind: 'work_memory',
          access_mode: 'memory_only',
          refresh_policy: 'never',
          realtime_level: 'observed',
          tags: [],
          first_seen_at: 1,
          last_seen_at: 2,
          status: 'active',
          latest_snapshot: {
            id: 120,
            source_id: 12,
            collected_at: 1_720_000_000_000,
            collector: 'memory',
            content_text: '优化后单位 Token 成本降至 0.018 元。',
            structured_data: {
              title: '单位 Token 成本优化结果',
              summary: '优化项目实现单位 Token 成本下降 28%。',
              metric_rows: [{
                dimension: '优化后',
                metric: '单位 Token 成本',
                value: '0.018 元',
                note: '下降 28%',
              }],
            },
            content_hash: 'legacy-hash',
            freshness_ttl_seconds: 86400,
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
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('写一篇单位Token成本优化的项目总结汇报文档'))

    await waitFor(() => expect(dataSearchBodies).toEqual([{
      query: '写一篇单位Token成本优化的项目总结汇报文档',
      limit: 10,
    }]))

    fireEvent.click(await screen.findByRole('tab', { name: '参考数据 (1)' }))
    expect(await screen.findByText('单位 Token 成本优化结果')).toBeInTheDocument()
    expect(screen.getByText('优化项目实现单位 Token 成本下降 28%。')).toBeInTheDocument()
    expect(screen.getByText('0.018 元')).toBeInTheDocument()
    expect(screen.getByText('该历史版本未保存来源编号，以下内容按原始需求从当前本地数据恢复。')).toBeInTheDocument()
  })
})

describe('定时任务创作记录', () => {
  const scheduledHistoryRecord = {
    id: 77,
    prompt: '生成行业周报',
    root_request: '生成行业周报',
    generated_content: '# 任务执行产物\n本周行业动态已汇总。',
    doc_type: '周报',
    audience: '管理层',
    session_id: 'session-task-3-9',
    source_kind: 'scheduled_task',
    source_ref_id: 3,
    reference_count: 0,
    references_json: '[]',
    conversation_json: '[]',
    agent_trace_json: JSON.stringify([{
      schema_version: 'creation.agent.v1',
      event_id: 'task-run-completed',
      session_id: 'session-task-3-9',
      run_id: 'task-run-1',
      sequence: 1,
      timestamp: 1_720_000_000_000,
      type: 'run.completed',
      status: 'completed',
      actor: { kind: 'agent', id: 'orchestrator', name: '创作编排' },
      summary: '执行完成',
      environment_patch: {},
      data: { document: '# 任务执行产物' },
    }]),
    created_at: 1_720_000_000_000,
    updated_at: 1_720_000_100_000,
  }

  it('创作记录列表为定时任务来源展示徽标', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{ ...scheduledHistoryRecord, id: 9 }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))

    expect(await screen.findByText('定时任务')).toBeInTheDocument()
    expect(screen.getByText('生成行业周报')).toBeInTheDocument()
  })

  it('任务页跳转目标会拉取单条记录并恢复会话与执行流水', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/history/77') {
        return Response.json(scheduledHistoryRecord)
      }
      return new Response('{}', { status: 404 })
    }))

    useAppStore.getState().setCreationHistoryOpenTarget(77)
    const view = render(<CreationPanel />)

    // 恢复后文档内容（编辑器内）与执行流水事件都应展示出来。
    await waitFor(() => {
      expect(view.container.textContent).toContain('任务执行产物')
    }, { timeout: 3000 })
    await waitFor(() => {
      expect(view.container.textContent).toContain('执行完成')
    })
    expect(vi.mocked(fetch).mock.calls.some(([input]) =>
      String(input).includes('/api/creation/history/77'))).toBe(true)
    await waitFor(() => {
      expect(useAppStore.getState().creationHistoryOpenTarget).toBeNull()
    })
  })
})
