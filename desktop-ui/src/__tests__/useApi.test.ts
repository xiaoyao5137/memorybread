import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  runGatewayRagQuery,
  runGatewayRagQueryStream,
  runRagQueryStream,
  useFetchBakeCaptures,
  useFetchBakeKnowledge,
  useFetchBakeKnowledgeDetail,
  useFetchBakeMemory,
  useFetchBakeMemories,
  useFetchBakeSop,
  useFetchBakeSops,
  useFetchBakeTemplates,
  useFetchRagHistory,
} from '../hooks/useApi'
import { useAppStore } from '../store/useAppStore'

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

describe('runRagQueryStream', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('按 SSE 顺序交付状态、参考资料、答案增量和耗时', async () => {
    const encoder = new TextEncoder()
    const chunks = [
      'data: {"type":"status","stage":"retrieving","message":"正在召回相关资料","progress":42}\n\n',
      'data: {"type":"references","contexts":[{"capture_id":1,"text":"资料","score":0.9,"source":"document"}]}\n\n',
      'data: {"type":"delta","text":"部分"}\n\ndata: {"type":"delta","text":"答案"}\n\n',
      'data: {"type":"done","answer":"部分答案","contexts":[{"capture_id":1,"text":"资料","score":0.9,"source":"document"}],"model":"local","elapsed_ms":1800,"inference_elapsed_ms":1200}\n\n',
    ]
    const body = new ReadableStream({
      start(controller) {
        chunks.forEach(chunk => controller.enqueue(encoder.encode(chunk)))
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const events: string[] = []

    const result = await runRagQueryStream(
      'http://localhost:7070',
      [],
      '流式测试',
      5,
      {},
      false,
      undefined,
      {
        onStatus: status => events.push(`status:${status.stage}`),
        onReferences: contexts => events.push(`references:${contexts.length}`),
        onDelta: (_delta, accumulated) => events.push(`answer:${accumulated}`),
      },
    )

    expect(events).toEqual([
      'status:retrieving',
      'references:1',
      'answer:部分',
      'answer:部分答案',
    ])
    expect(result).toMatchObject({
      answer: '部分答案',
      elapsed_ms: 1800,
      inference_elapsed_ms: 1200,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:7070/api/rag/stream',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      }),
    )
  })
})

describe('runGatewayRagQuery environment binding', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('只在云网关请求中携带当前服务环境', async () => {
    useAppStore.getState().reset()
    useAppStore.getState().setDebugModeEnabled(true)
    useAppStore.getState().setServiceEnvironment('staging')
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ contexts: [] }))
      .mockResolvedValueOnce(jsonResponse({ content: '云端回答' }))
      .mockResolvedValueOnce(jsonResponse({}))
    vi.stubGlobal('fetch', fetchMock)

    await runGatewayRagQuery(
      'http://127.0.0.1:7070',
      'http://127.0.0.1:18090',
      '环境绑定测试',
      'user-1',
    )

    expect(fetchMock.mock.calls[0][1]?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(fetchMock.mock.calls[1][1]?.headers).toEqual({
      'X-MemoryBread-Environment': 'staging',
      'Content-Type': 'application/json',
    })
  })

  it('云端流式咨询先交付本地参考资料，再消费网关答案增量', async () => {
    useAppStore.getState().reset()
    useAppStore.getState().setDebugModeEnabled(true)
    useAppStore.getState().setServiceEnvironment('staging')
    const encoder = new TextEncoder()
    const gatewayBody = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"delta","text":"云端"}\n\n'))
        controller.enqueue(encoder.encode(
          'data: {"type":"done","answer":"云端回答","model":"mbcd-plus-v1","elapsed_ms":900,"inference_elapsed_ms":700}\n\n',
        ))
        controller.close()
      },
    })
    const reference = {
      capture_id: 1,
      text: '本地参考',
      score: 0.9,
      source: 'document',
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ contexts: [reference] }))
      .mockResolvedValueOnce(new Response(gatewayBody, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
      .mockResolvedValueOnce(jsonResponse({}))
    vi.stubGlobal('fetch', fetchMock)
    const events: string[] = []

    const result = await runGatewayRagQueryStream(
      'http://127.0.0.1:7070',
      'http://127.0.0.1:18090',
      '云端流式测试',
      '00000000-0000-0000-0000-000000000001',
      undefined,
      { source: 'floating_assist' },
      {
        onReferences: contexts => events.push(`references:${contexts.length}`),
        onDelta: (_delta, accumulated) => events.push(`answer:${accumulated}`),
      },
    )

    expect(events).toEqual(['references:1', 'answer:云端'])
    expect(result).toMatchObject({
      answer: '云端回答',
      contexts: [reference],
      model: 'mbcd-plus-v1',
      inference_elapsed_ms: 700,
    })
    expect(fetchMock.mock.calls[1][1]?.headers).toEqual({
      'X-MemoryBread-Environment': 'staging',
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    })
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string).stream).toBe(true)
  })
})

