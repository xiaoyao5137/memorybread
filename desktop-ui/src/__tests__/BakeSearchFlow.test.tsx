import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import RepositoryPanel from '../components/RepositoryPanel'
import BakePanel from '../components/BakePanel'
import { useAppStore } from '../store/useAppStore'

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

const overviewResponse = {
  capture_count: 0,
  memory_count: 0,
  knowledge_count: 0,
  template_count: 0,
  pending_candidates: 0,
  recent_activities: [],
}

const styleConfigResponse = {
  preferredPhrases: [],
  replacementRules: [],
  styleSamples: [],
  applyToCreation: true,
  applyToTemplateEditing: true,
}

describe('显式搜索交互', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  it('BakePanel 知识搜索只有点击搜索后才发起带关键词请求', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/models')) return jsonResponse({ ollama: true, llm: true, embedding: true })
      if (url.includes('/api/bake/overview')) return jsonResponse(overviewResponse)
      if (url.includes('/api/bake/knowledge')) return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ bakeTab: 'knowledge' })

    render(<BakePanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?limit=20&offset=0')
    })

    const callsBeforeTyping = fetchMock.mock.calls.length
    fireEvent.change(screen.getByPlaceholderText('搜索知识标题、内容、分类或来源 URL'), { target: { value: '芝士' } })

    expect(fetchMock).toHaveBeenCalledTimes(callsBeforeTyping)

    fireEvent.click(screen.getByRole('button', { name: '搜索' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?q=%E8%8A%9D%E5%A3%AB&limit=20&offset=0')
    })
    expect(screen.queryByText('关键词：芝士')).not.toBeInTheDocument()
  })

  it('总览趋势保留服务端全量统计，不被热度 Top 100 列表覆盖', async () => {
    const now = new Date()
    const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
    const dayLabel = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/models')) return jsonResponse({ ollama: true, llm: true, embedding: true })
      if (url.includes('/api/bake/overview')) {
        return jsonResponse({
          ...overviewResponse,
          knowledge_count: 2066,
          template_count: 6,
          inventory_trend: [{
            label: dayLabel,
            start_ts: dayStart,
            end_ts: dayStart + 86_400_000 - 1,
            memory_count: 179,
            knowledge_count: 6,
            template_count: 1,
            sop_count: 0,
            data_count: 0,
          }],
        })
      }
      if (url.includes('/api/bake/knowledge')) {
        // 历史高热条目占满列表上限，今日 6 条不在这个素材集中。
        return jsonResponse({ items: [], total: 2066, limit: 100, offset: 0 })
      }
      if (url.includes('/api/bake/documents')) {
        return jsonResponse({
          items: [{
            id: 1,
            title: '今日文档',
            doc_type: 'article',
            status: 'enabled',
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
            review_status: 'confirmed',
            created_at_ms: dayStart + 1_000,
          }],
          total: 6,
          limit: 100,
          offset: 0,
        })
      }
      if (url.includes('/api/bake/sops')) return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 })
      if (url.includes('/api/data/sources')) return jsonResponse({ items: [], total: 0 })
      if (url.includes('/api/knowledge')) return jsonResponse({ entries: [], total: 0, limit: 1, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ bakeTab: 'overview' })
    const { container } = render(<BakePanel />)

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/bake/knowledge'))).toBe(true)
    })
    const chart = await waitFor(() => {
      const element = container.querySelector('.bake-trend-chart') as HTMLDivElement | null
      expect(element).not.toBeNull()
      return element as HTMLDivElement
    })
    vi.spyOn(chart, 'getBoundingClientRect').mockReturnValue({
      width: 720,
      height: 248,
      top: 0,
      right: 720,
      bottom: 248,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    })
    fireEvent.pointerMove(chart, { clientX: 360, clientY: 80 })

    await waitFor(() => {
      expect(container.querySelector('.bake-trend-tooltip')).toHaveTextContent('知识6')
    })
  })

  it('BakePanel 知识搜索无结果后不保留旧详情', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/models')) return jsonResponse({ ollama: true, llm: true, embedding: true })
      if (url.includes('/api/bake/overview')) return jsonResponse(overviewResponse)
      if (url.includes('/api/bake/knowledge?q=')) return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })
      if (url.includes('/api/bake/knowledge')) return jsonResponse({
        items: [{
          id: 7,
          summary: '旧知识条目',
          overview: '旧知识详情',
          details: '',
          category: '文档',
          importance: 4,
          occurrence_count: 1,
          status: 'confirmed',
          review_status: 'confirmed',
          entities: [],
          created_at: '2026-04-11 09:30',
          created_at_ms: 0,
          updated_at: '2026-04-11 09:30',
          updated_at_ms: 0,
        }],
        total: 1,
        limit: 20,
        offset: 0,
      })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ bakeTab: 'knowledge' })

    render(<BakePanel />)

    expect((await screen.findAllByText('旧知识条目')).length).toBeGreaterThan(0)

    fireEvent.change(screen.getByPlaceholderText('搜索知识标题、内容、分类或来源 URL'), { target: { value: '不存在' } })
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))

    await waitFor(() => {
      expect(screen.getByText('没有符合条件的知识')).toBeInTheDocument()
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('关键词：不存在')).not.toBeInTheDocument()
  })

  it('BakePanel 知识页在没有 bake knowledge 时显示明确空态', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/models')) return jsonResponse({ ollama: true, llm: true, embedding: true })
      if (url.includes('/api/bake/overview')) return jsonResponse(overviewResponse)
      if (url.includes('/api/bake/knowledge')) return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ bakeTab: 'knowledge' })

    render(<BakePanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?limit=20&offset=0')
    })

    expect(screen.getByText('暂无知识')).toBeInTheDocument()
  })

  it('BakePanel 关联知识跳转只展示目标知识，不展示额外筛选提示', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/models')) return jsonResponse({ ollama: true, llm: true, embedding: true })
      if (url.includes('/api/bake/overview')) return jsonResponse(overviewResponse)
      if (url === 'http://localhost:7070/api/bake/knowledge/9') {
        return jsonResponse({
          id: 9,
          capture_id: 12,
          summary: '目标知识',
          overview: '目标详情',
          details: '',
          category: '文档',
          importance: 4,
          occurrence_count: 1,
          status: 'confirmed',
          review_status: 'confirmed',
          entities: [],
          updated_at: '2026-04-11 10:00:00',
          updated_at_ms: 1,
        })
      }
      if (url === 'http://localhost:7070/api/bake/knowledge?limit=20&offset=0') {
        return jsonResponse({
          items: [{
            id: 8,
            capture_id: 11,
            summary: '普通知识',
            overview: '普通详情',
            details: '',
            category: '文档',
            importance: 3,
            occurrence_count: 1,
            status: 'confirmed',
            review_status: 'confirmed',
            entities: [],
            updated_at: '2026-04-11 09:00:00',
            updated_at_ms: 1,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.includes('/api/knowledge')) return jsonResponse({ entries: [], total: 0, limit: 1000, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({
      bakeTab: 'knowledge',
      bakeKnowledgeFocusId: '9',
      selectedKnowledgeId: '9',
    })

    render(<BakePanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge/9')
    })
    expect(screen.getAllByText('目标知识').length).toBeGreaterThan(0)
    expect(screen.queryByText('普通知识')).not.toBeInTheDocument()
    expect(screen.queryByText('仅看知识 #9')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看全部' })).not.toBeInTheDocument()
  })

  it('RepositoryPanel 以普通元信息展示 ID 和创建时间，不展示不可靠指标', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/knowledge')) {
        return jsonResponse({
          entries: [{
            id: 1,
            summary: '周报情节记忆',
            overview: '整理周报提纲',
            capture_id: 42,
            importance: 6,
            occurrence_count: 3,
            created_at: '2026-04-11 09:30',
            created_at_ms: 0,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'memory' })

    render(<RepositoryPanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/knowledge?limit=20&offset=0')
    })

    expect(screen.getByText('采集')).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /2026-04-11 09:30 ID #1/ })).toBeInTheDocument()
    expect(screen.queryByText(/停留/)).not.toBeInTheDocument()
    expect(screen.queryByText(/打开 \d+ 次/)).not.toBeInTheDocument()
    expect(screen.queryByText(/重复观察 \d+ 次/)).not.toBeInTheDocument()
    expect(screen.queryByText(/权重 \d+/)).not.toBeInTheDocument()
  })

  it('RepositoryPanel 时间线默认仅展示表格，点击操作后再打开详情', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/knowledge?limit=20&offset=0') {
        return jsonResponse({
          entries: [{
            id: 1,
            summary: '第一条时间线',
            overview: '第一条时间线详情',
            capture_id: 41,
            importance: 6,
            occurrence_count: 3,
            created_at: '2026-04-11 09:30',
            created_at_ms: 0,
            capture_ids: [],
          }, {
            id: 2,
            summary: '第二条时间线',
            overview: '第二条时间线详情',
            capture_id: 42,
            importance: 5,
            occurrence_count: 2,
            created_at: '2026-04-11 09:20',
            created_at_ms: 0,
            capture_ids: [],
          }],
          total: 2,
          limit: 20,
          offset: 0,
        })
      }
      if (url.includes('/api/bake/documents') || url.includes('/api/bake/knowledge') || url.includes('/api/bake/sops')) {
        return jsonResponse({ items: [], total: 0, limit: 1000, offset: 0 })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({
      repositoryTab: 'memory',
      selectedMemoryId: 'stale-selection',
    })

    render(<RepositoryPanel />)

    expect(await screen.findByRole('table', { name: '时间线表格' })).toBeInTheDocument()
    await waitFor(() => expect(useAppStore.getState().selectedMemoryId).toBeNull())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看时间线：第一条时间线' }))
    const drawer = screen.getByRole('dialog', { name: '第一条时间线' })
    expect(within(drawer).getByText('第一条时间线详情')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('RepositoryPanel 时间线详情补齐 keyTimestamps 未覆盖的关联采集', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/knowledge?limit=20&offset=0') {
        return jsonResponse({
          entries: [{
            id: 5034,
            summary: '面试时间线',
            overview: '面试时间线详情',
            capture_id: 1,
            importance: 5,
            occurrence_count: 4,
            created_at: '2026-08-15 10:10',
            created_at_ms: 1786759850978,
            capture_ids: [1, 2, 3],
            key_timestamps: [
              { capture_ids: [1], start_ts: 1000, end_ts: 1000, summary: '初始片段' },
            ],
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.includes('localhost:7070/captures?') && url.includes('ids=')) {
        return jsonResponse({
          total: 3,
          captures: [
            { id: 1, ts: 1000, app_name: 'Chrome', win_title: '面试', ax_text: '第一段', ocr_text: '' },
            { id: 2, ts: 5000, app_name: 'Chrome', win_title: '面试', ax_text: '第二段', ocr_text: '' },
            { id: 3, ts: 9000, app_name: 'Chrome', win_title: '面试', ax_text: '第三段', ocr_text: '' },
          ],
        })
      }
      if (url.includes('/api/bake/documents') || url.includes('/api/bake/knowledge') || url.includes('/api/bake/sops')) {
        return jsonResponse({ items: [], total: 0, limit: 1000, offset: 0 })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'memory' })
    render(<RepositoryPanel />)

    expect(await screen.findByRole('table', { name: '时间线表格' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看时间线：面试时间线' }))
    const drawer = await screen.findByRole('dialog', { name: '面试时间线' })
    // 片段只覆盖 capture 1，其余两条应按应用/窗口补齐展示，与列表页 3 条关联采集一致
    await waitFor(() => {
      expect(within(drawer).getByText('#3')).toBeInTheDocument()
    })
    expect(within(drawer).getByText('#1')).toBeInTheDocument()
    expect(within(drawer).getByText('#2')).toBeInTheDocument()
  })

  it('RepositoryPanel 时间线支持按需展开记忆图谱', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/knowledge?limit=20&offset=0') {
        return jsonResponse({ entries: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.includes('/api/bake/knowledge')) return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 })
      if (url.includes('/api/bake/documents')) return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 })
      if (url.includes('/api/bake/sops')) return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 })
      if (url.includes('/api/data/sources')) return jsonResponse({ items: [], total: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'memory' })
    render(<RepositoryPanel />)

    const graphButton = screen.getByRole('button', { name: '展开记忆图谱' })
    expect(screen.queryByRole('region', { name: '记忆图谱' })).not.toBeInTheDocument()
    fireEvent.click(graphButton)

    expect(await screen.findByRole('region', { name: '记忆图谱' })).toBeInTheDocument()
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?sort=heat&limit=100&offset=0')
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/documents?limit=100&offset=0')
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/sops?limit=100&offset=0')
      expect(fetchMock.mock.calls.some(([input]) => (
        String(input) === 'http://localhost:7070/api/data/sources?limit=100&offset=0'
      ))).toBe(true)
    })
  })

  it('RepositoryPanel 情节记忆搜索只有点击搜索后才发起带筛选请求', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/knowledge')) return jsonResponse({ entries: [], total: 0, limit: 20, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'memory' })

    render(<RepositoryPanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/knowledge?limit=20&offset=0')
    })

    const callsBeforeTyping = fetchMock.mock.calls.length
    fireEvent.change(screen.getByPlaceholderText('搜索时间线标题、摘要或详情'), { target: { value: '周报' } })
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-04-01' } })
    fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-04-11' } })

    const timelineSearchButton = screen.getByRole('button', { name: '搜索' })
    const timelineClearButton = screen.getByRole('button', { name: '清空' })
    const timelineActions = timelineSearchButton.closest('.bake-list-toolbar__repository-primary-actions')
    expect(timelineActions).toContainElement(timelineClearButton)
    expect(screen.getByText('关键词').closest('.bake-list-toolbar__repository-row--search')).not.toContainElement(timelineSearchButton)
    expect(fetchMock).toHaveBeenCalledTimes(callsBeforeTyping)

    fireEvent.click(timelineSearchButton)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/knowledge?q=%E5%91%A8%E6%8A%A5&from=1774972800000&to=1775923199999&limit=20&offset=0')
    })
    expect(screen.queryByText('关键词：周报')).not.toBeInTheDocument()
  })

  it('RepositoryPanel 时间线搜索无结果后不保留旧详情', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/knowledge?q=')) return jsonResponse({ entries: [], total: 0, limit: 20, offset: 0 })
      if (url.includes('/api/knowledge')) return jsonResponse({
        entries: [{
          id: 1,
          summary: '旧时间线',
          overview: '旧时间线摘要',
          capture_id: 42,
          importance: 6,
          occurrence_count: 3,
          created_at: '2026-04-11 09:30',
          created_at_ms: 0,
        }],
        total: 1,
        limit: 20,
        offset: 0,
      })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'memory' })

    render(<RepositoryPanel />)

    expect((await screen.findAllByText('旧时间线')).length).toBeGreaterThan(0)

    fireEvent.change(screen.getByPlaceholderText('搜索时间线标题、摘要或详情'), { target: { value: '不存在' } })
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))

    await waitFor(() => {
      expect(screen.getByText('没有匹配的时间线')).toBeInTheDocument()
    })
    expect(screen.queryByText('旧时间线摘要')).not.toBeInTheDocument()
    expect(screen.queryByText('关键词：不存在')).not.toBeInTheDocument()
  })

  it('RepositoryPanel 时间线跳转只展示目标时间线，空条件搜索后恢复列表', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/knowledge/7') {
        return jsonResponse({
          id: 7,
          summary: '目标时间线',
          overview: '目标时间线摘要',
          capture_id: 71,
          importance: 5,
          occurrence_count: 1,
          created_at: '2026-04-11 10:00',
          created_at_ms: 1,
          capture_ids: [],
        })
      }
      if (url === 'http://localhost:7070/api/knowledge?limit=20&offset=0') {
        return jsonResponse({
          entries: [{
            id: 6,
            summary: '普通时间线',
            overview: '普通时间线摘要',
            capture_id: 61,
            importance: 3,
            occurrence_count: 1,
            created_at: '2026-04-11 09:00',
            created_at_ms: 1,
            capture_ids: [],
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.includes('/api/bake/documents') || url.includes('/api/bake/knowledge') || url.includes('/api/bake/sops')) {
        return jsonResponse({ items: [], total: 0, limit: 1000, offset: 0 })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({
      repositoryTab: 'memory',
      repositoryMemoryFocusId: '7',
      selectedMemoryId: '7',
    })

    render(<RepositoryPanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/knowledge/7')
    })
    expect(screen.getAllByText('目标时间线').length).toBeGreaterThan(0)
    expect(screen.queryByText('仅看时间线 #7')).not.toBeInTheDocument()
    expect(screen.queryByText('普通时间线')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看全部' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '搜索' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/knowledge?limit=20&offset=0')
    })
    expect(useAppStore.getState().repositoryMemoryFocusId).toBeNull()
  })

  it('RepositoryPanel 采集记录不显示查看全部，空条件搜索会清掉来源范围', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/bake/captures')) return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({
      repositoryTab: 'capture',
      repositoryCaptureSourceCaptureId: '123',
    })

    render(<RepositoryPanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/captures?source_capture_id=123&limit=20&offset=0')
    })
    expect(screen.queryByText('仅看来源 ID #123')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看全部' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '搜索' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/captures?limit=20&offset=0')
    })
    expect(useAppStore.getState().repositoryCaptureSourceCaptureId).toBeNull()
  })

  it('RepositoryPanel 采集记录支持按应用筛选，并可一键清除', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/bake/captures')) return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'capture' })
    render(<RepositoryPanel />)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/captures?limit=20&offset=0')
    })

    const callsBeforeTyping = fetchMock.mock.calls.length
    fireEvent.change(screen.getByLabelText('应用'), { target: { value: 'Safari' } })
    const captureSearchButton = screen.getByRole('button', { name: '搜索' })
    const captureClearButton = screen.getByRole('button', { name: '清空' })
    const captureActions = captureSearchButton.closest('.bake-list-toolbar__repository-primary-actions')
    expect(captureActions).toContainElement(captureClearButton)
    expect(screen.getByText('关键词').closest('.bake-list-toolbar__repository-row--search')).not.toContainElement(captureSearchButton)
    expect(fetchMock).toHaveBeenCalledTimes(callsBeforeTyping)

    fireEvent.click(captureSearchButton)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/captures?app=Safari&limit=20&offset=0')
    })
    expect(useAppStore.getState().repositoryCaptureApp).toBe('Safari')

    fireEvent.click(screen.getByRole('button', { name: '清空' }))

    await waitFor(() => {
      expect(useAppStore.getState().repositoryCaptureApp).toBe('')
      expect(fetchMock.mock.calls.filter(([input]) => (
        String(input) === 'http://localhost:7070/api/bake/captures?limit=20&offset=0'
      )).length).toBeGreaterThanOrEqual(2)
    })
  })

  it('RepositoryPanel 采集日期筛选不重复展示开始和结束摘要', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/bake/captures')) return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({
      repositoryTab: 'capture',
      repositoryCaptureFrom: '2026-07-23',
      repositoryCaptureTo: '2026-07-23',
    })

    render(<RepositoryPanel />)

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => {
        const url = String(input)
        return url.includes('/api/bake/captures?from=')
          && url.includes('&to=')
          && url.includes('&limit=20&offset=0')
      })).toBe(true)
    })

    expect(screen.getByLabelText('开始日期')).toHaveValue('2026-07-23')
    expect(screen.getByLabelText('结束日期')).toHaveValue('2026-07-23')
    expect(screen.queryByText('开始：2026-07-23')).not.toBeInTheDocument()
    expect(screen.queryByText('结束：2026-07-23')).not.toBeInTheDocument()
  })

  it('RepositoryPanel 会自动刷新等待 OCR 回写的采集详情', async () => {
    const captureTs = Date.now()
    let detailRequestCount = 0
    const capture = {
      id: 26238,
      ts: captureTs,
      app_name: 'ChatGPT',
      win_title: 'ChatGPT',
      event_type: 'key_pause',
      screenshot_path: 'screenshots/26238.jpg',
      ax_text: null,
      ocr_text: null,
      is_sensitive: false,
      pii_scrubbed: false,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/bake/captures/26238') {
        detailRequestCount += 1
        return jsonResponse({
          ...capture,
          ocr_text: detailRequestCount >= 2 ? 'OCR 回写后的文本' : null,
        })
      }
      if (url === 'http://localhost:7070/api/bake/captures?limit=20&offset=0') {
        return jsonResponse({ items: [capture], total: 1, limit: 20, offset: 0 })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'capture' })
    render(<RepositoryPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '查看采集记录 #26238 详情' }))

    expect(await screen.findByText('文本识别中，完成后将自动显示…')).toBeInTheDocument()
    expect(await screen.findByText('OCR 回写后的文本', {}, { timeout: 3_500 })).toBeInTheDocument()
    expect(detailRequestCount).toBe(2)
  })

  it('RepositoryPanel 从时间线点击采集记录会限定到对应采集片段', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/knowledge')) {
        return jsonResponse({
          entries: [{
            id: 1,
            summary: '周报情节记忆',
            overview: '整理周报提纲',
            capture_id: 41,
            importance: 6,
            occurrence_count: 3,
            created_at: '2026-04-11 09:30',
            created_at_ms: 0,
            capture_ids: [41, 42],
            keyTimestamps: [{
              start_ts: 1710000000000,
              end_ts: 1710000001000,
              summary: '旧版分段没有采集 ID',
            }],
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url === 'http://localhost:7070/captures?limit=500&ids=41%2C42') {
        return jsonResponse({
          total: 2,
          captures: [
            { id: 41, ts: 1710000000000, app_name: 'Chrome', win_title: '旧页面', ax_text: '旧片段' },
            { id: 42, ts: 1710000001000, app_name: 'Chrome', win_title: '目标页面', ax_text: '目标片段' },
          ],
        })
      }
      if (url === 'http://localhost:7070/api/bake/captures/42') {
        return jsonResponse({
          id: 42,
          ts: 1710000001000,
          app_name: 'Chrome',
          win_title: '目标页面',
          event_type: 'manual',
          ax_text: '目标片段',
          is_sensitive: false,
          pii_scrubbed: false,
        })
      }
      if (url.includes('/api/bake/captures')) {
        return jsonResponse({
          items: [{
            id: 42,
            ts: 1710000001000,
            app_name: 'Chrome',
            win_title: '目标页面',
            event_type: 'manual',
            ax_text: '目标片段',
            is_sensitive: false,
            pii_scrubbed: false,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    useAppStore.setState({ repositoryTab: 'memory' })

    render(<RepositoryPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '查看时间线：周报情节记忆' }))
    await waitFor(() => {
      expect(screen.getByText('#42')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('#42'))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/captures?source_capture_id=42&limit=20&offset=0')
    })
    expect(useAppStore.getState().repositoryCaptureSourceCaptureId).toBe('42')
    expect(useAppStore.getState().selectedCaptureId).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: '查看采集记录 #42 详情' }))

    expect(useAppStore.getState().selectedCaptureId).toBe('42')
    expect(await screen.findByRole('dialog', { name: '目标页面' })).toBeInTheDocument()
  })
})
