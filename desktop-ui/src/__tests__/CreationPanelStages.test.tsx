import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'
import type { CreationBrainstormState } from '../store/useAppStore'
import { stageFixture, stageQuestion } from '../../test/brainstorm-stage-fixture'

function setup(initial: CreationBrainstormState, handler?: (payload: Record<string, any>) => Response | Promise<Response>, agent?: (payload: Record<string, any>) => Response | Promise<Response>) {
  let state = structuredClone(initial)
  const requests: Record<string, any>[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname
    const payload = JSON.parse(String(init?.body || '{}'))
    if (path === '/api/creation/brainstorm/turn') {
      requests.push(payload)
      expect(payload.root_request).toBe(initial.root_request)
      return handler ? handler(payload) : Response.json(state)
    }
    if (path === '/api/creation/agent/run') {
      requests.push({ ...payload, agent: true })
      return agent ? agent(payload) : Response.json({ message: '测试未配置此执行' }, { status: 500 })
    }
    if (path === '/api/creation/skills') return Response.json([])
    if (path === '/api/creation/skills/match') return Response.json({ matches: [] })
    if (path === '/api/creation/history/start') return Response.json({ id: 901, progress_epoch: 1 })
    if (path.endsWith('/progress')) return new Response(null, { status: 204 })
    if (path === '/api/creation/history') return init?.method === 'POST' ? Response.json({ id: 901 }) : Response.json({ items: [], total: 0 })
    return Response.json({ connected: false })
  }))
  useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', sessionId: initial.session_id,
    rootRequest: initial.root_request, brainstormState: initial, brainstormPaused: false,
    conversation: [{ id: 'root', role: 'user', content: initial.root_request!, createdAt: 0 }] })
  return { requests, setState: (next: CreationBrainstormState) => { state = next } }
}

