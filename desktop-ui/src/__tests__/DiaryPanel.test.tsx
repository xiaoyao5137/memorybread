import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import DiaryPanel from '../components/DiaryPanel'
import { useAppStore } from '../store/useAppStore'

const diaryResponse = [
  {
    id: 1,
    period_type: 'daily',
    period_start: '2026-07-07',
    period_end: '2026-07-07',
    diary_date: '2026-07-07',
    content: {
      title: '2026-07-07 工作日记',
      work_outputs: ['完成了日记 API'],
      problems_solved: ['修复了忙时任务被吞的问题'],
      next_plan: ['验证周记汇总'],
      timeline: [],
      markdown: '## 今日产出\n- 完成了日记 API',
    },
    source_timeline_ids: [10],
    source_diary_ids: [],
    generation_status: 'ready',
    is_system_generated: true,
    created_at: '2026-07-08 09:00:00',
    updated_at: '2026-07-08 09:00:00',
  },
]

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-07-08T10:00:00'))
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: async () => diaryResponse,
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('DiaryPanel', () => {
  it('读取日记接口并展示工作产出', async () => {
    render(<DiaryPanel />)

    await waitFor(() => {
      expect(screen.getByText('完成了日记 API')).toBeInTheDocument()
    })

    expect(fetch).toHaveBeenCalledWith('http://localhost:7070/api/diaries?period_type=daily&limit=20')
    expect(screen.getByText('工作日记')).toBeInTheDocument()
    expect(screen.getByText('问题与解决')).toBeInTheDocument()
    expect(screen.queryByText('后续计划')).not.toBeInTheDocument()
    expect(screen.queryByText('验证周记汇总')).not.toBeInTheDocument()
  })

  it('后端尚未升级日记接口时显示空状态而不是错误', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: false,
        status: 404,
      } as Response)
      .mockResolvedValueOnce({
        ok: false,
        status: 500,
        text: async () => '{"error":"INTERNAL_ERROR","message":"SQLite 错误: no such table: user_profiles"}',
      } as Response)

    render(<DiaryPanel />)

    await waitFor(() => {
      expect(screen.getByText('暂无日记')).toBeInTheDocument()
    })
    expect(screen.queryByText('日记加载失败')).not.toBeInTheDocument()
  })

  it('选择日期后按具体日期查询日记', async () => {
    render(<DiaryPanel />)

    await waitFor(() => {
      expect(screen.getByText('完成了日记 API')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByLabelText('选择日记日期'), {
      target: { value: '2026-07-07' },
    })

    await waitFor(() => {
      expect(fetch).toHaveBeenLastCalledWith(
        'http://localhost:7070/api/diaries?period_type=daily&limit=1&diary_date=2026-07-07',
      )
    })
  })

  it('最近两个已完成日期缺日记时触发日记任务', async () => {
    vi.setSystemTime(new Date('2026-07-10T10:00:00'))
    vi.mocked(fetch).mockImplementation(async (url, init) => {
      if (url === 'http://localhost:7070/api/diaries?period_type=daily&limit=20') {
        return {
          ok: true,
          json: async () => diaryResponse,
        } as Response
      }
      if (url === 'http://localhost:7070/api/tasks') {
        return {
          ok: true,
          json: async () => ({
            tasks: [
              {
                id: 99,
                name: '生成昨日工作日记',
                template_id: 'daily_journal',
                enabled: true,
              },
            ],
          }),
        } as Response
      }
      if (url === 'http://localhost:7070/api/tasks/99/trigger' && init?.method === 'POST') {
        return { ok: false } as Response
      }
      return { ok: false, status: 404, text: async () => '' } as Response
    })

    render(<DiaryPanel />)

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        'http://localhost:7070/api/tasks/99/trigger',
        { method: 'POST' },
      )
    })
  })
})

it('refreshes the selected diary and shows the source coverage', async () => {
  render(<DiaryPanel />)
  await screen.findByText('完成了日记 API')
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    json: async () => [{
      ...diaryResponse[0],
      content: {
        ...diaryResponse[0].content,
        work_outputs: ['重新生成后的产出'],
        timeline: [{ timeline_id: 10, summary: '可核对的来源' }],
        source_count: 120,
        source_time_range: ['11-16 19:29', '11-16 23:48'],
      },
    }],
  } as Response)

  fireEvent.click(screen.getByRole('button', { name: '刷新' }))
  await screen.findByText('重新生成后的产出')
  expect(screen.queryByText('完成了日记 API')).not.toBeInTheDocument()
  expect(screen.getByText(/120 条时间线.*19:29.*23:48/)).toBeInTheDocument()
  const sourceDetails = screen.getByText('来源线索').closest('details')
  expect(sourceDetails).not.toHaveAttribute('open')
})
