import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import BakeMemoryGraph, { getMemoryGraphWheelScale } from '../components/bake/BakeMemoryGraph'
import {
  buildMemoryGraph,
  createMemoryGraphLayout,
  scopeMemoryGraphToDateRange,
  sliceMemoryGraph,
  type MemoryGraphAssets,
} from '../components/bake/memoryGraph'
import type { ArticleTemplate, BakeKnowledgeItem, DataSource, SopCandidate } from '../types'

const knowledge = (overrides: Partial<BakeKnowledgeItem> = {}): BakeKnowledgeItem => ({
  id: 'k1',
  captureId: '11',
  sourceCaptureIds: ['11'],
  sourceTimelineId: '101',
  summary: 'GPU 利用率口径',
  overview: '区分 GPUTL 与 SM 活跃度',
  entities: ['GPU', 'GPUTL'],
  category: '性能分析',
  importance: 8,
  occurrenceCount: 1,
  status: 'confirmed',
  reviewStatus: 'confirmed',
  createdAt: '2026-08-01',
  createdAtMs: 1,
  updatedAt: '2026-08-01',
  updatedAtMs: 1,
  ...overrides,
})

const document = (overrides: Partial<ArticleTemplate> = {}): ArticleTemplate => ({
  id: 'd1',
  title: 'GPU 指标设计',
  docType: 'design',
  status: 'enabled',
  tags: ['GPU'],
  applicableTasks: ['creation'],
  sourceMemoryIds: ['202'],
  sourceCaptureIds: ['22'],
  sourceEpisodeIds: [],
  linkedKnowledgeIds: [],
  sections: [{ title: '指标口径', keywords: ['GPUTL'] }],
  stylePhrases: [],
  replacementRules: [],
  usageCount: 0,
  reviewStatus: 'confirmed',
  createdAtMs: 2,
  ...overrides,
})

const operation = (overrides: Partial<SopCandidate> = {}): SopCandidate => ({
  id: 'o1',
  sourceCaptureId: '33',
  sourceTimelineId: '303',
  triggerKeywords: ['告警排查'],
  confidence: 'high',
  extractedProblem: 'GPU 告警排查',
  steps: ['核对 GPU 指标口径'],
  linkedKnowledgeIds: [],
  linkedKnowledgeSummaries: [],
  status: 'confirmed',
  createdAtMs: 3,
  ...overrides,
})

const data = (overrides: Partial<DataSource> = {}): DataSource => ({
  id: 7,
  title: 'GPU 周报',
  source_kind: 'work_memory',
  access_mode: 'memory_only',
  refresh_policy: 'never',
  realtime_level: 'observed',
  tags: ['GPU'],
  first_seen_at: 4,
  last_seen_at: 4,
  status: 'active',
  latest_snapshot: {
    id: 70,
    source_id: 7,
    collected_at: 4,
    collector: 'memory_extract',
    content_text: 'GPU 周报',
    structured_data: {
      title: 'GPU 利用率周报',
      summary: '跟踪 GPUTL 和 SM 活跃度',
      metric_rows: [{ dimension: '集群', metric: 'GPUTL', value: '42%' }],
    },
    content_hash: 'hash',
    freshness_ttl_seconds: 0,
    provenance: {},
    source_capture_ids: [44],
    source_timeline_ids: [404],
    status: 'success',
  },
  ...overrides,
})

const assets = (overrides: Partial<MemoryGraphAssets> = {}): MemoryGraphAssets => ({
  knowledge: [knowledge()],
  documents: [document()],
  operations: [operation()],
  data: [data()],
  ...overrides,
})

