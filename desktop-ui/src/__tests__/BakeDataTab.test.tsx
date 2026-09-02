import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import BakeDataTab from '../components/bake/BakeDataTab'
import type { DataSource } from '../types'

const gpuSource: DataSource = {
  id: 22,
  title: '容器云 GPU 指标采集项目',
  source_kind: 'work_memory',
  access_mode: 'memory_only',
  refresh_policy: 'never',
  realtime_level: 'observed',
  tags: ['work_memory'],
  first_seen_at: 1,
  last_seen_at: 2,
  last_collected_at: Date.now(),
  status: 'active',
  latest_snapshot: {
    id: 220,
    source_id: 22,
    collected_at: Date.now(),
    observed_at: Date.now(),
    collector: 'memory_extract',
    content_text: '背景显示国内日均 GPU 利用率为 42%，海外为 47%，但 GPUTL 无法反映硅片内 SM 的实际使用情况，存在掩盖低效的事实',
    structured_data: {
      extraction_version: 'data-memory.v13',
      title: 'GPU 利用率对比',
      summary: '日均 GPU 利用率：国内 42%，海外 47%；GPUTL 无法反映 SM 实际使用，可能掩盖实际低效',
      metric_rows: [
        { dimension: '国内', metric: '日均 GPU 利用率', value: '42%', note: 'GPUTL 可能掩盖实际低效' },
        { dimension: '海外', metric: '日均 GPU 利用率', value: '47%', note: '' },
      ],
    },
    content_hash: 'gpu-hash',
    freshness_ttl_seconds: 0,
    provenance: { source: 'timeline' },
    source_capture_ids: [42],
    source_timeline_ids: [71],
    status: 'success',
  },
}

const orderSource: DataSource = {
  ...gpuSource,
  id: 23,
  title: '项目群周度数据',
  latest_snapshot: {
    ...gpuSource.latest_snapshot!,
    id: 230,
    source_id: 23,
    content_text: '本周订单 1200，环比增长 8%',
    structured_data: {
      extraction_version: 'data-memory.v13',
      title: '订单规模与环比变化',
      summary: '本周订单 1200，环比增长 8%',
      metric_rows: [
        { dimension: '本周', metric: '订单', value: '1200', note: '' },
        { dimension: '', metric: '环比增长', value: '8%', note: '' },
      ],
    },
    source_timeline_ids: [72],
  },
}

const reportSource: DataSource = {
  ...gpuSource,
  id: 24,
  title: 'GPU 实时看板',
  source_kind: 'report_url',
  source_url: 'https://bi.example.com/dashboard/gpu',
  access_mode: 'browser_session',
  refresh_policy: 'on_demand',
  realtime_level: 'live',
  latest_snapshot: {
    ...gpuSource.latest_snapshot!,
    id: 240,
    source_id: 24,
  },
}

const qconSource: DataSource = {
  ...gpuSource,
  id: 25,
  title: 'QCon上海2026大会时间',
  latest_snapshot: {
    ...gpuSource.latest_snapshot!,
    id: 250,
    source_id: 25,
    structured_data: {
      extraction_version: 'data-memory.v16',
      title: 'QCon上海2026大会时间',
      summary: '',
      metric_rows: [{
        dimension: 'QCon上海2026_全球软件开发大会暨智能软件开发生态展_InfoQ技术大会',
        metric: '会议日期范围',
        value: '10月22日-10月24日',
        note: '',
      }],
    },
  },
}

const renderDataTab = (overrides: Partial<React.ComponentProps<typeof BakeDataTab>> = {}) => {
  const props: React.ComponentProps<typeof BakeDataTab> = {
    items: [gpuSource],
    total: 1,
    limit: 20,
    offset: 0,
    draftQuery: '',
    selectedId: 22,
    loading: false,
    refreshingId: null,
    deletingId: null,
    onDraftQueryChange: vi.fn(),
    onSearch: vi.fn(),
    onClearSearch: vi.fn(),
    onSelect: vi.fn(),
    onPageChange: vi.fn(),
    onLimitChange: vi.fn(),
    onRefresh: vi.fn(),
    onDelete: vi.fn(),
    onViewTimeline: vi.fn(),
    ...overrides,
  }
  render(<BakeDataTab {...props} />)
  return props
}

