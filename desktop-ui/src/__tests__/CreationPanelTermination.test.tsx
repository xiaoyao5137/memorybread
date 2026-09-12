import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore, type CreationBrainstormState, type CreationChatMessage } from '../store/useAppStore'

const ROOT_REQUEST = '设计面向商家的短视频创作方案'
const SESSION_ID = 'session-termination-regression'
const PREPARATION_LABEL = '正在准备第一条脑暴问题'
const originalMessage: CreationChatMessage = {
  id: 'original-request', role: 'user', content: ROOT_REQUEST, createdAt: 1,
}
const sessionEndMessage: CreationChatMessage = {
  id: 'session-end', role: 'user', kind: 'session_end', content: '终止了当前会话', createdAt: 2,
}
const installedSkill = {
  id: 67,
  client_skill_key: 'termination-regression',
  title: '创作方案测试技能',
  summary: '用于创作方案设计。',
  status: 'saved',
  installed: true,
  execution_steps: [],
}

function brainstormState(phase = 'exploring', prompt = '迟到的问题不应重新出现'): CreationBrainstormState {
  return {
    session_id: SESSION_ID,
    phase,
    revision: 0,
    current_question: phase === 'exploring' ? {
      id: 'goal', dimension: '创作目标', type: 'single_choice', prompt,
      why_now: '确认创作目标。', required: true, allow_custom: true,
      options: [{ id: 'recommended', label: '推动商家采用', description: '确认首要目标。', recommended: true }],
      answer_template: '',
    } : null,
    brief_markdown: '# 创作简报\n\n待确认创作目标。',
    answered_count: 0,
    depth: 0,
    can_continue_brainstorm: false,
    open_flags: [],
    readiness_reason: '',
    continuation_directions: [],
    invalidated_question_ids: [],
    history: [],
    decisions: [],
  }
}

function deferredResponse() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(finish => { resolve = finish })
  return { promise, resolve }
}

function historyRecord(brief: CreationBrainstormState | null, conversation: CreationChatMessage[]) {
  return {
    id: 41,
    prompt: ROOT_REQUEST,
    root_request: ROOT_REQUEST,
    session_id: SESSION_ID,
    generated_content: '',
    creation_mode: 'brainstorm',
    lifecycle_status: 'cancelled',
    creation_brief_json: brief,
    conversation_json: conversation,
    created_at: 1,
  }
}

function composer() {
  return screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
}

function expectPreservedRequest() {
  expect(useAppStore.getState().creationDraft.rootRequest).toBe(ROOT_REQUEST)
  expect(useAppStore.getState().creationDraft.conversation.filter(message => message.content === ROOT_REQUEST)).toHaveLength(1)
  expect(within(screen.getByRole('region', { name: '创作对话' })).getByText(ROOT_REQUEST)).toBeInTheDocument()
}

async function submitBrainstorm() {
  render(<CreationPanel />)
  // Wait for the installed skill fixture to load so the matching stage runs.
  fireEvent.change(composer(), { target: { value: '@创作方案' } })
  await screen.findByRole('option', { name: /创作方案测试技能/ })
  fireEvent.change(composer(), { target: { value: ROOT_REQUEST } })
  fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
}

