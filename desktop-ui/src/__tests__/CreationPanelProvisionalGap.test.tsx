import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { sha256Hex } from '../components/creation-selection/creationInlineEdit'
import { useAppStore, type CreationBrainstormState } from '../store/useAppStore'
import { stageFixture } from '../../test/brainstorm-stage-fixture'

const rootRequest = 'GPU 成本优化周报'
const document = '# GPU 成本优化周报\n\n## 费用与用量\n\n本期账单与用量仍待核对。'
const guidance = '本期资料来源仍待核实。可手动编辑；如需 Agent 使用新增资料重写，请新建创作并重新确认周期/来源。'
const review = { status: 'pass', provisional: true, source_validation: 'pending', document_hash: 'a'.repeat(64) }

const completedState = (version = 17): CreationBrainstormState => {
  const state = stageFixture('completed', 'writing')
  state.phase = 'completed'
  state.root_request = rootRequest
  state.stage_workflow!.stages.forEach(stage => { stage.preparation_contract_version = version })
  state.stage_workflow!.chapters.forEach(chapter => { chapter.status = 'completed' })
  state.stage_workflow!.active_chapter_id = null
  return state
}

const event = (state: CreationBrainstormState, type: string, sequence: number, data: Record<string, unknown>) => ({
  schema_version: 'creation.agent.v1', event_id: `gap-${sequence}`, session_id: state.session_id,
  run_id: 'gap-run', sequence, timestamp: 1_720_000_000_000 + sequence,
  type, status: 'completed', actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
  summary: type, environment_patch: {}, data,
})

const reviewedEvents = (state: CreationBrainstormState, deliveryReview: Record<string, unknown> = review) => [
  event(state, 'delivery.checked', 1, { review: deliveryReview }),
  event(state, 'run.completed', 2, { delivery_review: deliveryReview }),
]

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
})
afterEach(() => vi.unstubAllGlobals())

describe('已完成的 v17 资料待核实草稿', () => {
  it('restores the delivery marker, keeps manual edits available, and blocks futile Agent follow-up after the edit', async () => {
    const state = completedState()
    // The document and trace are persisted before chapter_completed updates
    // the separate Brainstorm session; history.creation_brief is still writing.
    const savedBrief = structuredClone(state)
    savedBrief.phase = 'chapter_writing'
    savedBrief.stage_workflow!.active_chapter_id = 'chapter-2'
    savedBrief.stage_workflow!.chapters[1].status = 'writing'
    let savedDocument = document
    let revision = 1
    const history = () => ({
      id: 188, prompt: rootRequest, root_request: rootRequest, session_id: state.session_id,
      generated_content: savedDocument, revision_no: revision, lifecycle_status: 'completed',
      creation_mode: 'brainstorm', creation_brief_json: JSON.stringify(savedBrief),
      agent_trace_json: JSON.stringify([
        ...reviewedEvents(state),
        event(state, 'run.completed', 3, { document: savedDocument }), // later manual edit has no source review
      ]),
      conversation_json: '[]', references_json: '[]', evidence_json: '[]',
      created_at: 1_720_000_000_000, updated_at: 1_720_000_100_000,
    })
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      requests.push(`${init?.method || 'GET'} ${path}`)
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history' && !init?.method) return Response.json({ items: [history()], total: 1 })
      if (path === '/api/creation/history/188' && !init?.method) return Response.json(history())
      if (path === '/api/creation/history/188/document' && init?.method === 'PUT') {
        savedDocument = JSON.parse(String(init.body)).content
        revision += 1
        return Response.json({ history_id: 188, revision_no: revision,
          content: savedDocument, document_hash: await sha256Hex(savedDocument), changed: true })
      }
      if (path === '/api/creation/brainstorm/turn') return Response.json(state)
      return Response.json({ connected: false })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    expect(savedBrief.phase).toBe('chapter_writing')
    expect(screen.getByText('资料待核实草稿')).toBeInTheDocument()
    fireEvent.click(screen.getByText(rootRequest).closest('button')!)
    expect(await screen.findByText(guidance)).toBeInTheDocument()
    expect(screen.getByText('资料待核实草稿')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('本期资料待核实；新建创作后可使用新增资料。')).toBeDisabled()
    expect(screen.getByRole('button', { name: '提交' })).toBeDisabled()
    const manualEdit = screen.getByRole('button', { name: '编辑文档' })
    await waitFor(() => expect(manualEdit).toBeEnabled())
    fireEvent.click(manualEdit)
    const editor = await screen.findByRole('textbox', { name: '文档 Markdown 内容' })
    fireEvent.change(editor, { target: { value: `${document}\n\n人工补充：仍待核对。` } })
    fireEvent.click(screen.getByRole('button', { name: '保存文档' }))
    await waitFor(() => expect(screen.queryByRole('textbox', { name: '文档 Markdown 内容' })).not.toBeInTheDocument())
    expect(screen.getByText(guidance)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交' })).toBeDisabled()
    expect(requests.some(item => item.includes('/api/creation/agent/run'))).toBe(false)
    const newCreation = screen.getByRole('button', { name: '新建创作' })
    expect(newCreation).toBeEnabled()
    fireEvent.click(newCreation)
    expect(screen.queryByText(guidance)).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)).toBeEnabled()
    expect(useAppStore.getState().creationDraft.creationMode).toBe('brainstorm')
    expect(useAppStore.getState().creationDraft.sessionId).toBeNull()
  })

  it.each([
    ['v16', completedState(16), review],
    ['v17 with ordinary delivery', completedState(), { status: 'pass', provisional: false, source_validation: 'verified' }],
  ])('keeps %s follow-up available', async (_name, state, deliveryReview) => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/brainstorm/turn') return Response.json(state)
      return Response.json({ connected: false })
    }))
    useAppStore.getState().setCreationDraft({
      creationMode: 'brainstorm', sessionId: state.session_id, rootRequest,
      brainstormState: state, generatedContent: document, prompt: '请补充结论',
      agentEvents: reviewedEvents(state, deliveryReview),
    })
    render(<CreationPanel />)
    await waitFor(() => expect(screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/)).toBeEnabled())
    expect(screen.queryByText(guidance)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交' })).toBeEnabled()
  })
})
