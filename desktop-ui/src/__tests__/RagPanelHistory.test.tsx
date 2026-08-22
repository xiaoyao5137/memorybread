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
    await waitFor(() => expect(mocks.ragQuery).toHaveBeenCalledWith(
      '第一行',
      undefined,
      {},
      expect.any(AbortSignal),
    ))
  })

  it('提问完成后参考资料不自动弹出，点击标签后才展示', async () => {
    const contexts = [{ capture_id: 1, text: '召回资料', score: 0.9, source: 'capture' as const }]
    mocks.ragQuery.mockImplementation(async () => {
      useAppStore.getState().setRagResult('咨询回答', contexts)
      return { answer: '咨询回答', contexts }
    })

    render(<RagPanel />)
    const input = screen.getByTestId('rag-panel-input')
    fireEvent.change(input, { target: { value: '上周工作' } })
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })

    await waitFor(() => {
      expect(screen.getByTestId('rag-panel-answer')).toHaveTextContent('咨询回答')
    })
    expect(screen.queryByTestId('rag-panel-contexts')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /参考资料 \(1\)/ }))
    expect(screen.getByTestId('rag-panel-contexts')).toBeInTheDocument()
  })
})

describe('清空当前会话', () => {
  it('点击清空后重置指令、咨询输出与参考资料', async () => {
    useAppStore.getState().setRagQuery('上一轮问题')
    useAppStore.getState().setRagResult('上一轮回答', [{ capture_id: 9, text: '参考资料', score: 0.8, source: 'capture' }])

    render(<RagPanel />)
    const input = screen.getByTestId('rag-panel-input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: '待清空的指令' } })
    expect(screen.getByTestId('rag-panel-answer')).toHaveTextContent('上一轮回答')
    fireEvent.click(screen.getByRole('button', { name: /参考资料 \(1\)/ }))
    expect(screen.getByTestId('rag-panel-contexts')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('rag-panel-clear'))

    expect(input.value).toBe('')
    expect(useAppStore.getState().ragQuery).toBe('')
    expect(useAppStore.getState().ragAnswer).toBe('')
    expect(useAppStore.getState().ragContexts).toEqual([])
    expect(screen.getByTestId('rag-panel-answer')).toHaveTextContent('选择模板或输入问题后，咨询输出会在这里呈现。')
    expect(screen.queryByTestId('rag-panel-contexts')).not.toBeInTheDocument()
  })

  it('咨询进行中点击清空会中断请求并退出加载状态', async () => {
    let capturedSignal: AbortSignal | undefined
    mocks.ragQuery.mockImplementation(() => new Promise(() => {}))

    render(<RagPanel />)
    fireEvent.change(screen.getByTestId('rag-panel-input'), { target: { value: '长时间咨询' } })
    fireEvent.click(screen.getByTestId('rag-panel-submit'))

    await waitFor(() => {
      capturedSignal = mocks.ragQuery.mock.calls[0][3]
      expect(capturedSignal).toBeInstanceOf(AbortSignal)
    })
    // 真实 useRagQuery 会在请求开始时置 loading，这里用 store 模拟同等状态
    useAppStore.getState().setRagLoading(true)

    fireEvent.click(screen.getByTestId('rag-panel-clear'))

    expect(capturedSignal?.aborted).toBe(true)
    expect(useAppStore.getState().ragLoading).toBe(false)
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