describe('创作输入清空与终止会话恢复边界', () => {
  beforeEach(() => {
    window.localStorage.clear()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
    useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm' })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('初次提交立即清空输入，在技能匹配、历史保存和首题生成期间保留原始要求', async () => {
    const match = deferredResponse()
    const history = deferredResponse()
    const question = deferredResponse()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([installedSkill])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/skills/match') return match.promise
      if (path === '/api/creation/history/start') return history.promise
      if (path === '/api/creation/brainstorm/turn') return question.promise
      return new Response('{}', { status: 404 })
    }))

    await submitBrainstorm()
    expect(composer()).toHaveValue('')
    expectPreservedRequest()
    const status = screen.getByRole('status', { name: PREPARATION_LABEL })
    expect(status).toHaveTextContent('正在匹配创作技能')

    await act(async () => { match.resolve(Response.json({ skill_ids: [67], source: 'model' })) })
    expect(status).toHaveTextContent('正在保存创作会话')
    expect(composer()).toHaveValue('')
    expectPreservedRequest()

    await act(async () => { history.resolve(Response.json({ id: 41 })) })
    expect(status).toHaveTextContent('正在生成第一条脑暴问题')
    expect(composer()).toHaveValue('')
    expectPreservedRequest()

    await act(async () => { question.resolve(Response.json(brainstormState('exploring', '正常生成的第一条问题'))) })
    expect(await screen.findByText('正常生成的第一条问题')).toBeInTheDocument()
    expect(composer()).toHaveValue('')
    expectPreservedRequest()
  })

  it.each(['match', 'history', 'question'] as const)('在 %s 等待阶段终止即清空，忽略取消的迟到响应也不能重新运行', async (cancelStage) => {
    const pending = deferredResponse()
    const requests: string[] = []
    let signal: AbortSignal | null | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([installedSkill])
      if (path === '/api/creation/history') return init?.method === 'POST'
        ? Response.json({ id: 41 }) : Response.json({ items: [], total: 0 })
      if (path.endsWith('/progress')) return new Response(null, { status: 204 })
      const stage = path === '/api/creation/skills/match' ? 'match'
        : path === '/api/creation/history/start' ? 'history'
          : path === '/api/creation/brainstorm/turn' ? 'question' : null
      if (stage) {
        requests.push(stage)
        if (stage === cancelStage) { signal = init?.signal; return pending.promise }
        if (stage === 'match') return Response.json({ skill_ids: [67], source: 'model' })
        if (stage === 'history') return Response.json({ id: 41 })
      }
      return new Response('{}', { status: 404 })
    }))

    await submitBrainstorm()
    await waitFor(() => expect(requests).toContain(cancelStage))
    fireEvent.click(screen.getByRole('button', { name: '终止当前会话' }))
    expect(signal?.aborted).toBe(true)
    expect(composer()).toHaveValue('')
    expect(composer()).toBeDisabled()
    expect(screen.getByRole('button', { name: '添加' })).toBeDisabled()
    expectPreservedRequest()
    expect(screen.getByLabelText('会话终止消息')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    const requestCount = requests.length

    // The response intentionally ignores AbortSignal, as buffered completions can do.
    await act(async () => {
      pending.resolve(Response.json(cancelStage === 'match' ? { skill_ids: [67], source: 'model' }
        : cancelStage === 'history' ? { id: 999 } : brainstormState()))
    })
    expect(requests).toHaveLength(requestCount)
    expect(composer()).toHaveValue('')
    expect(composer()).toBeDisabled()
    expect(screen.getByRole('button', { name: '添加' })).toBeDisabled()
    expect(useAppStore.getState().creationDraft.brainstormState).toBeNull()
    expect(screen.queryByText('迟到的问题不应重新出现')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '正在梳理' })).not.toBeInTheDocument()
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '终止当前会话' })).toHaveTextContent('已终止')
    expectPreservedRequest()
  })

  it.each(['null', 'exploring', 'abandoned'] as const)('加载包含 session_end 且简报为 %s 的历史不再恢复脑暴', async (phase) => {
    const actions: string[] = []
    const brief = phase === 'null' ? null : brainstormState(phase)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/41') return Response.json(historyRecord(brief, [originalMessage, sessionEndMessage]))
      if (path === '/api/creation/brainstorm/turn') {
        actions.push(JSON.parse(String(init?.body)).action)
        return new Promise<Response>(() => undefined)
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({ prompt: '上次未清除的输入' })
    useAppStore.getState().setCreationHistoryOpenTarget(41)
    render(<CreationPanel />)

    await screen.findByLabelText('会话终止消息')
    expect(actions).toEqual([])
    expect(composer()).toHaveValue('')
    expect(composer()).toBeDisabled()
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expect(screen.queryByText('正在恢复脑暴进度')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '正在梳理' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '终止当前会话' })).toHaveTextContent('已终止')
    expectPreservedRequest()
  })

  it.each(['null', 'exploring'] as const)('仅本轮 cancelled、简报 %s 且无 session_end 的历史仍能恢复脑暴', async (phase) => {
    const pending = deferredResponse()
    const actions: string[] = []
    const userAbort: CreationChatMessage = {
      id: 'user-abort', role: 'user', kind: 'user_abort', content: '中止了本次创作', createdAt: 2,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/41') return Response.json(historyRecord(
        phase === 'null' ? null : brainstormState(phase), [originalMessage, userAbort],
      ))
      if (path === '/api/creation/brainstorm/turn') {
        actions.push(JSON.parse(String(init?.body)).action)
        return pending.promise
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationHistoryOpenTarget(41)
    render(<CreationPanel />)

    await waitFor(() => expect(actions).toEqual(['start']))
    expect(screen.getByRole('button', { name: '终止当前会话' })).toBeEnabled()
    expect(screen.queryByLabelText('会话终止消息')).not.toBeInTheDocument()
    await act(async () => { pending.resolve(Response.json(brainstormState('exploring', '允许继续恢复的问题'))) })
    expect(await screen.findByText('允许继续恢复的问题')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expectPreservedRequest()
  })

  it('恢复请求等待期间收到外部终态时取消请求、清空残留输入并收口旧问题，迟到响应不能覆盖终态', async () => {
    const pending = deferredResponse()
    const actions: string[] = []
    let restoreSignal: AbortSignal | null | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/41') return Response.json(historyRecord(null, [originalMessage]))
      if (path === '/api/creation/brainstorm/turn') {
        actions.push(JSON.parse(String(init?.body)).action)
        restoreSignal = init?.signal
        return pending.promise
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationHistoryOpenTarget(41)
    render(<CreationPanel />)

    expect(await screen.findByRole('status', { name: PREPARATION_LABEL })).toHaveTextContent('正在恢复脑暴进度')
    expect(restoreSignal?.aborted).toBe(false)
    expect(screen.getByRole('button', { name: '正在梳理' })).toBeInTheDocument()
    await act(async () => {
      // Simulate an old persisted draft or external update arriving during recovery.
      useAppStore.getState().setCreationDraft({
        prompt: ROOT_REQUEST,
        conversation: [originalMessage, sessionEndMessage],
        brainstormState: brainstormState('exploring', '旧草稿中的未完成问题'),
      })
    })

    expect(restoreSignal?.aborted).toBe(true)
    expect(composer()).toHaveValue('')
    expect(composer()).toBeDisabled()
    expect(useAppStore.getState().creationDraft.brainstormState).toMatchObject({
      phase: 'abandoned', current_question: null, can_continue_brainstorm: false,
    })
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '正在梳理' })).not.toBeInTheDocument()
    expect(screen.queryByText('旧草稿中的未完成问题')).not.toBeInTheDocument()
    expectPreservedRequest()
    const terminalDraft = useAppStore.getState().creationDraft

    await act(async () => { pending.resolve(Response.json(brainstormState())) })
    expect(actions).toEqual(['start'])
    expect(useAppStore.getState().creationDraft).toEqual(terminalDraft)
    expect(screen.queryByText('迟到的问题不应重新出现')).not.toBeInTheDocument()
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '正在梳理' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '终止当前会话' })).toHaveTextContent('已终止')
  })

  it.each(['null', 'exploring', 'abandoned'] as const)('挂载已终止草稿（旧简报 %s）时清除残留输入并保留原始消息', async (phase) => {
    const actions: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/brainstorm/turn') actions.push(JSON.parse(String(init?.body)).action)
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({
      sessionId: SESSION_ID, rootRequest: ROOT_REQUEST, prompt: ROOT_REQUEST,
      brainstormState: phase === 'null' ? null : brainstormState(phase),
      conversation: [originalMessage, sessionEndMessage],
    })
    render(<CreationPanel />)

    await waitFor(() => expect(useAppStore.getState().creationDraft.prompt).toBe(''))
    expect(composer()).toHaveValue('')
    expect(composer()).toBeDisabled()
    expect(screen.getByRole('button', { name: '添加' })).toBeDisabled()
    expect(actions).toEqual([])
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expect(screen.getByLabelText('会话终止消息')).toBeInTheDocument()
    expectPreservedRequest()
  })

  it.each(['history', 'draft'] as const)('%s 仅存 abandoned 简报而缺少 session_end 时仍保持会话终止', async (source) => {
    const actions: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/41') return Response.json(historyRecord(brainstormState('abandoned'), [originalMessage]))
      if (path === '/api/creation/brainstorm/turn') {
        actions.push(JSON.parse(String(init?.body)).action)
        return new Promise<Response>(() => undefined)
      }
      return new Response('{}', { status: 404 })
    }))
    if (source === 'history') {
      useAppStore.getState().setCreationHistoryOpenTarget(41)
    } else {
      useAppStore.getState().setCreationDraft({
        sessionId: SESSION_ID, rootRequest: ROOT_REQUEST, prompt: ROOT_REQUEST,
        brainstormState: brainstormState('abandoned'), conversation: [originalMessage],
      })
    }
    render(<CreationPanel />)

    await screen.findByText('会话已终止，已有内容仍可查看')
    expect(actions).toEqual([])
    expect(composer()).toHaveValue('')
    expect(composer()).toBeDisabled()
    expect(screen.getByRole('button', { name: '添加' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '终止当前会话' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '开启新会话' })).toBeEnabled()
    expect(screen.queryByRole('status', { name: PREPARATION_LABEL })).not.toBeInTheDocument()
    expectPreservedRequest()
  })
})
