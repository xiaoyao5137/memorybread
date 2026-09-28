import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { sha256Hex } from '../components/creation-selection/creationInlineEdit'
import { useAppStore } from '../store/useAppStore'

const original = [
  '# 项目方案',
  '',
  '旧结论需要人工修正。',
  '',
  '| 指标 | 当前值 |',
  '| --- | --- |',
  '| 覆盖率 | 待确认 |',
  '',
  '```mermaid',
  'graph TD',
  '  A[开始] --> B[结束]',
  '```',
  '',
  '[来源](https://example.com/source)',
].join('\n')

const edited = original.replace('旧结论需要人工修正。', '新结论已由用户修正。')

type HistoryRecord = ReturnType<typeof historyRecord>

const historyRecord = (id: number, content = original) => ({
  id,
  prompt: `创作记录 ${id}`,
  root_request: `创作记录 ${id}`,
  generated_content: content,
  session_id: `session-${id}`,
  lifecycle_status: 'completed',
  revision_no: 3,
  references_json: '[]',
  conversation_json: '[]',
  agent_trace_json: '[]',
  evidence_json: '[]',
  created_at: 1,
  updated_at: 2,
})

const mockHistoryApi = (options: {
  records?: HistoryRecord[]
  saveResponse?: (payload: Record<string, unknown>) => Promise<Response> | Response
  enableSelection?: boolean
  legacyGeneration?: string
} = {}) => {
  const records = new Map((options.records || [historyRecord(101)]).map(item => [item.id, item]))
  const saves: Array<{ path: string; payload: Record<string, unknown> }> = []
  const legacySaves: Record<string, unknown>[] = []
  const requests: string[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    const path = url.pathname
    requests.push(`${init?.method || 'GET'} ${path}`)
    if (path === '/api/creation/skills') return Response.json([])
    if (options.legacyGeneration && path === '/api/creation/history/start') {
      return Response.json({ id: 101, progress_epoch: 1 })
    }
    if (options.legacyGeneration && path === '/api/creation/agent/run') {
      return new Response('{}', { status: 404 })
    }
    if (options.legacyGeneration && path === '/api/creation/references') {
      return Response.json({ references: [] })
    }
    if (options.legacyGeneration && path === '/api/creation/generate') {
      return new Response(`data: ${JSON.stringify({ content: options.legacyGeneration })}\n\n`, {
        headers: { 'Content-Type': 'text/event-stream' },
      })
    }
    if (options.legacyGeneration && /^\/api\/creation\/history\/\d+\/progress$/.test(path)) {
      return new Response(null, { status: 204 })
    }
    if (options.legacyGeneration && path === '/api/creation/history' && init?.method === 'POST') {
      const payload = JSON.parse(String(init.body || '{}')) as Record<string, unknown>
      legacySaves.push(payload)
      const item = records.get(Number(payload.history_id))
      if (!item || payload.base_revision_no !== item.revision_no
        || payload.base_document_hash !== await sha256Hex(item.generated_content)) {
        return Response.json({ error: 'CREATION_DOCUMENT_EDIT_CONFLICT' }, { status: 409 })
      }
      records.set(item.id, { ...item, generated_content: String(payload.generated_content), revision_no: item.revision_no + 1 })
      return Response.json({ id: item.id })
    }
    if (path === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
      return Response.json({ items: [...records.values()], total: records.size, limit: 20, offset: 0 })
    }
    const documentMatch = path.match(/^\/api\/creation\/history\/(\d+)\/document$/)
    if (documentMatch && init?.method === 'PUT') {
      const payload = JSON.parse(String(init.body || '{}')) as Record<string, unknown>
      saves.push({ path, payload })
      if (options.saveResponse) return options.saveResponse(payload)
      const id = Number(documentMatch[1])
      const prior = records.get(id)!
      const content = String(payload.content || '')
      const updated = { ...prior, generated_content: content, revision_no: prior.revision_no + 1, updated_at: Date.now() }
      records.set(id, updated)
      return Response.json({
        history_id: id,
        session_id: updated.session_id,
        content,
        revision_no: updated.revision_no,
        document_hash: await sha256Hex(content),
        changed: true,
      })
    }
    const historyMatch = path.match(/^\/api\/creation\/history\/(\d+)$/)
    if (historyMatch && (!init?.method || init.method === 'GET')) {
      const item = records.get(Number(historyMatch[1]))
      return item ? Response.json(item) : new Response('{}', { status: 404 })
    }
    if (path === '/api/creation/inline-edit/capabilities' && options.enableSelection) {
      const item = records.get(Number(url.searchParams.get('history_id')))
      if (!item) return new Response('{}', { status: 404 })
      return Response.json({
        schema_version: 'creation.inline-edit.v1', enabled: true,
        actions: ['polish', 'expand', 'elaborate'],
        max_selection_bytes: 12000, max_custom_prompt_bytes: 2000,
        supported_node_kinds: ['p', 'h1', 'h2', 'h3', 'li', 'blockquote'],
        history_id: item.id, revision_no: item.revision_no,
        base_document_hash: await sha256Hex(item.generated_content), disabled_reason: null,
      })
    }
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { records, saves, legacySaves, requests, fetchMock }
}

const openHistory = async (id = 101, total = 1) => {
  fireEvent.click(await screen.findByRole('button', { name: `创作记录 (${total})` }))
  fireEvent.click(await screen.findByText(`创作记录 ${id}`))
  await waitFor(() => expect(useAppStore.getState().creationDraft.sessionId).toBe(`session-${id}`))
}

const beginEdit = async () => {
  const documentPanel = screen.getByRole('region', { name: '生成内容' })
  fireEvent.click(within(documentPanel).getByRole('button', { name: '编辑文档' }))
  return await within(documentPanel).findByRole('textbox', { name: '文档 Markdown 内容' }) as HTMLTextAreaElement
}

beforeEach(() => {
  window.sessionStorage.clear()
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => ({ left: 120, top: 240, width: 160, height: 22, bottom: 262 }),
  })
})

