import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import BakeOverviewTab from '../components/bake/BakeOverviewTab'
import type { BakeInventoryTrendBucket, BakeOverview } from '../types'

const DAY_MS = 86_400_000

const createBucket = (day: number, memoryCount: number): BakeInventoryTrendBucket => {
  const startTs = new Date(2026, 5, day).getTime()
  return {
    label: `2026-06-${String(day).padStart(2, '0')}`,
    startTs,
    endTs: startTs + DAY_MS - 1,
    memoryCount,
    dataCount: day % 5,
    knowledgeCount: day % 3,
    templateCount: day % 2,
    sopCount: day % 4,
  }
}

const overview: BakeOverview = {
  captureCount: 0,
  memoryCount: 12,
  dataCount: 2,
  knowledgeCount: 4,
  templateCount: 3,
  sopCount: 2,
  pendingCandidates: 0,
  recentActivities: [],
  inventoryTrend: Array.from({ length: 12 }, (_, index) => createBucket(index + 1, index + 1)),
}

describe('BakeOverviewTab 趋势图', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('提供时间范围控件并使用紧凑横轴日期', () => {
    render(<BakeOverviewTab overview={overview} />)

    expect(screen.getByRole('button', { name: '全部' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '7天' })).toBeInTheDocument()
    expect(screen.getByText('06/01')).toBeInTheDocument()
    expect(screen.queryByText('2026-06-01')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '7天' }))

    expect(screen.getByRole('button', { name: '7天' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('7天范围按今天往前的本地自然日显示，hover 使用当前日桶日期', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 6, 20, 10, 0, 0))
    const coarseOverview: BakeOverview = {
      ...overview,
      inventoryTrend: [{
        label: '2026-06-01-2026-06-12',
        startTs: new Date(2026, 5, 1).getTime(),
        endTs: new Date(2026, 5, 13).getTime() - 1,
        memoryCount: 12,
        dataCount: 2,
        knowledgeCount: 4,
        templateCount: 3,
        sopCount: 2,
      }],
    }

    const { container } = render(<BakeOverviewTab overview={coarseOverview} />)

    fireEvent.click(screen.getByRole('button', { name: '7天' }))

    expect(screen.getByText('07/14')).toBeInTheDocument()
    expect(screen.getByText('07/20')).toBeInTheDocument()
    expect(screen.queryByText('06/12')).not.toBeInTheDocument()

    const chart = container.querySelector('.bake-trend-chart')
    expect(chart).not.toBeNull()
    vi.spyOn(chart as HTMLDivElement, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      width: 720,
      height: 248,
      right: 720,
      bottom: 248,
      toJSON: () => ({}),
    })

    fireEvent.mouseMove(chart as HTMLDivElement, { clientX: 720 })

    expect(screen.getByText('2026-07-20')).toBeInTheDocument()

    fireEvent.mouseMove(chart as HTMLDivElement, { clientX: 0 })

    expect(screen.getByText('2026-07-14')).toBeInTheDocument()
  })

  it('鼠标悬浮趋势图时显示当前数据桶详情', () => {
    const { container } = render(<BakeOverviewTab overview={overview} />)
    const chart = container.querySelector('.bake-trend-chart')
    expect(chart).not.toBeNull()
    vi.spyOn(chart as HTMLDivElement, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      width: 720,
      height: 248,
      right: 720,
      bottom: 248,
      toJSON: () => ({}),
    })

    fireEvent.mouseMove(chart as HTMLDivElement, { clientX: 720 })

    expect(screen.getByText('2026-06-12')).toBeInTheDocument()
    expect(screen.getByText('合计 14')).toBeInTheDocument()
    expect(container.querySelector('.bake-trend-tooltip')).toHaveTextContent('数据2')

    fireEvent.mouseMove(chart as HTMLDivElement, { clientX: 0 })

    expect(screen.getByText('2026-06-01')).toBeInTheDocument()
    expect(screen.getByText('合计 5')).toBeInTheDocument()
  })

  it('记忆总览不再承载备份与恢复入口', () => {
    render(<BakeOverviewTab overview={overview} />)

    expect(screen.queryByLabelText('记忆备份')).not.toBeInTheDocument()
  })

  it('不再展示趋势副标题与底部快捷操作/仓库概览/最近处理流水', () => {
    render(<BakeOverviewTab overview={overview} />)

    expect(screen.queryByText('按年月日观察时间线、知识、文档、操作和数据的生产分布')).not.toBeInTheDocument()
    expect(screen.queryByText('快捷操作')).not.toBeInTheDocument()
    expect(screen.queryByText('仓库概览')).not.toBeInTheDocument()
    expect(screen.queryByText('最近处理流水')).not.toBeInTheDocument()
  })
})
