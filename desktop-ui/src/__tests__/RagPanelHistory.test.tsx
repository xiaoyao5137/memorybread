import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import RagPanel from '../components/RagPanel.v2'
import { useAppStore } from '../store/useAppStore'
import type { RagHistoryItem, RagHistoryPage } from '../types'

const mocks = vi.hoisted(() => ({
  fetchBillingBalance: vi.fn(),
  fetchHistory: vi.fn(),
  ragQuery: vi.fn(),
}))

vi.mock('../hooks/useApi', () => ({
  useFetchRagHistory: () => mocks.fetchHistory,
  useRagQuery: () => mocks.ragQuery,
  useModelStatus: () => ({
    status: { llm: true, embedding: true, runtime: true },
    ready: true,
    loading: false,
  }),
}))

vi.mock('../utils/authApi', () => ({
  fetchBillingBalance: mocks.fetchBillingBalance,
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(),
}))

const item = (id: number, query: string): RagHistoryItem => ({
  id,
  ts: 1_720_000_000_000 + id,
  query,
  answer: `${query}的回答`,
  contexts: [],
  context_count: 0,
  latency_ms: 1200,
  model: 'mbcd-std-v1',
})

beforeEach(() => {
  Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: true })
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  mocks.ragQuery.mockReset()
  mocks.ragQuery.mockResolvedValue({ answer: '咨询回答', contexts: [] })
  mocks.fetchHistory.mockReset()
  mocks.fetchBillingBalance.mockReset()
  mocks.fetchHistory.mockImplementation(async (
    params: { limit: number; offset: number; query: string },
  ): Promise<RagHistoryPage> => {
    const searching = params.query === '年度规划'
    return {
      items: [item(params.offset + 1, searching ? '年度规划方案' : '最近咨询')],
      total: searching ? 25 : 45,
      limit: params.limit,
      offset: params.offset,
    }
  })
})

describe('咨询输入框提交', () => {
  it('离线打开默认咨询页时直接使用本地模型且不请求云端余额', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_offline_rag_token',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
      user: {
        id: 'offline-rag-user',
        username: '离线咨询用户',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: false })

    render(<RagPanel />)

    await waitFor(() => expect(mocks.fetchHistory).toHaveBeenCalled())
    expect(screen.getByTestId('rag-panel')).toBeInTheDocument()
    expect(mocks.fetchBillingBalance).not.toHaveBeenCalled()
  })

  it('输入法确认候选词时不提交咨询', async () => {
    render(<RagPanel />)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '咨询记录 (45)' })).toBeInTheDocument()
    })
    const input = screen.getByTestId('rag-panel-input')

    fireEvent.change(input, { target: { value: '你好' } })
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input, { data: '你好' })
    const defaultAllowed = fireEvent.keyDown(input, {
      key: 'Enter',
      code: 'Enter',
      keyCode: 229,
      isComposing: false,
    })

    expect(defaultAllowed).toBe(true)
    expect(mocks.ragQuery).not.toHaveBeenCalled()
  })

  it('普通 Enter 提交，Shift+Enter 保留换行', async () => {
    render(<RagPanel />)
    const input = screen.getByTestId('rag-panel-input')

    fireEvent.change(input, { target: { value: '第一行' } })
    expect(fireEvent.keyDown(input, {
      key: 'Enter',
      code: 'Enter',
      shiftKey: true,
    })).toBe(true)
    expect(mocks.ragQuery).not.toHaveBeenCalled()

    expect(fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })).toBe(false)
    await waitFor(() => expect(mocks.ragQuery).toHaveBeenCalledWith('第一行'))
  })
})

describe('咨询记录搜索与分页', () => {
  it('展示真实总数，并按关键词和页码请求记录', async () => {
    render(<RagPanel />)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '咨询记录 (45)' })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: '咨询记录 (45)' }))

    fireEvent.change(screen.getByLabelText('搜索咨询记录'), {
      target: { value: '年度规划' },
    })

    await waitFor(() => {
      expect(mocks.fetchHistory).toHaveBeenCalledWith(
        { limit: 20, offset: 0, query: '年度规划' },
        expect.any(AbortSignal),
      )
      expect(screen.getByText('年度规划方案')).toBeInTheDocument()
      expect(screen.getByText('找到 25 条')).toBeInTheDocument()
    }, { timeout: 1500 })

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))

    await waitFor(() => {
      expect(mocks.fetchHistory).toHaveBeenCalledWith(
        { limit: 20, offset: 20, query: '年度规划' },
        expect.any(AbortSignal),
      )
      expect(screen.getByText('第 2 / 2 页')).toBeInTheDocument()
    })
  })
})
