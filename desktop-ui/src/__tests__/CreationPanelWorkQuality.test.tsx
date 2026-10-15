import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'
import { emptyWorkContext } from '../utils/creationWorkContext'

beforeEach(() => {
  window.localStorage.clear()
  useAppStore.getState().reset()
  useAppStore.getState().resetCreationDraft()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
})
afterEach(() => vi.unstubAllGlobals())

describe('工作上下文真实请求链路', () => {
  it('脑暴开始随请求保存工作设置，恢复已保存设置而不把样稿混入用户原始需求', async () => {
    const context = { ...emptyWorkContext(), purpose: '评审试点', decision_goal: '是否批准试点', style_samples: ['虚构样稿中的成本和日期'], review_enabled: true }
    const savedContext = { ...context, constraints: '已保存的人员边界' }
    useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', workContext: context })
    const payloads: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/history/start') return Response.json({ id: 1 })
      if (path === '/api/creation/skills/match') return Response.json({ matches: [] })
      if (path === '/api/creation/brainstorm/turn') {
        const body = JSON.parse(String(init?.body)); payloads.push(body)
        return Response.json({ session_id: body.session_id, phase: 'ready', revision: 0, current_question: null,
          brief_markdown: '# 创作简报', answered_count: 0, work_context: savedContext })
      }
      return Response.json({ connected: false })
    }))
    render(<CreationPanel />)
    fireEvent.change(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/), { target: { value: '设计入口试点方案' } })
    fireEvent.click(screen.getByRole('button', { name: '开始梳理' }))
    await waitFor(() => expect(useAppStore.getState().creationDraft.brainstormState?.phase).toBe('ready'))
    expect(payloads[0]).toMatchObject({ action: 'start', work_context: context, root_request: '设计入口试点方案' })
    expect(payloads[0].root_request).not.toContain('成本和日期')
    expect(useAppStore.getState().creationDraft.workContext).toEqual(savedContext)
  })

  it('预览发送全文和工作设置，候选单独展示，原正文及已确认脑暴均不修改', async () => {
    const document = '# 原方案\n\n保留现有工作流程。'
    const context = { ...emptyWorkContext(), purpose: '项目评审', profile_id: 'proposal_review', review_enabled: true }
    useAppStore.getState().setCreationDraft({ generatedContent: document, rootRequest: '设计试点方案', workContext: context })
    const requests: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/work-quality/preview') {
        requests.push(JSON.parse(String(init?.body)))
        return Response.json({ status: 'candidate', content: '试写候选，只验证入口与责任分工。', read_only: true })
      }
      return Response.json({ connected: false })
    }))
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('tab', { name: '创作参数' }))
    fireEvent.click(screen.getByRole('button', { name: '试写关键段落' }))
    expect(await screen.findByText('试写候选，只验证入口与责任分工。')).toBeInTheDocument()
    expect(requests).toEqual([{ action: 'trial', instruction: '设计试点方案', current_document: document, work_context: context }])
    expect(useAppStore.getState().creationDraft.generatedContent).toBe(document)
    expect(useAppStore.getState().creationDraft.brainstormState).toBeNull()
    expect(window.localStorage.getItem('memorybread.creation-work-context-draft.v1')).toContain('项目评审')
  })

  it('直出历史恢复工作设置而不制造脑暴状态，后续 Agent 与历史保存均传递', async () => {
    const context = { ...emptyWorkContext(), purpose: '申请试点', decision_goal: '是否批准试点', review_enabled: true }
    const requests: any[] = [], histories: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history' && init?.method !== 'POST') return Response.json({ items: [{ id: 18, root_request: '试点方案', prompt: '试点方案', generated_content: '# 原稿\n\n保留现有系统。', creation_mode: 'direct', creation_brief_json: JSON.stringify({ work_context: context }) }], total: 1 })
      if (path === '/api/creation/history/start' || (path === '/api/creation/history' && init?.method === 'POST')) {
        histories.push(JSON.parse(String(init?.body))); return Response.json({ id: 18, progress_epoch: 1 })
      }
      if (path === '/api/creation/agent/run') {
        requests.push(JSON.parse(String(init?.body)))
        const event = { schema_version: 'creation.agent.v1', event_id: 'work-done', session_id: 'history-18', run_id: 'work-run', sequence: 1, timestamp: 1, type: 'run.completed', status: 'completed', actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' }, summary: '已回答', data: { response: '方案可先验证入口。' }, environment_patch: {} }
        return new Response(`data: ${JSON.stringify(event)}\n\n`, { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return Response.json({ connected: false })
    }))
    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(screen.getByText('试点方案').closest('button')!)
    expect(useAppStore.getState().creationDraft.workContext).toEqual(context)
    expect(useAppStore.getState().creationDraft.brainstormState).toBeNull()
    fireEvent.change(screen.getByPlaceholderText(/继续告诉 Agent 如何修改当前文档/), { target: { value: '只回答当前方案的风险，不修改正文。' } })
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({ work_context: context, creation_brief: { work_context: context } })
    expect(histories[0].creation_brief).toEqual({ work_context: context })
  })
})
