import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'

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