describe('memoryGraph', () => {
  it('为知识、文档、操作和数据建立稳定节点，并用提炼概念生成语义边', () => {
    const graph = buildMemoryGraph(assets())

    expect(graph.nodes.map(node => node.kind)).toEqual(expect.arrayContaining([
      'knowledge',
      'document',
      'operation',
      'data',
    ]))
    expect(graph.edges.some(edge => edge.relationType === 'semantic' && edge.evidence.includes('GPU'))).toBe(true)
    expect(buildMemoryGraph(assets()).graphVersion).toBe(graph.graphVersion)
  })

  it('从中文标题和摘要提取概念并生成语义边', () => {
    const graph = buildMemoryGraph(assets({
      knowledge: [knowledge({
        summary: '年度预算评审结论',
        overview: '明确费用科目和预算周期',
        entities: [],
        category: '',
        sourceTimelineId: 'timeline-budget',
        sourceCaptureIds: ['capture-budget'],
      })],
      documents: [document({
        title: '年度预算执行方案',
        summary: '跟踪各部门预算执行情况',
        tags: [],
        sections: [],
        docType: '',
        sourceMemoryIds: ['timeline-document'],
        sourceCaptureIds: ['capture-document'],
      })],
      operations: [],
      data: [],
    }))

    expect(graph.nodes.find(node => node.id === 'knowledge:k1')?.concepts).toContain('预算')
    expect(graph.nodes.find(node => node.id === 'document:d1')?.concepts).toContain('预算')
    expect(graph.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({
        source: 'document:d1',
        target: 'knowledge:k1',
        relationType: 'semantic',
        evidence: expect.stringContaining('预算'),
      }),
    ]))
  })

  it('过滤旧数据中 timeline id 与未来 knowledge id 的命名空间碰撞', () => {
    const graph = buildMemoryGraph(assets({
      knowledge: [knowledge({ id: '2688', sourceTimelineId: '5689' })],
      documents: [document({
        sourceMemoryIds: ['2688'],
        linkedKnowledgeIds: ['2688'],
      })],
      operations: [],
      data: [],
    }))

    expect(graph.edges.some(edge => edge.relationType === 'references')).toBe(false)
  })

  it('显式引用优先于同一节点对的语义关系', () => {
    const graph = buildMemoryGraph(assets({
      documents: [document({ linkedKnowledgeIds: ['k1'] })],
    }))
    const edge = graph.edges.find(item => (
      new Set([item.source, item.target]).has('knowledge:k1')
      && new Set([item.source, item.target]).has('document:d1')
    ))

    expect(edge?.relationType).toBe('references')
    expect(edge?.generationSource).toBe('explicit')
  })

  it('共享时间线会生成共同来源边，但仅时间接近不会生成边', () => {
    const sharedGraph = buildMemoryGraph(assets({
      documents: [document({ sourceMemoryIds: ['404'] })],
      data: [data()],
    }))
    expect(sharedGraph.edges.some(edge => edge.relationType === 'shared_source')).toBe(true)

    const unrelatedGraph = buildMemoryGraph(assets({
      knowledge: [knowledge({
        summary: '年度预算口径',
        overview: '财务预算与费用科目',
        entities: ['预算'],
        category: '财务',
        sourceTimelineId: '1',
        sourceCaptureIds: ['1'],
      })],
      documents: [],
      operations: [],
      data: [data({
        tags: ['GPU'],
        first_seen_at: 1,
        latest_snapshot: {
          ...data().latest_snapshot!,
          source_timeline_ids: [2],
          source_capture_ids: [2],
        },
      })],
    }))
    expect(unrelatedGraph.edges).toHaveLength(0)
  })

  it('旧响应中的 timeline/capture 同号不会生成知识共同来源假边', () => {
    const graph = buildMemoryGraph(assets({
      knowledge: [
        knowledge({
          id: '224',
          captureId: '538',
          sourceTimelineId: '538',
          sourceCaptureIds: ['5543'],
          summary: '调试 Desktop UI',
          overview: '排查本地界面问题',
          entities: ['Desktop'],
        }),
        knowledge({
          id: '2683',
          captureId: '5543',
          sourceTimelineId: '5543',
          sourceCaptureIds: [],
          summary: '万擎定价文档',
          overview: '查看平台 SKU 定价',
          entities: ['万擎'],
        }),
      ],
      documents: [],
      operations: [],
      data: [],
    }))

    expect(graph.edges.some(edge => edge.relationType === 'shared_source')).toBe(false)
  })

  it('知识确实共享真实 capture 时仍生成共同来源边', () => {
    const graph = buildMemoryGraph(assets({
      knowledge: [
        knowledge({
          id: 'first',
          captureId: '42',
          sourceTimelineId: '100',
          sourceCaptureIds: ['42'],
          summary: '第一条独立知识',
          overview: '主题甲',
          entities: ['主题甲'],
        }),
        knowledge({
          id: 'second',
          captureId: '42',
          sourceTimelineId: '200',
          sourceCaptureIds: ['42'],
          summary: '第二条独立知识',
          overview: '主题乙',
          entities: ['主题乙'],
        }),
      ],
      documents: [],
      operations: [],
      data: [],
    }))

    expect(graph.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({ relationType: 'shared_source', generationSource: 'shared_source' }),
    ]))
  })

  it('聚焦切片始终保留当前节点并生成稳定布局', () => {
    const graph = buildMemoryGraph(assets())
    const slice = sliceMemoryGraph(graph, { focusNodeId: 'knowledge:k1', maxNodes: 2 })
    expect(slice.nodes.some(node => node.id === 'knowledge:k1')).toBe(true)

    const first = createMemoryGraphLayout(slice.nodes, slice.edges, { focusNodeId: 'knowledge:k1' })
    const second = createMemoryGraphLayout(slice.nodes, slice.edges, { focusNodeId: 'knowledge:k1' })
    expect(second).toEqual(first)
  })

  it('总览切片优先保留出现次数更高的知识', () => {
    const graph = buildMemoryGraph(assets({
      knowledge: [
        knowledge({ id: 'cold', summary: '低热度知识', occurrenceCount: 1, updatedAtMs: 9 }),
        knowledge({ id: 'hot', summary: '高热度知识', occurrenceCount: 12, updatedAtMs: 1 }),
      ],
      documents: [],
      operations: [],
      data: [],
    }))

    const slice = sliceMemoryGraph(graph, { maxNodes: 1, preferHeat: true })
    expect(slice.nodes.map(node => node.id)).toEqual(['knowledge:hot'])
  })

  it('日期范围只保留范围内创建的节点及节点之间的关联', () => {
    const todayStart = 1_000
    const graph = buildMemoryGraph(assets({
      knowledge: [
        knowledge({ createdAtMs: todayStart + 10 }),
        knowledge({ id: 'today-related', createdAtMs: todayStart + 20 }),
      ],
      documents: [document({
        id: 'historical-related',
        title: '历史关联文档',
        createdAtMs: todayStart - 100,
        linkedKnowledgeIds: ['k1'],
      }), document({
        id: 'today-related',
        title: '今日关联文档',
        createdAtMs: todayStart + 30,
        linkedKnowledgeIds: ['today-related'],
      })],
      operations: [],
      data: [],
    }))

    const scoped = scopeMemoryGraphToDateRange(graph, {
      fromMs: todayStart,
      toMs: todayStart + 999,
    })

    expect(scoped.nodes.map(node => node.id)).toEqual(expect.arrayContaining([
      'knowledge:k1',
      'knowledge:today-related',
      'document:today-related',
    ]))
    expect(scoped.nodes.map(node => node.id)).not.toContain('document:historical-related')
    expect(scoped.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({
        source: 'document:today-related',
        target: 'knowledge:today-related',
        relationType: 'references',
      }),
    ]))
    expect(scoped.edges).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'document:historical-related' }),
    ]))
  })
})