describe('BakeDataTab', () => {
  it('以完整表格展示数据，并按需打开详情抽屉', () => {
    renderDataTab()

    expect(screen.getByRole('table', { name: '数据表格' })).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: '数据表格' })).queryByRole('columnheader', { name: '状态' })).not.toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: '数据表格' })).getByRole('columnheader', { name: '创建时间' })).toBeInTheDocument()
    expect(screen.getByText('GPU 利用率对比')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    const dataActionRow = screen.getByRole('button', { name: '搜索' }).closest('.bake-list-toolbar__repository-actions--secondary')
    expect(dataActionRow?.parentElement).toHaveClass('bake-list-toolbar__repository')
    expect(dataActionRow?.parentElement).not.toHaveClass('bake-list-toolbar__repository-row--asset-filters')
    fireEvent.click(screen.getByRole('button', { name: '查看数据：GPU 利用率对比' }))
    const drawer = screen.getByRole('dialog', { name: 'GPU 利用率对比' })
    expect(within(drawer).getByText(/数据 #22 · 工作记录 · 近期数据 · 数据时间/)).toBeInTheDocument()
    expect(within(drawer).queryByText('近期数据')).not.toBeInTheDocument()
    expect(within(drawer).getByText(/日均 GPU 利用率：国内 42%，海外 47%/)).toBeInTheDocument()
    const table = within(drawer).getByRole('table')
    expect(within(table).getByRole('columnheader', { name: '对象 / 范围' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: '指标' })).toBeInTheDocument()
    expect(within(table).getByText('国内')).toBeInTheDocument()
    expect(within(table).getByText('海外')).toBeInTheDocument()
    expect(within(table).getByText('42%')).toBeInTheDocument()
    expect(within(table).getByText('47%')).toBeInTheDocument()
    expect(within(table).getByText('GPUTL 可能掩盖实际低效')).toBeInTheDocument()
    expect(within(drawer).getByText('该数据表共 2 行指标。')).toBeInTheDocument()
    expect(within(drawer).getByText('完整采集内容')).toBeInTheDocument()
    expect(within(drawer).queryByText('查看完整采集内容')).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('展示数据 ID，支持跳转来源时间线和删除', async () => {
    const props = renderDataTab()

    fireEvent.click(screen.getByRole('button', { name: '查看数据：GPU 利用率对比' }))
    expect(screen.getByText(/数据 #22/)).toBeInTheDocument()
    expect(screen.getByText('快照 ID').parentElement).toHaveTextContent('#220')
    fireEvent.click(screen.getAllByRole('button', { name: '时间线 #71' })[0])
    expect(props.onViewTimeline).toHaveBeenCalledWith(71)
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(props.onDelete).toHaveBeenCalledWith(22)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('不显示数据生成说明、重生成入口、页内概况和待采集报表', () => {
    renderDataTab({
      items: [gpuSource, orderSource],
      total: 2,
      limit: 10,
    })

    expect(screen.queryByText('每条数据都会生成可检索的主题标题和清晰概述；相同语义的数据合并为一条，并只保留最新快照。')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重新生成数据' })).not.toBeInTheDocument()
    expect(screen.queryByText('本页数据记录')).not.toBeInTheDocument()
    expect(screen.queryByText('最近采集')).not.toBeInTheDocument()
    expect(screen.queryByText('待采集报表')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('本页数据概况')).not.toBeInTheDocument()
  })

  it('不再显示本地工作数据的来源核对提示', () => {
    renderDataTab()

    expect(screen.queryByText('这份数据从本地文档或工作消息中提取，时间较久时应结合来源时间线核对统计周期与口径。')).not.toBeInTheDocument()
  })

  it('直接展示后端返回的数据记录，不再分页后二次过滤', () => {
    renderDataTab({
      items: [gpuSource, orderSource],
      total: 2,
    })

    expect(screen.getAllByText(/本周订单 1200/).length).toBeGreaterThanOrEqual(1)
  })

  it('摘要缺失时使用完整事实句降级，不展示 SEO 标题拼接串', () => {
    renderDataTab({ items: [qconSource], selectedId: 25 })

    expect(screen.getByText('QCon上海2026大会时间为10月22日-10月24日。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看数据：QCon上海2026大会时间' }))
    const drawer = screen.getByRole('dialog', { name: 'QCon上海2026大会时间' })
    expect(within(drawer).getByText('QCon上海2026大会时间为10月22日-10月24日。')).toBeInTheDocument()
    expect(within(drawer).queryByText(/全球软件开发大会暨智能软件开发生态展.*会议日期范围 10月22日/)).not.toBeInTheDocument()
  })

  it('网页来源的数据记录并展示来源网址', () => {
    renderDataTab({
      items: [reportSource],
      selectedId: 24,
    })

    fireEvent.click(screen.getByRole('button', { name: '查看数据：GPU 利用率对比' }))
    expect(screen.getByText('来源网址')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'https://bi.example.com/dashboard/gpu' })
    expect(link).toHaveAttribute('href', 'https://bi.example.com/dashboard/gpu')
    expect(link).toHaveAttribute('target', '_blank')
    expect(screen.queryByRole('button', { name: '打开原始来源' })).not.toBeInTheDocument()
  })

  it('非网页来源的数据不展示来源网址', () => {
    renderDataTab()

    expect(screen.queryByText('来源网址')).not.toBeInTheDocument()
    expect(screen.queryByText(/^网址：/)).not.toBeInTheDocument()
  })

  it('支持新建数据和编辑现有数据', async () => {
    const onCreate = vi.fn().mockResolvedValue(true)
    const onUpdate = vi.fn().mockResolvedValue(true)
    renderDataTab({ onCreate, onUpdate })

    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    fireEvent.change(screen.getByRole('textbox', { name: '数据名称 *' }), { target: { value: '本周经营指标' } })
    fireEvent.change(screen.getByRole('textbox', { name: '第 1 行指标' }), { target: { value: '订单' } })
    fireEvent.change(screen.getByRole('textbox', { name: '第 1 行数值' }), { target: { value: '1200' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      title: '本周经营指标',
      rows: [expect.objectContaining({ metric: '订单', value: '1200' })],
    })))

    fireEvent.click(screen.getByRole('button', { name: '编辑数据：GPU 利用率对比' }))
    fireEvent.change(screen.getByRole('textbox', { name: '数据名称' }), { target: { value: 'GPU 经营指标' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(onUpdate).toHaveBeenCalledWith(22, expect.objectContaining({ title: 'GPU 经营指标' })))
  })

  it('详情可收藏，列表可切换收藏筛选', async () => {
    const onFavoriteFilterChange = vi.fn()
    const onToggleFavorite = vi.fn().mockResolvedValue(true)
    renderDataTab({
      favoriteFilter: 'all',
      onFavoriteFilterChange,
      onToggleFavorite,
    })

    fireEvent.change(screen.getByRole('combobox', { name: '收藏状态' }), {
      target: { value: 'favorite' },
    })
    expect(onFavoriteFilterChange).toHaveBeenCalledWith('favorite')
    fireEvent.click(screen.getByRole('button', { name: '查看数据：GPU 利用率对比' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'GPU 利用率对比' })).getByRole('button', { name: '收藏' }))
    await waitFor(() => expect(onToggleFavorite).toHaveBeenCalledWith(gpuSource, true))
  })
})