describe('useFetchRagHistory', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('把关键词与分页参数传给咨询记录接口，并返回总数', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      items: [{
        id: 21,
        ts: 1_720_000_000_000,
        query: '年度规划',
        answer: '规划内容',
        contexts: [],
        context_count: 0,
        latency_ms: 1000,
      }],
      total: 37,
      limit: 20,
      offset: 20,
    }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    const { result } = renderHook(() => useFetchRagHistory())
    const data = await result.current({ limit: 20, offset: 20, query: '年度规划' }, controller.signal)

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:7070/api/rag/history?limit=20&offset=20&q=%E5%B9%B4%E5%BA%A6%E8%A7%84%E5%88%92',
      { signal: controller.signal },
    )
    expect(data.total).toBe(37)
    expect(data.items[0].query).toBe('年度规划')
  })
})

describe('useFetchBakeMemories', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('请求 timelines 知识接口并保留查询参数', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        entries: [{
          id: 1,
          summary: '摘要 A',
          overview: '概览 A',
          details: '详情 A',
          capture_id: 11,
          importance: 3,
          occurrence_count: 2,
          created_at: '2026-04-11 10:00',
          created_at_ms: 1712800800000,
          capture_ids: [11, 12],
          keyTimestamps: [{
            start_ts: 1712800800000,
            end_ts: 1712800860000,
            summary: '缺少 capture_ids 的历史分段',
          }],
        }],
        total: 1,
        limit: 10,
        offset: 20,
      }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeMemories())
    const data = await result.current({ q: '设计', from: 100, to: 200, limit: 10, offset: 20 })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://localhost:7070/api/knowledge?q=%E8%AE%BE%E8%AE%A1&from=100&to=200&limit=10&offset=20',
    )
    expect(data.total).toBe(1)
    expect(data.limit).toBe(10)
    expect(data.offset).toBe(20)
    expect(data.items[0]).toMatchObject({
      id: '1',
      title: '摘要 A',
      sourceCaptureId: '11',
      summary: '概览 A',
      createdAt: '2026-04-11 10:00',
      createdAtMs: 1712800800000,
      captureIds: [11, 12],
      keyTimestamps: [{
        capture_ids: [],
        start_ts: 1712800800000,
        end_ts: 1712800860000,
        summary: '缺少 capture_ids 的历史分段',
      }],
    })
  })

  it('知识接口返回错误时直接抛错', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ message: 'server error' }, 500))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeMemories())

    await expect(result.current({ limit: 10, offset: 0 })).rejects.toThrow('timelines fetch failed: 500')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://localhost:7070/api/knowledge?limit=10&offset=0',
    )
  })

  it('请求单条时间线详情并映射为时间线条目', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        id: 7,
        summary: '目标时间线',
        overview: '目标概览',
        capture_id: 72,
        importance: 5,
        occurrence_count: 1,
        created_at: '2026-04-11 10:00',
        created_at_ms: 1712800800000,
        capture_ids: [72],
      }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeMemory())
    const data = await result.current('7')

    expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/knowledge/7')
    expect(data).toMatchObject({
      id: '7',
      title: '目标时间线',
      sourceCaptureId: '72',
      captureIds: [72],
    })
  })
})

describe('useFetchBakeKnowledge', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('仅请求 bake knowledge 列表并保留筛选和分页参数', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      items: [{
        id: 9,
        capture_id: 12,
        summary: '已提炼知识',
        overview: '概述',
        details: '详情',
        entities: ['芝士'],
        category: 'bake_knowledge',
        importance: 4,
        occurrence_count: 2,
        observed_at: 123,
        updated_at: '2026-04-11 10:00:00',
        updated_at_ms: 456,
      }],
      total: 1,
      limit: 50,
      offset: 10,
    }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeKnowledge())
    const data = await result.current({ q: '芝士', from: 100, to: 200, limit: 50, offset: 10 })

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:7070/api/bake/knowledge?q=%E8%8A%9D%E5%A3%AB&from=100&to=200&limit=50&offset=10',
    )
    expect(data.items[0]).toMatchObject({
      id: '9',
      captureId: '12',
      category: 'bake_knowledge',
      summary: '已提炼知识',
    })
    expect(data.total).toBe(1)
    expect(data.limit).toBe(50)
    expect(data.offset).toBe(10)
  })

  it('请求单条 bake knowledge 详情', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      id: 9,
      capture_id: 12,
      summary: '已提炼知识',
      overview: '概述',
      details: '{"source_capture_ids":["12"]}',
      entities: ['芝士'],
      category: 'bake_knowledge',
      importance: 4,
      occurrence_count: 2,
      updated_at: '2026-04-11 10:00:00',
      updated_at_ms: 456,
    }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeKnowledgeDetail())
    const data = await result.current('9')

    expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge/9')
    expect(data).toMatchObject({
      id: '9',
      captureId: '12',
      sourceCaptureIds: ['12'],
      summary: '已提炼知识',
    })
  })
})