describe('BakeMemoryGraph', () => {
  it('支持节点打开、展示创建时间和全屏退出', () => {
    const onOpenNode = vi.fn()
    const createdAtMs = new Date(2026, 7, 1, 10, 30).getTime()
    render(<BakeMemoryGraph
      assets={assets({ knowledge: [knowledge({ createdAtMs })] })}
      focusNodeId="knowledge:k1"
      onOpenNode={onOpenNode}
    />)

    expect(screen.getByRole('region', { name: '今日记忆' })).toBeInTheDocument()
    expect(screen.getByText('今日记忆')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '今日记忆' })).not.toBeInTheDocument()
    expect(screen.queryByText('语义记忆网络')).not.toBeInTheDocument()
    expect(screen.queryByText('按来源、引用与提炼语义连接知识、文档、操作和数据')).not.toBeInTheDocument()
    const createdTime = screen.getByText(/创建于 2026\/08\/01 10:30/)
    expect(createdTime).toHaveAttribute('dateTime', new Date(createdAtMs).toISOString())

    fireEvent.click(screen.getByRole('button', { name: '打开内容' }))
    expect(onOpenNode).toHaveBeenCalledWith(expect.objectContaining({ id: 'knowledge:k1' }))

    const knowledgeNode = screen.getByRole('button', { name: /知识：GPU 利用率口径/ })
    fireEvent.doubleClick(knowledgeNode)
    expect(onOpenNode).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: '全屏查看图谱' }))
    expect(screen.getByRole('dialog', { name: '今日记忆' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '今日记忆' })).not.toBeInTheDocument()
  })

  it('dock 模式使用详情抽屉形式，并支持 Escape 和遮罩关闭', () => {
    const onClose = vi.fn()
    const { rerender } = render(
      <BakeMemoryGraph assets={assets()} mode="dock" onClose={onClose} />,
    )

    const drawer = screen.getByRole('dialog', { name: '记忆图谱' })
    expect(drawer).toHaveClass('bake-memory-graph--dock')
    expect(drawer.parentElement).toHaveClass('bake-memory-graph-drawer-overlay')
    expect(screen.getAllByText('记忆图谱')).toHaveLength(1)
    expect(screen.queryByText('语义记忆网络')).not.toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)

    onClose.mockClear()
    fireEvent.mouseDown(drawer.parentElement!)
    expect(onClose).toHaveBeenCalledTimes(1)

    rerender(<BakeMemoryGraph assets={assets()} mode="dock" onClose={onClose} />)
  })

  it('总览支持搜索记忆节点', () => {
    render(<BakeMemoryGraph assets={assets({
      knowledge: [
        knowledge(),
        knowledge({
          id: 'budget',
          summary: '年度预算口径',
          overview: '费用科目与预算周期',
          entities: ['预算'],
          category: '财务',
          sourceTimelineId: '505',
          sourceCaptureIds: ['55'],
        }),
      ],
    })} />)

    fireEvent.change(screen.getByRole('searchbox', { name: '搜索记忆图谱' }), {
      target: { value: '年度预算' },
    })

    expect(screen.getByText('匹配 1 项')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /知识：年度预算口径/ })).toHaveClass('bake-memory-graph__node--search-match')
  })

  it('总览搜索可以加载当前热门候选之外的节点', async () => {
    const onSearchAssets = vi.fn().mockResolvedValue(assets({
      knowledge: [knowledge({
        id: 'remote-budget',
        summary: '历史预算规则',
        overview: '搜索返回的预算记忆',
        entities: ['预算'],
      })],
      documents: [],
      operations: [],
      data: [],
    }))
    render(<BakeMemoryGraph assets={assets()} onSearchAssets={onSearchAssets} />)

    fireEvent.change(screen.getByRole('searchbox', { name: '搜索记忆图谱' }), {
      target: { value: '历史预算' },
    })

    await waitFor(() => expect(onSearchAssets).toHaveBeenCalledWith('历史预算'))
    expect(await screen.findByRole('button', { name: /知识：历史预算规则/ })).toHaveClass('bake-memory-graph__node--search-match')
  })

  it('总览默认只展示今日创建的节点', () => {
    render(<BakeMemoryGraph
      assets={assets({
        knowledge: [knowledge({ createdAtMs: 1_100 })],
        documents: [document({
          id: 'historical-related',
          title: '历史关联文档',
          createdAtMs: 900,
          linkedKnowledgeIds: ['k1'],
        })],
        operations: [operation({
          id: 'historical-unrelated',
          extractedProblem: '历史无关操作',
          triggerKeywords: ['完全不同'],
          steps: ['处理其他事项'],
          createdAtMs: 900,
        })],
        data: [],
      })}
      defaultDateRange={{ fromMs: 1_000, toMs: 1_999 }}
      defaultScopeLabel="今日"
    />)

    expect(screen.getByText('今日展示 1/1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /知识：GPU 利用率口径/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /文档：历史关联文档/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /操作：历史无关操作/ })).not.toBeInTheDocument()
  })

  it('滚轮缩放使用更高灵敏度', () => {
    expect(getMemoryGraphWheelScale(1, -100)).toBeGreaterThan(1.3)
    expect(getMemoryGraphWheelScale(1, 100)).toBeLessThan(0.8)
  })
})