afterEach(() => {
  window.sessionStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('创作文档手动原地编辑', () => {
  it('保存完整 Markdown 到当前历史版本，重新打开后保留表格、图示与链接', async () => {
    const api = mockHistoryApi()
    const view = render(<CreationPanel />)
    await openHistory()
    const textarea = await beginEdit()
    expect(textarea).toHaveValue(original)
    fireEvent.change(textarea, { target: { value: edited } })
    fireEvent.click(within(screen.getByRole('region', { name: '生成内容' })).getByRole('button', { name: /保存/ }))

    await waitFor(() => expect(api.saves).toHaveLength(1))
    expect(api.saves[0]).toMatchObject({
      path: '/api/creation/history/101/document',
      payload: {
        session_id: 'session-101',
        base_revision_no: 3,
        base_document_hash: await sha256Hex(original),
        content: edited,
      },
    })
    await waitFor(() => {
      expect(useAppStore.getState().creationDraft.generatedContent).toBe(edited)
      expect(screen.getByText('新结论已由用户修正。')).toBeInTheDocument()
    })
    expect(api.records.get(101)?.generated_content).toBe(edited)

    view.unmount()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
    render(<CreationPanel />)
    await openHistory()
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(edited)
    expect(screen.getByText('新结论已由用户修正。')).toBeInTheDocument()
  })

  it('版本冲突时保留未保存的用户输入，不覆盖已交付正文', async () => {
    const api = mockHistoryApi({
      saveResponse: () => Response.json({ error: 'DOCUMENT_VERSION_CONFLICT' }, { status: 409 }),
    })
    render(<CreationPanel />)
    await openHistory()
    const textarea = await beginEdit()
    fireEvent.change(textarea, { target: { value: edited } })
    fireEvent.click(within(screen.getByRole('region', { name: '生成内容' })).getByRole('button', { name: /保存/ }))

    await waitFor(() => expect(api.saves).toHaveLength(1))
    expect(await screen.findByText(/草稿已保留/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '文档 Markdown 内容' })).toHaveValue(edited)
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(original)
    expect(api.records.get(101)?.generated_content).toBe(original)
    expect(api.fetchMock.mock.calls.some(([input, init]) => (
      new URL(String(input)).pathname === '/api/creation/history' && init?.method === 'POST'
    ))).toBe(false)
  })

  it('旧版无会话编号的记录按真实空 session_id 保存，不发送页面合成编号', async () => {
    const api = mockHistoryApi({ records: [{ ...historyRecord(103), session_id: '' }] })
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('创作记录 103'))
    await waitFor(() => expect(useAppStore.getState().creationDraft.sessionId).toBe('history-103'))
    const textarea = await beginEdit()
    fireEvent.change(textarea, { target: { value: edited } })
    fireEvent.click(within(screen.getByRole('region', { name: '生成内容' })).getByRole('button', { name: /保存/ }))

    await waitFor(() => expect(api.saves).toHaveLength(1))
    expect(api.saves[0].payload.session_id).toBe('')
    await waitFor(() => expect(useAppStore.getState().creationDraft.generatedContent).toBe(edited))
  })

  it('手编后旧版生成器兜底保存沿用生成前的持久正文基线', async () => {
    const generated = `${edited}\n\n## Agent 补充\n\n新增执行步骤。`
    const api = mockHistoryApi({ legacyGeneration: generated })
    render(<CreationPanel />)
    await openHistory()
    const textarea = await beginEdit()
    fireEvent.change(textarea, { target: { value: edited } })
    fireEvent.click(within(screen.getByRole('region', { name: '生成内容' })).getByRole('button', { name: /保存/ }))
    await waitFor(() => expect(api.records.get(101)?.revision_no).toBe(4))
    await waitFor(() => expect(useAppStore.getState().creationDraft.generatedContent).toBe(edited))

    fireEvent.change(screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/), {
      target: { value: '在当前文档中补充执行步骤' },
    })
    fireEvent.click(screen.getByRole('button', { name: '提交' }))

    await waitFor(() => expect(api.legacySaves).toHaveLength(1))
    expect(api.legacySaves[0]).toMatchObject({
      history_id: 101,
      session_id: 'session-101',
      base_revision_no: 4,
      base_document_hash: await sha256Hex(edited),
      generated_content: generated,
    })
    expect(api.legacySaves[0].base_document_hash).not.toBe(await sha256Hex(original))
    expect(api.legacySaves[0].base_document_hash).not.toBe(await sha256Hex(generated))
    await waitFor(() => expect(api.records.get(101)?.generated_content).toBe(generated))
    const manualSaveIndex = api.requests.indexOf('PUT /api/creation/history/101/document')
    const frozenBaseIndex = api.requests.findIndex((request, index) => (
      index > manualSaveIndex && request === 'GET /api/creation/history/101'
    ))
    const generationIndex = api.requests.indexOf('POST /api/creation/generate')
    expect(manualSaveIndex).toBeGreaterThanOrEqual(0)
    expect(frozenBaseIndex).toBeGreaterThan(manualSaveIndex)
    expect(generationIndex).toBeGreaterThan(frozenBaseIndex)
  })

  it('取消编辑丢弃草稿，不提交正文', async () => {
    const api = mockHistoryApi()
    render(<CreationPanel />)
    await openHistory()
    const textarea = await beginEdit()
    fireEvent.change(textarea, { target: { value: edited } })
    fireEvent.click(within(screen.getByRole('region', { name: '生成内容' })).getByRole('button', { name: /取消/ }))

    await waitFor(() => expect(screen.queryByRole('textbox', { name: '文档 Markdown 内容' })).not.toBeInTheDocument())
    expect(screen.getByText('旧结论需要人工修正。')).toBeInTheDocument()
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(original)
    expect(api.saves).toHaveLength(0)
  })

  it('重建面板后恢复同一历史的未保存草稿，取消后重新编辑显示服务端原文', async () => {
    const api = mockHistoryApi()
    const first = render(<CreationPanel />)
    await openHistory()
    const textarea = await beginEdit()
    fireEvent.change(textarea, { target: { value: edited } })
    await waitFor(() => expect(window.sessionStorage.length).toBeGreaterThan(0))

    first.unmount()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
    render(<CreationPanel />)
    await openHistory()
    expect(await beginEdit()).toHaveValue(edited)
    expect(screen.getByText(/已恢复这份文档的未保存草稿/)).toBeInTheDocument()
    expect(api.records.get(101)?.generated_content).toBe(original)

    fireEvent.click(within(screen.getByRole('region', { name: '生成内容' })).getByRole('button', { name: /取消/ }))
    expect(await beginEdit()).toHaveValue(original)
    expect(api.saves).toHaveLength(0)
  })

  it('编辑期间不执行 Agent 生成或旧选区改写', async () => {
    const api = mockHistoryApi({ enableSelection: true })
    render(<CreationPanel />)
    await openHistory()
    await waitFor(() => expect(api.fetchMock.mock.calls.some(([input]) => (
      new URL(String(input)).pathname === '/api/creation/inline-edit/capabilities'
    ))).toBe(true))

    const paragraph = await waitFor(() => {
      const node = document.querySelector('.creation-document-content p')
      expect(node).toHaveAttribute('data-md-start')
      return node as HTMLParagraphElement
    })
    const range = document.createRange()
    range.selectNodeContents(paragraph)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(paragraph)
    expect(await screen.findByRole('toolbar', { name: '所选内容操作' })).toBeInTheDocument()

    await beginEdit()
    expect(screen.queryByRole('toolbar', { name: '所选内容操作' })).not.toBeInTheDocument()
    const prompt = screen.queryByPlaceholderText(/输入 @ 可选择/)
    if (prompt) fireEvent.change(prompt, { target: { value: '继续完善当前方案' } })
    const submit = screen.queryByRole('button', { name: '提交' })
    if (submit) fireEvent.click(submit)
    expect(api.fetchMock.mock.calls.some(([input]) => (
      ['/api/creation/history/start', '/api/creation/agent/run', '/api/creation/inline-edit/run']
        .includes(new URL(String(input)).pathname)
    ))).toBe(false)
  })

  it('切换历史前保留未保存草稿或要求用户确认', async () => {
    mockHistoryApi({ records: [historyRecord(101), historyRecord(102, '# 第二篇文档\n\n原文二。')] })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<CreationPanel />)
    await openHistory(101, 2)
    const textarea = await beginEdit()
    fireEvent.change(textarea, { target: { value: edited } })

    fireEvent.click(screen.getByRole('button', { name: '创作记录 (2)' }))
    fireEvent.click(await screen.findByText('创作记录 102'))
    const currentEditor = screen.queryByRole('textbox', { name: '文档 Markdown 内容' }) as HTMLTextAreaElement | null
    const stayedOnOriginal = useAppStore.getState().creationDraft.sessionId === 'session-101'
      && currentEditor?.value === edited
    const showsConfirmation = screen.queryAllByText(/未保存/).length > 0
    if (confirm.mock.calls.length > 0) {
      expect(useAppStore.getState().creationDraft.sessionId).toBe('session-101')
    } else {
      expect(stayedOnOriginal || showsConfirmation).toBe(true)
    }
  })
})
