import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'

const CHAT_MESSAGE_CREATED_AT = new Date(2020, 7, 2, 14, 7).getTime()

describe('创作新会话', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
    useAppStore.getState().setCreationDraft({
      prompt: '继续补充风险章节',
      docType: '技术方案',
      audience: '研发团队',
      generatedContent: '# 当前文档',
      enableWebSearch: true,
      contentWeight: 35,
      referencePreview: {
        requirement: {
          topic: 'Agent 架构',
          doc_type: '技术方案',
          audience: '研发团队',
          style: '',
          keywords: [],
        },
        references: [],
      },
      dataReferences: [{
        source_id: 7,
        title: '经营看板',
        source_kind: 'report_url',
        freshness_class: 'fresh',
        refresh_required: false,
        can_use: true,
      }],
      sessionId: 'session-existing',
      rootRequest: '设计 Agent 架构方案',
      conversation: [{
        id: 'message-1',
        role: 'user',
        content: '设计 Agent 架构方案',
        createdAt: CHAT_MESSAGE_CREATED_AT,
      }],
      agentEvents: [{
        schema_version: 'creation.agent.v1',
        event_id: 'event-1',
        session_id: 'session-existing',
        run_id: 'run-1',
        sequence: 1,
        timestamp: 1,
        type: 'run.completed',
        status: 'completed',
        actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
        summary: '本轮创作完成',
        data: {},
      }],
    })

    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('像 IM 软件一样展示每条消息的发言时间', async () => {
    render(<CreationPanel />)
    await act(async () => {
      await Promise.resolve()
    })

    const timestamp = screen.getByLabelText('用户消息').querySelector('time')
    expect(timestamp).toHaveTextContent('2020年8月2日 14:07')
    expect(timestamp).toHaveAttribute('datetime', new Date(CHAT_MESSAGE_CREATED_AT).toISOString())
    expect(timestamp).toHaveAttribute('title', '发送于 2020年8月2日 14:07')
  })

  it('未登录时在用户发言上展示本地生成的昵称', async () => {
    useAppStore.getState().setLocalNickname('好奇的小法棍')

    render(<CreationPanel />)
    await act(async () => {
      await Promise.resolve()
    })

    const userMessage = screen.getByLabelText('用户消息')
    expect(userMessage).toHaveTextContent('好奇的小法棍')
    expect(userMessage.querySelector('.creation-chat-message__meta > span')).not.toHaveTextContent(/^用户$/)
  })

  it('从页面开启新会话，并保留创作偏好', async () => {
    render(<CreationPanel />)

    fireEvent.click(screen.getByRole('button', { name: '开启新会话' }))

    await waitFor(() => {
      const draft = useAppStore.getState().creationDraft
      expect(draft).toMatchObject({
        prompt: '',
        generatedContent: '',
        referencePreview: null,
        dataReferences: [],
        sessionId: null,
        rootRequest: '',
        conversation: [],
        agentEvents: [],
        docType: '技术方案',
        audience: '研发团队',
        enableWebSearch: true,
        contentWeight: 35,
      })
    })
    expect(screen.getByRole('button', { name: '开启新会话' })).toBeDisabled()
    expect(screen.queryByText('当前文档')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('用户消息')).not.toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)).toHaveFocus()
    })
  })

  it('可在非生成状态终止当前会话，并保留内容直到开启新会话', async () => {
    render(<CreationPanel />)

    const terminateButton = screen.getByRole('button', { name: '终止当前会话' })
    expect(terminateButton).toBeEnabled()
    fireEvent.click(terminateButton)

    expect(await screen.findByLabelText('会话终止消息')).toHaveTextContent('终止了当前会话')
    expect(screen.getByText('会话已终止，已有内容仍可查看')).toBeInTheDocument()
    expect(terminateButton).toBeDisabled()
    expect(terminateButton).toHaveTextContent('已终止')
    expect(screen.getByPlaceholderText(/继续告诉 Agent/)).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '开启新会话' })).toBeEnabled()
    expect(screen.getByText('当前文档')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开启新会话' }))
    expect(screen.queryByLabelText('会话终止消息')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '终止当前会话' })).toBeDisabled()
  })
})