describe('useFetchBakeTemplates', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('请求文档列表时保留关键词、日期和分页参数', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      items: [{
        id: 3,
        title: '文档模板',
        doc_type: 'article',
        status: 'draft',
        tags: [],
        applicable_tasks: [],
        source_memory_ids: [],
        source_capture_ids: [],
        source_episode_ids: [],
        linked_knowledge_ids: [],
        sections: [],
        style_phrases: [],
        replacement_rules: [],
        usage_count: 0,
        review_status: 'draft',
        updated_at: '2026-04-11 10:00:00',
      }],
      total: 1,
      limit: 20,
      offset: 0,
    }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeTemplates())
    const data = await result.current({ q: '模板', from: 100, to: 200, limit: 20, offset: 0 })

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:7070/api/bake/documents?q=%E6%A8%A1%E6%9D%BF&from=100&to=200&limit=20&offset=0',
    )
    expect(data.items[0]).toMatchObject({
      id: '3',
      title: '文档模板',
      docType: 'article',
    })
  })
})

describe('useFetchBakeSops', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('请求操作列表时保留关键词、日期和分页参数', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      items: [{
        id: 5,
        source_capture_id: '',
        source_timeline_id: '5',
        trigger_keywords: ['导出'],
        confidence: 'medium',
        extracted_problem: '导出文档',
        detailed_content: '',
        steps: ['点击导出'],
        linked_knowledge_ids: [],
        linked_knowledge_summaries: [],
        status: 'confirmed',
        created_at: '2026-04-11 10:00:00',
        created_at_ms: 100,
        updated_at: '2026-04-11 10:00:00',
        updated_at_ms: 100,
      }],
      total: 1,
      limit: 20,
      offset: 0,
    }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeSops())
    const data = await result.current({ q: '导出', from: 100, to: 200, limit: 20, offset: 0 })

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:7070/api/bake/sops?q=%E5%AF%BC%E5%87%BA&from=100&to=200&limit=20&offset=0',
    )
    expect(data.items[0]).toMatchObject({
      id: '5',
      extractedProblem: '导出文档',
    })
  })

  it('请求单条操作详情', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      id: 5,
      source_capture_id: '2',
      source_timeline_id: '8',
      trigger_keywords: ['导出'],
      confidence: 'medium',
      extracted_problem: '导出文档',
      detailed_content: '',
      steps: ['点击导出'],
      linked_knowledge_ids: [],
      linked_knowledge_summaries: [],
      status: 'confirmed',
      created_at: '2026-04-11 10:00:00',
      created_at_ms: 100,
      updated_at: '2026-04-11 10:00:00',
      updated_at_ms: 100,
    }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeSop())
    const data = await result.current('5')

    expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/sops/5')
    expect(data).toMatchObject({
      id: '5',
      sourceTimelineId: '8',
      extractedProblem: '导出文档',
    })
  })
})

describe('useFetchBakeCaptures', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('拼装关键词、日期和来源片段参数', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      items: [{
        id: 7,
        ts: 1710000000000,
        app_name: 'Chrome',
        app_bundle_id: 'com.google.Chrome',
        win_title: '设计稿页面',
        event_type: 'manual',
        ax_text: 'AX',
        ocr_text: 'OCR',
        input_text: '输入',
        audio_text: null,
        screenshot_path: 'foo.jpg',
        is_sensitive: false,
        pii_scrubbed: false,
        best_text: '最佳文本',
        summary: '设计稿页面',
        linked_timeline_id: null,
        linked_timeline_summary: null,
      }],
      total: 1,
      limit: 20,
      offset: 0,
    }))

    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFetchBakeCaptures())
    const data = await result.current({ q: '设计稿', from: 100, to: 200, source_capture_id: 123, limit: 20, offset: 0 })

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:7070/api/bake/captures?q=%E8%AE%BE%E8%AE%A1%E7%A8%BF&from=100&to=200&source_capture_id=123&limit=20&offset=0',
    )
    expect(data.items[0]).toMatchObject({
      id: '7',
      winTitle: '设计稿页面',
      summary: '设计稿页面',
    })
    expect(data.total).toBe(1)
  })
})
