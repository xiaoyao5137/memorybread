import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'
import { CREATION_TOOLS_STORAGE_KEY } from '../utils/creationTools'

describe('创作工具 Tab', () => {
  beforeEach(() => {
    window.localStorage.removeItem(CREATION_TOOLS_STORAGE_KEY)
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
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
    window.localStorage.removeItem(CREATION_TOOLS_STORAGE_KEY)
  })

  it('使用精简卡片展示必备 Tool，并可分别安装和开启可选 Tool', async () => {
    render(<CreationPanel />)
    fireEvent.click(screen.getByRole('button', { name: '工具 (4)' }))

    expect(screen.queryByText('随 MemoryBread 默认安装并开启，保证创作具备公开信息与本地记忆两类基础上下文。')).not.toBeInTheDocument()
    expect(screen.queryByText('安装后才会进入 Agent 的可用能力列表；关闭时保留安装，但本次创作不会调用。')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '当前工具调用链' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '互联网检索 Tool已安装' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '互联网检索 Tool已开启' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '记忆搜索 Tool已安装' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '记忆搜索 Tool已开启' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '数据检索 Tool已安装' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '数据检索 Tool已开启' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '网页爬取 Tool已安装' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '网页爬取 Tool已开启' })).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: '记忆搜索 Tool默认召回条数' })).toHaveValue(10)
    expect(screen.getByRole('spinbutton', { name: '数据检索 Tool默认召回条数' })).toHaveValue(30)

    fireEvent.change(
      screen.getByRole('spinbutton', { name: '记忆搜索 Tool默认召回条数' }),
      { target: { value: '14' } },
    )
    fireEvent.change(
      screen.getByRole('spinbutton', { name: '数据检索 Tool默认召回条数' }),
      { target: { value: '42' } },
    )

    const plantUmlCard = screen.getByText('PlantUML 画图 Tool').closest('article')
    expect(plantUmlCard).not.toBeNull()
    const plantUmlActions = within(plantUmlCard as HTMLElement)
    expect(plantUmlActions.getByRole('button', { name: '开启PlantUML 画图 Tool' })).toBeDisabled()

    fireEvent.click(plantUmlActions.getByRole('button', { name: '安装PlantUML 画图 Tool' }))
    expect(plantUmlActions.getByRole('button', { name: '卸载PlantUML 画图 Tool' })).toBeEnabled()
    expect(plantUmlActions.getByRole('button', { name: '开启PlantUML 画图 Tool' })).toBeEnabled()

    fireEvent.click(plantUmlActions.getByRole('button', { name: '开启PlantUML 画图 Tool' }))
    expect(plantUmlActions.getByRole('button', { name: '关闭PlantUML 画图 Tool' })).toBeEnabled()
    fireEvent.click(plantUmlActions.getByRole('button', { name: '关闭PlantUML 画图 Tool' }))
    expect(plantUmlActions.getByRole('button', { name: '开启PlantUML 画图 Tool' })).toBeEnabled()

    await waitFor(() => {
      const stored = JSON.parse(window.localStorage.getItem(CREATION_TOOLS_STORAGE_KEY) || '[]')
      expect(stored.find((tool: { id: string }) => tool.id === 'plantuml_diagram')).toMatchObject({
        installed: true,
        enabled: false,
      })
      expect(stored.find((tool: { id: string }) => tool.id === 'memory_search')).toMatchObject({
        resultLimit: 14,
      })
      expect(stored.find((tool: { id: string }) => tool.id === 'data_search')).toMatchObject({
        resultLimit: 42,
      })
    })
  })

  it('把已开启 Tool ID 传给创作 Agent', async () => {
    window.localStorage.setItem(CREATION_TOOLS_STORAGE_KEY, JSON.stringify([
      { id: 'internet_search', installed: true, enabled: true },
      { id: 'memory_search', installed: true, enabled: true, resultLimit: 12 },
      { id: 'data_search', installed: true, enabled: true, resultLimit: 37 },
      { id: 'webpage_scrape', installed: true, enabled: true },
      { id: 'github_search', installed: true, enabled: true },
    ]))
    const agentPayloads: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        agentPayloads.push(JSON.parse(String(init?.body || '{}')))
        return new Response([
          'data: {"schema_version":"creation.agent.v1","event_id":"e1","session_id":"s1","run_id":"r1","sequence":1,"timestamp":1,"type":"run.completed","status":"completed","actor":{"kind":"agent","id":"creation_main_agent","name":"创作 Agent"},"summary":"完成","goal":{"status":"complete","revision":1,"remaining_steps":[]},"environment_patch":{},"data":{"document":"# 方案\\n\\n## 背景\\n\\n内容\\n\\n## 设计\\n\\n内容\\n\\n## 验证\\n\\n内容"}}\n\n',
        ].join(''), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 1 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.change(
      within(screen.getByRole('region', { name: '创作对话' })).getByRole('textbox'),
      {
      target: { value: '检索 GitHub 开源仓库并生成技术选型方案' },
      },
    )
    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))

    await waitFor(() => expect(agentPayloads).toHaveLength(1))
    expect(agentPayloads[0].enabled_tools).toEqual([
      'internet_search',
      'memory_search',
      'data_search',
      'webpage_scrape',
      'github_search',
    ])
    expect(agentPayloads[0].max_references).toBe(12)
    expect(agentPayloads[0].data_search_limit).toBe(37)
  })
})