beforeEach(() => { localStorage.clear(); useAppStore.getState().reset(); useAppStore.getState().setApiBaseUrl('http://localhost:7070') })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('三阶段脑暴页面流程', () => {
  it('renders a v17 content question and submits only the selected answer', async () => {
    const state = stageFixture('answering', 'direction')
    const question = {
      ...stageQuestion('gpu-cost-focus', 'GPU成本变化'),
      prompt: '本期 GPU 周报优先核查哪类成本变化？',
      why_now: '明确本期数据核查对象。',
      options: [
        { id: 'project-cost', label: '项目年化成本', description: '核查项目清单中的成本变化。' },
        { id: 'resource-usage', label: '资源用量', description: '核查实际卡数与配额变化。' },
      ],
    }
    state.current_question = question
    state.stage_workflow!.stages[0].preparation_contract_version = 17
    state.stage_workflow!.stages[0].questions = [question]
    const backend = setup(state)
    render(<CreationPanel />)
    expect(screen.getByText('本期 GPU 周报优先核查哪类成本变化？')).toBeInTheDocument()
    expect(screen.getByText('本阶段已准备 1 题，请逐题确认。可在创作简报中查看全部候选问题与选项。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /项目年化成本/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))
    await waitFor(() => expect(backend.requests.some(item => item.action === 'answer')).toBe(true))
    expect(backend.requests.find(item => item.action === 'answer')?.answer.selected_option_ids).toEqual(['project-cost'])
  })

  it('lets an existing writing session return to input questions without losing its answers or draft', async () => {
    const state = stageFixture('ready', 'writing')
    state.phase = 'chapter_writing'
    delete state.stage_workflow!.stages[1].input_preflight
    const answered = stageQuestion('confirmed-style', '已确认表达')
    state.history = [{ question: answered, answer: {
      selected_option_ids: [answered.options[0].id], custom_text: '', source: 'user',
    } }]
    state.decisions = [{ question_id: answered.id, dimension: answered.dimension,
      summary: answered.options[0].label, source: 'user' }]
    let authoritative = structuredClone(state)
    const backend = setup(state, payload => {
      if (payload.action === 'review_inputs') {
        authoritative = structuredClone(authoritative)
        authoritative.revision++
        authoritative.stage_workflow!.active_stage = 'detail'
        authoritative.stage_workflow!.stages[1].status = 'thinking'
      }
      return Response.json(authoritative)
    })
    useAppStore.getState().setCreationDraft({ generatedContent: '# 已有周报草稿\n\n此前已保存的内容' })
    render(<CreationPanel />)
    const action = screen.getByRole('button', { name: '补齐资料选题' })
    expect(action).toBeEnabled()
    expect(screen.queryByRole('button', { name: /继续本章创作|确认本章设计并创作/ })).not.toBeInTheDocument()
    fireEvent.click(action)
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.active_stage).toBe('detail'))
    expect(backend.requests.find(item => item.action === 'review_inputs')).toMatchObject({
      session_id: state.session_id, revision: state.revision,
    })
    expect(useAppStore.getState().creationDraft.brainstormState?.history).toEqual(state.history)
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual(state.decisions)
    expect(useAppStore.getState().creationDraft.generatedContent).toBe('# 已有周报草稿\n\n此前已保存的内容')
  })

  it('asks for a report period in the detail card without auto-answering or offering skip', async () => {
    const state = stageFixture('answering', 'detail')
    const question = {
      ...stageQuestion('period', '周报周期'), type: 'single_choice' as const,
      prompt: '这份周报覆盖哪一周？',
      input_clarification: { kind: 'report_period', need_ids: ['meeting'] },
      options: [
        { id: 'complete-week', label: '9 月 21 日至 27 日', description: '最近完整周',
          input_value: { start: '2026-09-21', end: '2026-09-27', timezone: 'Asia/Shanghai' } },
        { id: 'current-week', label: '9 月 28 日至 10 月 4 日', description: '本周',
          input_value: { start: '2026-09-28', end: '2026-10-04', timezone: 'Asia/Shanghai' } },
      ],
    }
    state.current_question = question
    state.stage_workflow!.stages[1].questions[0] = question
    state.stage_workflow!.stages[1].input_preflight = {
      schema_version: 1, status: 'partial', period: { status: 'unconfirmed' },
      needs: [{ id: 'meeting', label: '周会纪要', source_type: 'memory', status: 'candidate', receipts: [{
        source_type: 'document', source_id: 783, title: 'GPU 周会纪要', observed_at: '2026-09-26',
        source_period: null,
        period_status: 'unknown', completeness: 'partial',
      }] }],
    }
    const backend = setup(state)
    render(<CreationPanel />)
    expect(screen.getByRole('region', { name: '本地资料核查' })).toHaveTextContent('统计期未标明 · 采集或更新 2026-09-26')
    expect(screen.getByRole('region', { name: '本地资料核查' })).toHaveTextContent('报告周期待你确认')
    expect(screen.queryByRole('button', { name: '跳过此题' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '该方向不重要' })).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /9 月 21 日至 27 日/ })).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('radio', { name: /9 月 28 日至 10 月 4 日/ })).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('button', { name: /确认并继续/ })).toBeDisabled()
    expect(backend.requests.every(item => item.action === 'status')).toBe(true)
    fireEvent.click(screen.getByRole('radio', { name: /9 月 21 日至 27 日/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))
    await waitFor(() => expect(backend.requests.some(item => item.action === 'answer')).toBe(true))
    expect(backend.requests.find(item => item.action === 'answer')?.answer.selected_option_ids).toEqual(['complete-week'])
  })

  it('synchronizes a cached failed stage before displaying its state, then follows automatic Core recovery without asking for retry', async () => {
    const cached = stageFixture('failed', 'writing')
    cached.stage_workflow!.stages[2].error = { code: 'BRAINSTORM_MODEL_OUTPUT_INVALID', message: '旧快照生成失败', retryable: true }
    const authoritative = stageFixture('thinking', 'writing')
    authoritative.revision = cached.revision + 1
    authoritative.stage_workflow!.stages[2].generation_id = 'restored-current-generation'
    let finishFirstStatus!: () => void
    const pendingStatus = new Promise<void>(resolve => { finishFirstStatus = resolve })
    const backend = setup(cached, () => pendingStatus.then(() => Response.json(authoritative)))
    render(<CreationPanel />)
    expect(screen.getByText('正在同步创作行文进度')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续本阶段思考' })).not.toBeInTheDocument()
    expect(screen.queryByText(/旧快照生成失败|思考暂停|思考进度已保存，请继续/)).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.stages[2].status).toBe('failed')
    await act(async () => { finishFirstStatus() })
    expect(await screen.findByText('创作行文阶段模型思考中')).toBeInTheDocument()
    expect(screen.queryByText('正在同步创作行文进度')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(backend.requests.every(item => item.action === 'status')).toBe(true)
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual([])
  })

  it('reveals a real failed stage immediately after the current API confirms it', async () => {
    const failed = stageFixture('failed', 'writing')
    failed.stage_workflow!.stages[2].error = { code: 'BRAINSTORM_PREPARATION_EXHAUSTED', message: '当前阶段暂时无法完成，已保存进度', retryable: true }
    let finishFirstStatus!: () => void
    const pendingStatus = new Promise<void>(resolve => { finishFirstStatus = resolve })
    setup(failed, () => pendingStatus.then(() => Response.json(failed)))
    render(<CreationPanel />)
    expect(screen.getByText('正在同步创作行文进度')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await act(async () => { finishFirstStatus() })
    expect(screen.getByRole('alert')).toHaveTextContent('当前阶段暂时无法完成，已保存进度')
    expect(screen.getByRole('button', { name: '继续本阶段思考' })).toBeEnabled()
    expect(screen.queryByText('正在同步创作行文进度')).not.toBeInTheDocument()
  })

  it('restores stage progress on returning and does not offer premature document generation', async () => {
    const state = stageFixture()
    const backend = setup(state)
    const view = render(<CreationPanel />)
    await waitFor(() => expect(backend.requests.filter(item => item.action === 'status')).toHaveLength(1))
    expect(screen.getByText('创作方向阶段模型思考中')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /先生成一版|开始创作/ })).not.toBeInTheDocument()
    view.rerender(<CreationPanel active={false} />)
    backend.setState(stageFixture('answering'))
    view.rerender(<CreationPanel active />)
    await waitFor(() => expect(screen.getByRole('group', { name: '答案选项' })).toBeInTheDocument())
    expect(screen.queryByText('创作方向阶段模型思考中')).not.toBeInTheDocument()
    expect(screen.getByText('当前第 1 题 · 已回答 0 题')).toBeInTheDocument()
    expect(screen.getByText(/本阶段已准备 6 题，按选择展开/)).toBeInTheDocument()
    expect(screen.queryByText('第 1 / 1 题')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /先生成一版/ })).not.toBeInTheDocument()
    expect(backend.requests.every(item => item.action === 'status')).toBe(true)
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual([])
  })

  it('keeps automatic preparation recovery visible as thinking and polls through to ready questions without a retry request', async () => {
    const state = stageFixture('thinking', 'writing')
    state.stage_workflow!.stages[2].recovery_progress = { attempt: 1, max_attempts: 2, retry_at_ms: Date.now() + 1000 }
    state.stage_workflow!.stages[2].preparation_progress = { topic_title: '目标与评估范围', saved_nodes: 1, pending_nodes: 5 }
    const backend = setup(state)
    render(<CreationPanel />)
    await waitFor(() => expect(backend.requests.filter(item => item.action === 'status')).toHaveLength(1))
    expect(screen.getByText('创作行文阶段模型思考中')).toBeInTheDocument()
    expect(screen.getByText('正在结合已确认内容完善本阶段思路。')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续本阶段思考' })).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState?.revision).toBe(state.revision)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')

    const prepared = stageFixture('answering', 'writing')
    prepared.revision = state.revision + 1
    backend.setState(prepared)
    await waitFor(() => expect(screen.getByRole('group', { name: '答案选项' })).toBeInTheDocument(), { timeout: 3000 })
    expect(backend.requests.length).toBeGreaterThanOrEqual(2)
    expect(backend.requests.every(item => item.action === 'status')).toBe(true)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('正在结合已确认内容完善本阶段思路。')).not.toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual([])
  })

  it('anchors a waiting stage at its heading on open and return instead of scrolling to the chat bottom', async () => {
    const original = HTMLElement.prototype.getBoundingClientRect
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const timeline = document.querySelector('.creation-chat-timeline') as HTMLElement | null
      const top = this.classList.contains('creation-chat-timeline') ? 100 : this.classList.contains('creation-brainstorm-stage-anchor') ? 200 - (timeline?.scrollTop || 0) : 0
      return { ...original.call(this), top } as DOMRect
    })
    const state = stageFixture()
    const backend = setup(state)
    const view = render(<CreationPanel />)
    await waitFor(() => expect(backend.requests.length).toBe(1))
    const timeline = document.querySelector('.creation-chat-timeline') as HTMLElement
    expect(timeline.scrollTop).toBe(100)
    timeline.scrollTop = 40
    await act(async () => { useAppStore.getState().setCreationDraft({ brainstormState: { ...state, revision: 6 } }) })
    expect(timeline.scrollTop).toBe(40)
    view.rerender(<CreationPanel active={false} />)
    view.rerender(<CreationPanel active />)
    expect(timeline.scrollTop).toBe(100)
    vi.restoreAllMocks()
  })

  it('shows a stage transition error even when no question is active', async () => {
    const state = stageFixture('ready')
    setup(state, payload => payload.action === 'confirm_stage'
      ? Response.json({ message: '阶段确认未保存，请重试', code: 'FAILED' }, { status: 500 })
      : Response.json(state))
    const view = render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '确认创作方向，进入创作细节' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('阶段确认未保存，请重试'))
    expect(screen.getByRole('button', { name: '确认创作方向，进入创作细节' })).toBeEnabled()
    expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.active_stage).toBe('direction')
    view.rerender(<CreationPanel active={false} />)
    view.rerender(<CreationPanel active />)
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent('阶段确认未保存，请重试')
  })

  it('surfaces permanent status synchronization failures while preserving completed batches and stops retrying', async () => {
    vi.useFakeTimers()
    const state = stageFixture()
    const backend = setup(state, () => Response.json({ message: '请重新登录以同步进度' }, { status: 401 }))
    render(<CreationPanel />)
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent('请重新登录以同步进度')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
    await act(async () => { vi.advanceTimersByTime(30000) })
    expect(backend.requests.filter(item => item.action === 'status')).toHaveLength(1)
  })

  it('recovers temporary status synchronization errors silently and clears only its own persistent-outage message after success', async () => {
    vi.useFakeTimers()
    const state = stageFixture()
    let statusReads = 0
    const backend = setup(state, () => ++statusReads <= 4
      ? Response.json({ message: '进度服务暂时不可用' }, { status: 503 })
      : Response.json(state))
    render(<CreationPanel />)
    await act(async () => {})
    expect(backend.requests).toHaveLength(1)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(1500) })
    expect(backend.requests).toHaveLength(2)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(3000) })
    expect(backend.requests).toHaveLength(3)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(6000) })
    expect(backend.requests).toHaveLength(4)
    expect(screen.getByRole('alert')).toHaveTextContent('进度暂时无法同步，后台思考仍会继续；请检查本地服务连接。')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
    expect(screen.queryByRole('button', { name: '继续本阶段思考' })).not.toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(12000) })
    expect(backend.requests).toHaveLength(5)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(backend.requests.every(item => item.action === 'status')).toBe(true)
    expect(useAppStore.getState().creationDraft.brainstormState?.revision).toBe(state.revision)
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual([])
  })

  it('lets the active chapter revise a saved design after an earlier chapter is completed, while preserving server-frozen history', async () => {
    const first = stageQuestion('first-design', '第一章设计')
    const second = stageQuestion('second-design', '第二章设计')
    const current = stageQuestion('second-next', '第二章表达方式')
    let state = stageFixture('answering', 'writing')
    state.stage_workflow!.stages[2].questions = [first, second, current]
    Object.assign(state.stage_workflow!.chapters[0], { status: 'completed', question_ids: [first.id] })
    Object.assign(state.stage_workflow!.chapters[1], { status: 'answering', question_ids: [second.id, current.id] })
    state.stage_workflow!.active_chapter_id = 'chapter-2'
    state.history = [first, second].map(question => ({ question, answer: { selected_option_ids: [question.options[0].id], custom_text: '', source: 'user' } }))
    state.decisions = [first, second].map(question => ({ question_id: question.id, dimension: question.dimension, summary: question.options[0].label, source: 'user' }))
    state.current_question = current
    state.answered_count = 2
    const backend = setup(state, payload => {
      if (payload.action === 'revise_answer') {
        if (payload.question_id === first.id) return Response.json({ code: 'BRAINSTORM_CHAPTER_REVIEW_REQUIRED', message: '已完成章节的设计不能直接修改' }, { status: 409 })
        state = structuredClone(state)
        state.revision++
        state.history![1].answer = { ...payload.answer, source: 'user' }
        state.decisions[1].summary = second.options[1].label
      }
      return Response.json(state)
    })
    useAppStore.getState().setCreationDraft({ generatedContent: '# 已完成第一章\n\n必须保留的正文' })
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    fireEvent.click(screen.getByRole('checkbox', { name: /第二章设计：先聚焦核心目标/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /第二章设计：覆盖完整场景/ }))
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.history?.[1].answer.selected_option_ids).toEqual([second.options[1].id]))
    expect(backend.requests.find(item => item.action === 'revise_answer')?.question_id).toBe(second.id)
    expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.chapters[0].status).toBe('completed')
    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    fireEvent.click(screen.getByRole('checkbox', { name: /第一章设计：先聚焦核心目标/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /第一章设计：覆盖完整场景/ }))
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('已完成章节的设计不能直接修改'))
    expect(useAppStore.getState().creationDraft.brainstormState?.history?.[0].answer.selected_option_ids).toEqual([first.options[0].id])
    expect(useAppStore.getState().creationDraft.generatedContent).toBe('# 已完成第一章\n\n必须保留的正文')
  })

  it('retries a failed chapter with the same instruction and preserves its existing body', async () => {
    let state = stageFixture('ready', 'writing')
    const backend = setup(state, payload => {
      if (payload.action === 'confirm_chapter') {
        state = structuredClone(state)
        state.revision++
        state.phase = 'chapter_writing'
        Object.assign(state.stage_workflow!.chapters[0], { status: 'writing', instruction_id: 'retry-stable-instruction', operation_id: 'derived-operation' })
      }
      return Response.json(state)
    }, () => Response.json({ message: '模型服务暂时不可用' }, { status: 503 }))
    useAppStore.getState().setCreationDraft({ generatedContent: '# 已有章节\n\n必须保留的正文' })
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '确认本章设计并创作' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '继续本章创作' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '继续本章创作' }))
    await waitFor(() => expect(backend.requests.filter(item => item.agent)).toHaveLength(2))
    await waitFor(() => expect(screen.getByRole('button', { name: '继续本章创作' })).toBeEnabled())
    expect(backend.requests.filter(item => item.agent).map(item => item.instruction_id)).toEqual(['retry-stable-instruction', 'retry-stable-instruction'])
    expect(backend.requests.filter(item => item.agent).every(item => item.resume_operation_id === undefined)).toBe(true)
    expect(backend.requests.some(item => item.action === 'chapter_completed')).toBe(false)
    expect(useAppStore.getState().creationDraft.conversation.filter(item => item.id === 'retry-stable-instruction')).toHaveLength(1)
    expect(useAppStore.getState().creationDraft.generatedContent).toBe('# 已有章节\n\n必须保留的正文')
    expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.active_chapter_id).toBe('chapter-1')
  })

  it.each([false, true])('unlocks the next chapter only after server acceptance (stop during acknowledgement: %s)', async (stopDuringAcknowledgement) => {
    let state = stageFixture('ready', 'writing')
    let resolveCompletion!: (response: Response) => void
    const pendingAcceptance = new Promise<Response>(resolve => { resolveCompletion = resolve })
    const body = '# 评估方案\n\n## 目标与评估范围\n先明确评估目标与场景。'
    const backend = setup(state, payload => {
      if (payload.action === 'confirm_chapter') {
        state = structuredClone(state)
        state.revision++
        state.phase = 'chapter_writing'
        Object.assign(state.stage_workflow!.chapters[0], { status: 'writing', instruction_id: 'stable-chapter-instruction', operation_id: 'derived-operation-not-yet-created' })
      }
      if (payload.action === 'chapter_completed') return pendingAcceptance
      return Response.json(state)
    }, payload => {
      expect(payload.brainstorm_chapter_id).toBe('chapter-1')
      expect(payload.instruction_id).toBe('stable-chapter-instruction')
      expect(payload.resume_operation_id).toBeUndefined()
      expect(payload.selected_skills).toEqual([])
      expect(payload.explicit_skill_ids).toEqual([])
      const base = { schema_version: 'creation.agent.v1', session_id: state.session_id, run_id: 'chapter-run', actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' }, timestamp: Date.now(), environment_patch: {} }
      const events = [
        { ...base, event_id: 'chapter-event-1', sequence: 1, type: 'document.replaced', status: 'running', summary: '本章内容已保存', data: { content: body } },
        { ...base, event_id: 'chapter-event-2', sequence: 2, type: 'run.completed', status: 'completed', summary: '本章创作完成', data: { document: body } },
      ]
      return new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } })
    })
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '确认本章设计并创作' }))
    await waitFor(() => expect(backend.requests.some(item => item.action === 'chapter_completed')).toBe(true))
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(body)
    expect(useAppStore.getState().creationDraft.brainstormState?.stage_workflow?.active_chapter_id).toBe('chapter-1')
    expect(screen.getByText('0 / 2 章已完成')).toBeInTheDocument()
    if (stopDuringAcknowledgement) fireEvent.click(screen.getByRole('button', { name: '中止' }))
    state = structuredClone(state)
    state.revision++
    state.phase = 'exploring'
    state.stage_workflow!.chapters[0].status = 'completed'
    state.stage_workflow!.chapters[1].status = 'answering'
    state.stage_workflow!.active_chapter_id = 'chapter-2'
    state.current_question = stageFixture('answering', 'writing').current_question
    await act(async () => { resolveCompletion(Response.json(state)); await Promise.resolve() })
    await waitFor(() => expect(screen.getByText('1 / 2 章已完成')).toBeInTheDocument())
    expect(useAppStore.getState().creationDraft.brainstormPaused).toBe(false)
    expect(screen.getByText('本章已通过交付检查。请继续确认下一章设计，已完成的正文会保留。')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('请在上方确认本章设计，再创作本章内容')).toBeDisabled()
    expect(screen.queryByText('继续对话优化当前文档')).not.toBeInTheDocument()
    expect(screen.getByRole('group', { name: '答案选项' })).toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(body)
    expect(backend.requests.filter(item => item.agent)).toHaveLength(1)
  })
})
