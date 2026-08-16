import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import ScheduledTasksPanel from '../components/ScheduledTasksPanel'
import { useAppStore } from '../store/useAppStore'

const taskFixture = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  name: '行业周报',
  user_instruction: '生成行业周报',
  cron_expression: '0 9 * * 1',
  enabled: true,
  is_builtin: false,
  can_delete: true,
  template_id: null,
  notification_channel_ids: [],
  executor_kind: 'consult',
  run_count: 0,
  last_run_at: null,
  last_run_status: null,
  next_run_at: null,
  created_at: 1,
  updated_at: 1,
  ...overrides,
})

const stubFetch = (tasks: unknown[], extra?: (url: URL) => Response | null) => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    const custom = extra?.(url)
    if (custom) return custom
    if (url.pathname === '/api/tasks' && !init?.method) {
      return Response.json({ tasks })
    }
    if (url.pathname === '/api/notification-channels') {
      return Response.json({ channels: [] })
    }
    if (url.pathname === '/api/creation/skills') {
      return Response.json([{
        id: 11,
        client_skill_key: 'skill-key-1',
        title: '行业白皮书',
        summary: '生成行业白皮书',
        skill_description: '',
        execution_steps: [],
        package_files: [],
        source_kind: 'manual',
        installed: true,
        created_at: 1,
        updated_at: 1,
      }])
    }
    return new Response('{}', { status: 404 })
  }))
}

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('任务卡片与执行智能体选择', () => {
  it('任务卡片按 executor_kind 展示智能体标签', async () => {
    stubFetch([
      taskFixture({ id: 1, name: '创作任务', executor_kind: 'creation' }),
      taskFixture({ id: 2, name: '咨询任务', executor_kind: 'consult' }),
    ])

    render(<ScheduledTasksPanel />)

    expect(await screen.findByText('创作任务')).toBeInTheDocument()
    expect(screen.getByText('创作智能体')).toBeInTheDocument()
    expect(screen.getByText('咨询智能体')).toBeInTheDocument()
  })

  it('创建表单默认咨询智能体，切换后随创建请求提交', async () => {
    const postBodies: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (init?.method === 'POST' && url.pathname === '/api/tasks') {
        postBodies.push(JSON.parse(String(init.body)))
        return Response.json({ task: taskFixture() })
      }
      if (url.pathname === '/api/tasks') return Response.json({ tasks: [] })
      if (url.pathname === '/api/notification-channels') return Response.json({ channels: [] })
      if (url.pathname === '/api/creation/skills') return Response.json([])
      return new Response('{}', { status: 404 })
    }))

    render(<ScheduledTasksPanel />)
    fireEvent.click(await screen.findByText('+ 新建'))

    // 表单内出现执行智能体分段选择，默认选中咨询智能体。
    const agentButtons = await screen.findAllByRole('button', { name: /智能体/ })
    expect(agentButtons.map(button => button.textContent)).toEqual(
      expect.arrayContaining(['咨询智能体', '创作智能体']),
    )

    fireEvent.change(screen.getByPlaceholderText('例：每日工作日记'), {
      target: { value: '行业周报' },
    })
    const textarea = screen.getByPlaceholderText(/描述你希望 AI 做什么/)
    fireEvent.change(textarea, { target: { value: '生成行业周报', selectionStart: 6 } })

    // 默认不切换直接提交，请求体携带 consult。
    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))
    await waitFor(() => {
      expect(postBodies).toHaveLength(1)
    })
    expect(postBodies[0].executor_kind).toBe('consult')

    // 再次创建并切换到创作智能体后提交。
    fireEvent.click(screen.getByText('+ 新建'))
    fireEvent.change(await screen.findByPlaceholderText('例：每日工作日记'), {
      target: { value: '行业周报' },
    })
    fireEvent.change(screen.getByPlaceholderText(/描述你希望 AI 做什么/), {
      target: { value: '生成行业周报', selectionStart: 6 },
    })
    fireEvent.click(screen.getByRole('button', { name: '创作智能体' }))
    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))

    await waitFor(() => {
      expect(postBodies).toHaveLength(2)
    })
    expect(postBodies[1].executor_kind).toBe('creation')
    expect(postBodies[1].name).toBe('行业周报')
  })
})

describe('执行指令 @ 提及选择器', () => {
  it('输入 @ 弹出候选，回车插入完整提及并保持高亮', async () => {
    stubFetch([])

    const { container } = render(<ScheduledTasksPanel />)
    fireEvent.click(await screen.findByText('+ 新建'))

    const textarea = await screen.findByPlaceholderText(/描述你希望 AI 做什么/)
    fireEvent.change(textarea, { target: { value: '帮我用 @互', selectionStart: 6 } })

    // 候选包含内置工具（按输入前缀过滤）。
    expect(await screen.findByText('@互联网检索')).toBeInTheDocument()
    expect(screen.queryByText('@行业白皮书')).not.toBeInTheDocument()

    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => {
      expect((textarea as HTMLTextAreaElement).value).toBe('帮我用 @互联网检索 ')
    })
    // 提及在镜像层以 mark 高亮渲染。
    await waitFor(() => {
      const marks = container.querySelectorAll('mark.mention-highlight-field__mention')
      expect(marks.length).toBeGreaterThan(0)
      expect(marks[0].textContent).toContain('@互联网检索')
    })
  })

  it('Escape 关闭选择器，已安装技能标题也进入候选', async () => {
    stubFetch([])

    render(<ScheduledTasksPanel />)
    fireEvent.click(await screen.findByText('+ 新建'))

    const textarea = await screen.findByPlaceholderText(/描述你希望 AI 做什么/)
    fireEvent.change(textarea, { target: { value: '@行业白', selectionStart: 4 } })

    expect(await screen.findByText('@行业白皮书')).toBeInTheDocument()
    fireEvent.keyDown(textarea, { key: 'Escape' })

    await waitFor(() => {
      expect(screen.queryByText('@行业白皮书')).not.toBeInTheDocument()
    })
  })
})

describe('执行历史跳转创作页', () => {
  it('携带 creation_history_id 的执行记录显示跳转按钮并写入跳转目标', async () => {
    stubFetch([taskFixture({ id: 5, executor_kind: 'creation' })], (url) => {
      if (url.pathname === '/api/tasks/5/executions') {
        return Response.json({
          executions: [
            {
              id: 21, task_id: 5, started_at: 1_720_000_000_000, completed_at: 1_720_000_100_000,
              status: 'success', knowledge_count: 0, token_used: 0,
              result_text: '周报正文', error_message: null, latency_ms: 1200,
              creation_history_id: 42, notification_deliveries: [],
            },
            {
              id: 20, task_id: 5, started_at: 1_719_000_000_000, completed_at: 1_719_000_100_000,
              status: 'success', knowledge_count: 3, token_used: 0,
              result_text: '旧结果', error_message: null, latency_ms: 900,
              creation_history_id: null, notification_deliveries: [],
            },
          ],
        })
      }
      return null
    })

    render(<ScheduledTasksPanel />)
    await screen.findByText('行业周报')

    // 打开执行历史（卡片上的眼睛按钮）。
    fireEvent.click(screen.getByTitle('查看历史'))
    expect(await screen.findByText('查看执行过程')).toBeInTheDocument()

    fireEvent.click(screen.getByText('查看执行过程'))

    await waitFor(() => {
      expect(useAppStore.getState().creationHistoryOpenTarget).toBe(42)
      expect(useAppStore.getState().windowMode).toBe('creation')
    })
  })
})
