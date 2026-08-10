import { fireEvent, render, screen, within } from '@testing-library/react'
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
  it('以可理解的摘要和表格展示数据含义', () => {
    renderDataTab()

    expect(screen.getAllByText('GPU 利用率对比').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText(/日均 GPU 利用率：国内 42%，海外 47%/).length).toBeGreaterThanOrEqual(2)
    const table = screen.getByRole('table')
    expect(within(table).getByRole('columnheader', { name: '对象 / 范围' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: '指标' })).toBeInTheDocument()
    expect(within(table).getByText('国内')).toBeInTheDocument()
    expect(within(table).getByText('海外')).toBeInTheDocument()
    expect(within(table).getByText('42%')).toBeInTheDocument()
    expect(within(table).getByText('47%')).toBeInTheDocument()
    expect(within(table).getByText('GPUTL 可能掩盖实际低效')).toBeInTheDocument()
    expect(screen.getByText('该数据表共 2 行指标。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /GPU 利用率/ })).not.toHaveTextContent('行指标')
  })

  it('展示数据 ID，支持跳转来源时间线和删除', () => {
    const props = renderDataTab()

    expect(screen.getAllByText('数据 #22').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('快照 ID').parentElement).toHaveTextContent('#220')
    fireEvent.click(screen.getAllByRole('button', { name: '时间线 #71' })[0])
    expect(props.onViewTimeline).toHaveBeenCalledWith(71)
    fireEvent.click(screen.getByRole('button', { name: '删除数据' }))
    expect(props.onDelete).toHaveBeenCalledWith(22)
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

  it('网页来源的数据记录并展示来源网址', () => {
    renderDataTab({
      items: [reportSource],
      selectedId: 24,
    })

    // 列表项展示网址
    expect(screen.getByText('网址：https://bi.example.com/dashboard/gpu')).toBeInTheDocument()
    // 详情区像文档一样展示可点击的来源网址
    expect(screen.getByText('来源网址')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'https://bi.example.com/dashboard/gpu' })
    expect(link).toHaveAttribute('href', 'https://bi.example.com/dashboard/gpu')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('非网页来源的数据不展示来源网址', () => {
    renderDataTab()

    expect(screen.queryByText('来源网址')).not.toBeInTheDocument()
    expect(screen.queryByText(/^网址：/)).not.toBeInTheDocument()
  })
})
