import { invoke } from '@tauri-apps/api/core'
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

vi.mock('../hooks/useConsultationReadiness', () => ({
  useConsultationReadiness: () => ({
    status: { llm: true, embedding: true, runtime: true },
    ready: true, loading: false, refresh: vi.fn().mockResolvedValue(true),
  }),
}))

vi.mock('../hooks/useApi', () => ({
  useFetchRagHistory: () => mocks.fetchHistory,
  useRagQuery: () => mocks.ragQuery,
  useModelStatus: () => ({
    status: { llm: true, embedding: true, runtime: true },
    refresh: vi.fn().mockResolvedValue(true),
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
  it('主咨询上传同名图片并艾特第二张，发送编号和原图引用', async () => {
    vi.mocked(invoke).mockResolvedValue({ path: '/local/image.png', ocr_text: '图中文字' })
    render(<RagPanel />)
    fireEvent.change(screen.getByLabelText('选择咨询图片'), { target: { files: [new File(['a'], 'Image.png', { type: 'image/png' }), new File(['b'], 'Image.png', { type: 'image/png' })] } })
    await screen.findByRole('button', { name: '查看图片 图片2' })
    const input = screen.getByTestId('rag-panel-input')
    fireEvent.change(input, { target: { value: '解释 @' } })
    fireEvent.click(screen.getByRole('option', { name: '@图片2' }))
    fireEvent.submit(screen.getByTestId('rag-panel-form'))
    await waitFor(() => expect(mocks.ragQuery).toHaveBeenCalledOnce())
    const [query, , metadata] = mocks.ragQuery.mock.calls[0]
    expect(query).toContain('解释 @图片2')
    expect(query).toContain('图片2（image/png')
    expect(metadata.attachments.map((entry: any) => entry.name)).toEqual(['图片1', '图片2'])
    expect(JSON.stringify(metadata)).not.toContain('base64')
  })

  it('多张上传图片在历史列表及打开后的咨询中均可预览', async () => {
    const record = item(141, '比较图片内容')
    record.contexts = [{ capture_id: 0, source: 'floating_assist', text: '附件', score: 1, attachments: [
      { id: 'a', name: 'a.png', type: 'image/png', size: 10, path: '/local/a.png' },
      { id: 'b', name: 'b.png', type: 'image/png', size: 10, path: '/local/b.png' },
    ] }]
    mocks.fetchHistory.mockResolvedValue({ items: [record], total: 1, limit: 20, offset: 0 })
    vi.mocked(invoke).mockResolvedValue('data:image/png;base64,YQ==')
    render(<RagPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '咨询记录 (1)' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '查看图片 图片2' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '查看图片 图片2' }))
    expect(screen.getByRole('dialog', { name: '图片原图预览' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭图片预览' }))
    fireEvent.click(screen.getByRole('button', { name: /比较图片内容/ }))
    expect(screen.getByRole('button', { name: '查看图片 图片1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看图片 图片2' })).toBeInTheDocument()
  })

  it('展开长输入可查看全部内容，编辑后收起仍保留文本且不会提交', async () => {
    render(<RagPanel />)
    await waitFor(() => expect(mocks.fetchHistory).toHaveBeenCalled())
    const input = screen.getByTestId('rag-panel-input')
    Object.defineProperty(input, 'scrollHeight', { configurable: true, value: 600 })
    const text = '长问题\n'.repeat(30)
    fireEvent.change(input, { target: { value: text } })
    expect(input).toHaveStyle({ height: 'auto', overflowY: 'auto' })

    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    expect(input).toHaveStyle({ height: '600px', maxHeight: 'none', overflowY: 'hidden' })
    expect(screen.getByRole('button', { name: '收起' })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.change(input, { target: { value: `${text}补充问题` } })
    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(input).toHaveStyle({ height: 'auto', overflowY: 'auto' })
    expect(input).toHaveValue(`${text}补充问题`)
    expect(mocks.ragQuery).not.toHaveBeenCalled()
  })

  it('历史记录进入、展开后收起及切换标签返回均使用同一初始高度', async () => {
    render(<RagPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '咨询记录 (45)' }))
    fireEvent.click(screen.getByRole('button', { name: /最近咨询/ }))
    const input = screen.getByTestId('rag-panel-input')
    const initialHeight = input.style.height
    expect(input).toHaveAttribute('rows', '3')
    expect(initialHeight).toBe('auto')
    Object.defineProperty(input, 'scrollHeight', { configurable: true, value: 600 })
    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    expect(input).toHaveStyle({ height: '600px' })
    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(input.style.height).toBe(initialHeight)
    fireEvent.change(input, { target: { value: '补充问题\n'.repeat(30) } })
    expect(input.style.height).toBe(initialHeight)
    fireEvent.click(screen.getByRole('button', { name: '咨询记录 (45)' }))
    fireEvent.click(screen.getByRole('button', { name: '咨询' }))
    expect(screen.getByTestId('rag-panel-input').style.height).toBe(initialHeight)
  })

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
  it('点击清空后重置指令、咨询结果与参考资料', async () => {
    useAppStore.getState().setRagQuery('上一轮问题')
    useAppStore.getState().setRagResult('上一轮回答', [{ capture_id: 9, text: '参考资料', score: 0.8, source: 'capture' }])

    render(<RagPanel />)
    expect(screen.getByText('咨询结果')).toBeInTheDocument()
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
    expect(screen.getByTestId('rag-panel-answer')).toHaveTextContent('选择模板或输入问题后，咨询结果会在这里呈现。')
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
  it('每次打开历史记录都折叠参考资料，仍可手动展开', async () => {
    mocks.fetchHistory.mockResolvedValue({
      items: [{ ...item(1, '历史问题'), contexts: [
        { capture_id: 9, text: '历史参考内容', score: 0.8, source: 'capture' },
      ], context_count: 1 }], total: 1, limit: 20, offset: 0,
    })
    render(<RagPanel />)
    for (let attempt = 0; attempt < 2; attempt += 1) {
      fireEvent.click(await screen.findByRole('button', { name: '咨询记录 (1)' }))
      fireEvent.click(screen.getByRole('button', { name: /历史问题/ }))
      expect(screen.getByTestId('rag-panel-input')).toHaveValue('历史问题')
      expect(screen.getByTestId('rag-panel-answer')).toHaveTextContent('历史问题的回答')
      expect(screen.queryByTestId('rag-panel-contexts')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: /参考资料 \(1\)/ }))
      expect(screen.getByTestId('rag-panel-contexts')).toBeInTheDocument()
    }
  })

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


describe('咨询历史截图', () => {
  const showScreenshotHistory = async () => {
    mocks.fetchHistory.mockResolvedValue({
      items: [{ ...item(137, '屏幕咨询'), contexts: [{
        capture_id: 0, text: '屏幕文字', score: 1,
        source: 'floating_assist', source_type: 'floating_assist',
        screenshot_path: '/history/saved.jpg',
      }] }], total: 1, limit: 20, offset: 0,
    })
    render(<RagPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '咨询记录 (1)' }))
  }

  it('加载原图并允许预览', async () => {
    vi.mocked(invoke).mockResolvedValue('data:image/jpeg;base64,aW1hZ2U=')
    await showScreenshotHistory()
    const thumbnail = await screen.findByRole('button', { name: '查看悬浮球截屏' })
    expect(invoke).toHaveBeenCalledWith('read_floating_assist_image_data_url', { path: '/history/saved.jpg' })
    expect(screen.getByAltText('悬浮球截屏缩略图')).toHaveAttribute('src', 'data:image/jpeg;base64,aW1hZ2U=')
    fireEvent.click(thumbnail)
    expect(screen.getAllByRole('img').length).toBeGreaterThan(1)
  })

  it('原文件丢失时明确提示，文字记录仍可查看', async () => {
    vi.mocked(invoke).mockRejectedValue('SCREENSHOT_NOT_FOUND')
    await showScreenshotHistory()
    expect(await screen.findByRole('button', { name: '原截图已丢失' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('屏幕咨询')).toBeInTheDocument()
  })

  it('其他读取错误不误报文件丢失', async () => {
    vi.mocked(invoke).mockRejectedValue('读取截屏失败：Permission denied')
    await showScreenshotHistory()
    expect(await screen.findByRole('button', { name: '悬浮球截屏暂不可用' })).toBeInTheDocument()
    expect(screen.queryByText('原截图已丢失')).not.toBeInTheDocument()
  })
})
