import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import SystemFloatingAssist from '../components/SystemFloatingAssist'
import { useAppStore } from '../store/useAppStore'
import { runGatewayRagQueryStream, runRagQueryStream } from '../hooks/useApi'
import {
  FLOATING_ASSIST_AUTO_TASK_KEY,
  FLOATING_ASSIST_ENABLED_KEY,
} from '../utils/floatingAssistAutoTask'
import {
  INTERACTION_SETTINGS_KEY,
  readInteractionSettings,
} from '../utils/interactionSettings'

const cloudMocks = vi.hoisted(() => ({
  fetchBreadcrumbProfile: vi.fn(),
  fetchBillingBalance: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn().mockResolvedValue(() => {}),
}))

vi.mock('../hooks/useApi', () => ({
  RAG_REFERENCE_LIMIT: 5,
  runGatewayRagQueryStream: vi.fn(),
  runRagQueryStream: vi.fn(),
}))

vi.mock('../utils/authApi', () => ({
  fetchBillingBalance: cloudMocks.fetchBillingBalance,
}))

vi.mock('../utils/breadcrumbApi', () => ({
  BREADCRUMBS_CHANGED_KEY: 'memorybread.breadcrumbs.changed',
  fetchBreadcrumbProfile: cloudMocks.fetchBreadcrumbProfile,
}))

const mockedInvoke = vi.mocked(invoke)
const mockedListen = vi.mocked(listen)
const mockedRunGatewayRagQueryStream = vi.mocked(runGatewayRagQueryStream)
const mockedRunRagQueryStream = vi.mocked(runRagQueryStream)
const assistButton = () => screen.getByRole('button', { name: /^单击：/ })
const AUTO_TASK_SCAN_INITIAL_DELAY_MS = 10_000
const AUTO_TASK_SCAN_INTERVAL_MS = 120_000
const taskOcrResult = {
  text: '飞书\n老板：帮我修复登录验证码异常，明天下午前给结论',
  confidence: 0.92,
  screenshot_path: '/tmp/floating-task.jpg',
  width: 1440,
  height: 900,
  screenshot_source: 'window',
  app_bundle_id: 'com.bytedance.lark',
  app_name: '飞书',
  window_title: '项目群',
}
const anotherTaskOcrResult = {
  text: '飞书\n老板：帮我整理项目风险清单，本周内给一版',
  confidence: 0.93,
  screenshot_path: '/tmp/floating-task-another.jpg',
  width: 1440,
  height: 900,
  screenshot_source: 'window',
  app_bundle_id: 'com.bytedance.lark',
  app_name: '飞书',
  window_title: '项目群',
}
const documentTaskOcrResult = {
  text: 'Chrome\ndocs.example.com/d/home/example\n所有改动已自动保存\nTODO\n- [ ] 修复登录验证码异常\n截止：明天下午前',
  confidence: 0.91,
  screenshot_path: '/tmp/floating-document-task.jpg',
  width: 1440,
  height: 900,
  screenshot_source: 'window',
  app_bundle_id: 'com.google.Chrome',
  app_name: 'Chrome',
  window_title: '项目文档',
}
const flushMicrotasks = async () => {
  await Promise.resolve()
  await Promise.resolve()
}
const captureOcrCalls = () =>
  mockedInvoke.mock.calls.filter(([command]) => command === 'capture_screen_ocr_for_floating_assist')
const completeNextAutoScan = async (scanDelayMs: number) => {
  await act(async () => {
    vi.advanceTimersByTime(scanDelayMs)
    await flushMicrotasks()
  })
  await act(async () => {
    vi.advanceTimersByTime(50)
    await flushMicrotasks()
  })
  await act(async () => {
    vi.advanceTimersByTime(900)
    await flushMicrotasks()
  })
  await act(async () => {
    vi.advanceTimersByTime(50)
    await flushMicrotasks()
  })
  await act(async () => {
    vi.advanceTimersByTime(50)
    await flushMicrotasks()
  })
  await act(async () => {
    vi.advanceTimersByTime(100)
    await flushMicrotasks()
  })
}
const returnDoneStateToIdle = async () => {
  await act(async () => {
    vi.advanceTimersByTime(5 * 60 * 1000)
    await flushMicrotasks()
  })
}
const closeDoneSurfaceAndReturnIdle = async () => {
  await act(async () => {
    fireEvent.click(assistButton())
    await flushMicrotasks()
  })
  await returnDoneStateToIdle()
}
const installMemoryLocalStorage = () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      values.set(key, String(value))
    }),
    removeItem: vi.fn((key: string) => {
      values.delete(key)
    }),
    clear: vi.fn(() => {
      values.clear()
    }),
  }
  Object.defineProperty(window, 'localStorage', {
    value: storage,
    configurable: true,
  })
  Object.defineProperty(globalThis, 'localStorage', {
    value: storage,
    configurable: true,
  })
  return storage
}

beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: true })
  Object.defineProperty(window, 'requestAnimationFrame', {
    value: (callback: FrameRequestCallback) => {
      callback(Date.now())
      return 1
    },
    configurable: true,
  })
  useAppStore.getState().reset()
  installMemoryLocalStorage()
  window.localStorage.clear()
  mockedInvoke.mockReset()
  mockedInvoke.mockImplementation(async (command: string) => {
    if (command === 'capture_screen_ocr_for_floating_assist') return taskOcrResult
    if (command === 'read_floating_assist_image_data_url') return ''
    return undefined
  })
  mockedListen.mockReset()
  mockedListen.mockResolvedValue(() => {})
  mockedRunRagQueryStream.mockReset()
  mockedRunRagQueryStream.mockResolvedValue({
    answer: '自动识别任务的咨询输出',
    contexts: [],
    output_truncated: false,
  } as any)
  mockedRunGatewayRagQueryStream.mockReset()
  mockedRunGatewayRagQueryStream.mockResolvedValue({
    answer: '云端咨询输出',
    contexts: [],
    output_truncated: false,
  } as any)
  cloudMocks.fetchBreadcrumbProfile.mockReset()
  cloudMocks.fetchBreadcrumbProfile.mockResolvedValue({ breadcrumbs: [], equipped: {} })
  cloudMocks.fetchBillingBalance.mockReset()
  cloudMocks.fetchBillingBalance.mockResolvedValue(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  window.history.pushState({}, '', '/')
})

describe('SystemFloatingAssist', () => {
  it('只在开发模式显示悬浮球开发标识', () => {
    const { rerender } = render(<SystemFloatingAssist developmentMode />)

    expect(screen.getByText('开发模式')).toBeInTheDocument()
    expect(assistButton()).toHaveClass('system-floating-assist__ball--development')

    rerender(<SystemFloatingAssist developmentMode={false} />)

    expect(screen.queryByText('开发模式')).not.toBeInTheDocument()
    expect(assistButton()).not.toHaveClass('system-floating-assist__ball--development')
  })

  it('离线启动时直接显示本地悬浮助手并读取本机面包屑', () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_offline_floating_token',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
      user: {
        id: 'offline-floating-user',
        username: '离线悬浮用户',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: false })

    render(<SystemFloatingAssist />)

    expect(assistButton()).toBeInTheDocument()
    expect(cloudMocks.fetchBillingBalance).not.toHaveBeenCalled()
    expect(cloudMocks.fetchBreadcrumbProfile).toHaveBeenCalled()
  })

  it('闲置态渲染面包人角色且不再使用旧图片层', () => {
    const { container } = render(<SystemFloatingAssist />)
    const button = assistButton()

    expect(button).toHaveClass('system-floating-assist__ball--idle')
    expect(container.querySelector('.system-floating-assist__bread-person')).toBeInTheDocument()
    expect(container.querySelector('.system-floating-assist__bread-body')).toBeInTheDocument()
    expect(container.querySelector('.system-floating-assist__mascot-img')).not.toBeInTheDocument()
    expect(container.querySelector('.system-floating-assist__idle-snack')).not.toBeInTheDocument()
    expect(container.querySelector('.system-floating-assist__idle-eye')).not.toBeInTheDocument()
    expect(container.querySelector('.system-floating-assist__idle-shadow')).not.toBeInTheDocument()
    expect(container.querySelectorAll('.system-floating-assist__bread-cheek')).toHaveLength(2)
  })

  it('鼠标进入和离开时切换悬停动画状态', () => {
    render(<SystemFloatingAssist />)
    const button = assistButton()

    fireEvent.pointerEnter(button)
    expect(button).toHaveClass('system-floating-assist__ball--native-hover')

    fireEvent.pointerLeave(button)
    expect(button).not.toHaveClass('system-floating-assist__ball--native-hover')
  })

  it('原生追踪区域进入和离开时切换悬停动画状态', async () => {
    render(<SystemFloatingAssist />)
    await act(async () => flushMicrotasks())
    const registration = mockedListen.mock.calls.find(
      ([eventName]) => eventName === 'floating-assist-native-hover-changed',
    )
    expect(registration).toBeDefined()

    const button = assistButton()
    act(() => registration?.[1]({ payload: true } as any))
    expect(button).toHaveClass('system-floating-assist__ball--native-hover')

    act(() => registration?.[1]({ payload: false } as any))
    expect(button).not.toHaveClass('system-floating-assist__ball--native-hover')
  })

  it('闲置动画按周期播放并在间隔期停止合成', () => {
    render(<SystemFloatingAssist />)
    const button = assistButton()

    expect(button).toHaveClass('system-floating-assist__ball--ambient-active')
    act(() => {
      vi.advanceTimersByTime(2_200)
    })
    expect(button).not.toHaveClass('system-floating-assist__ball--ambient-active')

    act(() => {
      vi.advanceTimersByTime(5_800)
    })
    expect(button).toHaveClass('system-floating-assist__ball--ambient-active')
  })

  it('完成态 5 分钟后自动切回闲置态', () => {
    window.history.pushState({}, '', '/?view=floating-assist&debugPhase=done')
    render(<SystemFloatingAssist />)

    expect(assistButton()).toHaveClass('system-floating-assist__ball--done')

    act(() => {
      vi.advanceTimersByTime(5 * 60 * 1000)
    })

    expect(assistButton()).toHaveClass('system-floating-assist__ball--idle')
  })

  it('完成态点击展开后切回闲置态，并保留已生成输出', () => {
    window.history.pushState(
      {},
      '',
      '/?view=floating-assist&debugPhase=done&debugAnswer=%E5%B7%B2%E7%94%9F%E6%88%90%E7%9A%84%E5%92%A8%E8%AF%A2%E8%BE%93%E5%87%BA',
    )
    render(<SystemFloatingAssist />)

    const button = assistButton()
    expect(button).toHaveClass('system-floating-assist__ball--done')

    fireEvent.click(button)
    act(() => {
      vi.advanceTimersByTime(220)
    })

    expect(button).toHaveClass('system-floating-assist__ball--idle')
    expect(screen.getByText('咨询输出')).toBeInTheDocument()
    expect(screen.getByText('已生成的咨询输出')).toBeInTheDocument()
  })

  it('默认单击打开悬浮球咨询框，双击打开主面板', async () => {
    render(<SystemFloatingAssist />)
    const button = assistButton()

    expect(button).toHaveAttribute('title', '单击：打开悬浮球咨询框；双击：打开主面板')
    expect(button).not.toHaveAttribute('title', expect.stringContaining('当屏任务识别'))

    fireEvent.click(button)
    act(() => {
      vi.advanceTimersByTime(220)
    })
    expect(screen.getByText('记忆咨询')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')).toBeInTheDocument()

    fireEvent.doubleClick(button)
    await act(async () => flushMicrotasks())
    expect(mockedInvoke).toHaveBeenCalledWith('show_main_panel_from_floating_assist')
    expect(captureOcrCalls()).toHaveLength(0)
  })

  it('展开后窗口失焦或点击外层区域时保持展开', () => {
    const { container } = render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })
    expect(screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')).toBeInTheDocument()

    mockedInvoke.mockClear()
    act(() => {
      window.dispatchEvent(new Event('blur'))
    })
    fireEvent.pointerDown(container.querySelector('.system-floating-assist')!)

    expect(screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')).toBeInTheDocument()
    expect(mockedInvoke.mock.calls).not.toContainEqual([
      'set_floating_assist_size',
      { width: 82, height: 82 },
    ])
  })

  it('按配置将单击设为无事件、双击设为当屏任务识别', async () => {
    window.localStorage.setItem(INTERACTION_SETTINGS_KEY, JSON.stringify({
      ...readInteractionSettings(),
      floatingBall: {
        singleClick: 'none',
        doubleClick: 'recognize_screen_task',
      },
    }))
    render(<SystemFloatingAssist />)
    const button = assistButton()

    expect(button).toHaveAttribute('title', '单击：无事件；双击：触发当屏任务识别')

    fireEvent.click(button)
    act(() => {
      vi.advanceTimersByTime(220)
    })
    expect(screen.queryByPlaceholderText('帮我回顾一下上周项目评审的关键结论')).not.toBeInTheDocument()

    fireEvent.doubleClick(button)
    await act(async () => {
      await flushMicrotasks()
    })
    await act(async () => {
      vi.advanceTimersByTime(900)
      await flushMicrotasks()
    })
    expect(captureOcrCalls()).toHaveLength(1)
    expect(captureOcrCalls()[0]).toEqual([
      'capture_screen_ocr_for_floating_assist',
      { hideFloatingWindow: false },
    ])
  })

  it('云端屏幕咨询失败时自动切换本地能力并交付答案', async () => {
    const balance = {
      available: '100.0000',
      reserved: '0.0000',
      currency: 'CREDIT',
      as_of: '2026-08-29T00:00:00Z',
    }
    useAppStore.getState().setAuthSession({
      access_token: 'floating-cloud-token',
      expires_at: '2099-01-01T00:00:00Z',
      user: {
        id: '00000000-0000-0000-0000-000000000001',
        nickname: '测试用户',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-01-01T00:00:00Z',
      },
    })
    useAppStore.getState().setCloudBalance(balance)
    useAppStore.getState().setCreationModelConfig('mbcd-plus-v1', { enabled: true })
    cloudMocks.fetchBillingBalance.mockResolvedValue(balance)
    mockedRunGatewayRagQueryStream.mockRejectedValue(new TypeError('Failed to fetch'))
    mockedRunRagQueryStream.mockResolvedValue({
      answer: '本地备用题解',
      contexts: [],
      model: 'mbcd-std-v1',
    } as any)
    window.localStorage.setItem(INTERACTION_SETTINGS_KEY, JSON.stringify({
      ...readInteractionSettings(),
      floatingBall: {
        singleClick: 'none',
        doubleClick: 'recognize_screen_task',
      },
    }))

    render(<SystemFloatingAssist />)
    fireEvent.doubleClick(assistButton())
    await act(async () => {
      await flushMicrotasks()
      vi.advanceTimersByTime(900)
      await flushMicrotasks()
    })

    expect(mockedRunGatewayRagQueryStream).toHaveBeenCalledTimes(1)
    expect(mockedRunRagQueryStream).toHaveBeenCalledTimes(1)
    expect(mockedRunRagQueryStream.mock.calls[0]?.[5]).toBe(false)
    expect(screen.getByText('本地备用题解')).toBeInTheDocument()
  })

  it('默认显示 5 条参考资料并支持展开和收起更多资料', async () => {
    const sourceTypes = ['bake_knowledge', 'document', 'operation', 'knowledge', 'document', 'operation', 'bake_knowledge']
    mockedRunRagQueryStream.mockResolvedValue({
      answer: '已生成的咨询输出',
      contexts: Array.from({ length: 7 }, (_, index) => ({
        capture_id: index + 1,
        doc_key: `${sourceTypes[index]}:${index + 1}`,
        title: `参考资料 ${index + 1}`,
        text: `参考内容 ${index + 1}`,
        score: 1 - index / 10,
        source: sourceTypes[index],
        source_type: sourceTypes[index],
      })),
      output_truncated: false,
    } as any)

    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })

    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '分析当前资料' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })
    await act(async () => {
      vi.advanceTimersByTime(900)
      await flushMicrotasks()
    })
    act(() => {
      vi.advanceTimersByTime(28)
    })

    expect(screen.getByText('参考资料（7）')).toBeInTheDocument()
    expect(screen.getByText('参考资料 5')).toBeInTheDocument()
    expect(screen.queryByText('参考资料 6')).not.toBeInTheDocument()
    expect(screen.getByText('参考资料 1').closest('button')?.firstElementChild).toHaveTextContent('知识')
    expect(screen.getByText('参考资料 2').closest('button')?.firstElementChild).toHaveTextContent('文档')
    expect(screen.getByText('参考资料 3').closest('button')?.firstElementChild).toHaveTextContent('操作')
    expect(screen.getByText('参考资料 4').closest('button')?.firstElementChild).toHaveTextContent('时间线')

    fireEvent.click(screen.getByRole('button', { name: '展开更多（2）' }))
    expect(screen.getByText('参考资料 6')).toBeInTheDocument()
    expect(screen.getByText('参考资料 7')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.queryByText('参考资料 6')).not.toBeInTheDocument()
  })

  it('流式生成时先展示参考资料和部分答案，完成后展示推理耗时', async () => {
    let finishStream: ((value: any) => void) | null = null
    let emitAnswerDelta: (() => void) | null = null
    const streamedReference = {
      capture_id: 1,
      doc_key: 'document:1',
      title: '提前召回的资料',
      text: '参考内容',
      score: 0.9,
      source: 'document',
      source_type: 'document',
    }
    mockedRunRagQueryStream.mockImplementation((...args: any[]) => {
      const callbacks = args[7]
      callbacks.onStatus?.({ stage: 'retrieving', message: '正在召回相关资料', progress: 42 })
      callbacks.onReferences?.([streamedReference])
      callbacks.onStatus?.({ stage: 'answering', message: '正在生成答案', progress: 58 })
      emitAnswerDelta = () => callbacks.onDelta?.('这是部分答案', '这是部分答案')
      return new Promise(resolve => {
        finishStream = resolve
      }) as any
    })

    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })
    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '分析这份资料' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })

    expect(screen.getByText('提前召回的资料')).toBeInTheDocument()
    expect(screen.getByText('咨询输出 · 正在生成').closest('.system-floating-assist__answer'))
      .toHaveClass('system-floating-assist__answer--streaming')
    expect(mockedInvoke.mock.calls).toContainEqual([
      'set_floating_assist_size',
      { width: 392, height: 590 },
    ])

    act(() => {
      emitAnswerDelta?.()
    })

    expect(screen.getByText('这是部分答案')).toBeInTheDocument()
    expect(screen.getAllByText(/正在生成答案/)).toHaveLength(2)
    expect(screen.getByText('这是部分答案').closest('.system-floating-assist__answer'))
      .toHaveClass('system-floating-assist__answer--streaming')

    await act(async () => {
      finishStream?.({
        answer: '这是完整答案',
        contexts: [streamedReference],
        model: 'test-model',
        inference_elapsed_ms: 1234,
      })
      await flushMicrotasks()
    })

    expect(screen.getByText('这是完整答案')).toBeInTheDocument()
    expect(screen.getByText('推理耗时 1.2 秒')).toBeInTheDocument()
  })

  it('流式召回后展开的参考资料在推理完成时保持展开', async () => {
    let finishStream: ((value: any) => void) | null = null
    const streamedReferences = Array.from({ length: 6 }, (_, index) => ({
      capture_id: index + 1,
      doc_key: `document:${index + 1}`,
      title: `流式资料 ${index + 1}`,
      text: `参考内容 ${index + 1}`,
      score: 1 - index / 10,
      source: 'document',
      source_type: 'document',
    }))
    mockedRunRagQueryStream.mockImplementation((...args: any[]) => {
      const callbacks = args[7]
      callbacks.onStatus?.({ stage: 'retrieving', message: '正在召回相关资料', progress: 42 })
      callbacks.onReferences?.(streamedReferences)
      callbacks.onStatus?.({ stage: 'answering', message: '正在生成答案', progress: 58 })
      return new Promise(resolve => {
        finishStream = resolve
      }) as any
    })

    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })
    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '分析这份资料' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })

    expect(screen.getByText('流式资料 5')).toBeInTheDocument()
    expect(screen.queryByText('流式资料 6')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '展开更多（1）' }))
    expect(screen.getByText('流式资料 6')).toBeInTheDocument()

    await act(async () => {
      finishStream?.({
        answer: '这是完整答案',
        contexts: streamedReferences.map(item => ({ ...item })),
        model: 'test-model',
        inference_elapsed_ms: 1234,
      })
      await flushMicrotasks()
    })

    expect(screen.getByText('这是完整答案')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '收起' })).toBeInTheDocument()
    expect(screen.getByText('流式资料 6')).toBeInTheDocument()
  })

  it('阶段状态等待期间持续推进进度，不停在服务端建议值', async () => {
    mockedRunRagQueryStream.mockImplementation((...args: any[]) => {
      const callbacks = args[7]
      callbacks.onStatus?.({ stage: 'understanding', message: '正在理解当前问题', progress: 28 })
      return new Promise(() => {}) as any
    })

    const { container } = render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })
    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '分析当前问题' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })

    const progressBar = container.querySelector<HTMLElement>('.system-floating-assist__progress span')
    expect(progressBar?.style.width).toBe('28%')

    act(() => {
      vi.advanceTimersByTime(3_000)
    })

    expect(Number.parseFloat(progressBar?.style.width || '0')).toBeGreaterThan(28)
    expect(screen.getAllByText(/正在理解当前问题/)).toHaveLength(2)
  })

  it('咨询输入框使用 Enter 发送，并保留 Shift+Enter 换行', async () => {
    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })

    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '第一行' } })

    expect(fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter', shiftKey: true })).toBe(true)
    expect(mockedRunRagQueryStream).not.toHaveBeenCalled()

    fireEvent.change(textarea, { target: { value: '第一行\n第二行' } })
    let enterHandled = true
    await act(async () => {
      enterHandled = fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' })
      await flushMicrotasks()
    })
    expect(enterHandled).toBe(false)
    await act(async () => {
      vi.advanceTimersByTime(900)
      await flushMicrotasks()
    })

    expect(mockedRunRagQueryStream).toHaveBeenCalledTimes(1)
    expect(mockedRunRagQueryStream.mock.calls[0]?.[2]).toContain('第一行\n第二行')
  })

  it('咨询输入框使用输入法确认候选词时不发送', () => {
    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })

    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '你好' } })
    fireEvent.compositionStart(textarea)
    fireEvent.compositionEnd(textarea, { data: '你好' })
    const defaultAllowed = fireEvent.keyDown(textarea, {
      key: 'Enter',
      code: 'Enter',
      keyCode: 229,
      isComposing: false,
    })

    expect(defaultAllowed).toBe(true)
    expect(mockedRunRagQueryStream).not.toHaveBeenCalled()
  })

  it('自动识别任务开启后在面包人左上角显示 auto 标识', () => {
    render(<SystemFloatingAssist />)
    expect(screen.queryByText('auto')).not.toBeInTheDocument()

    cleanup()
    window.localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, 'true')
    render(<SystemFloatingAssist />)

    expect(screen.getByText('auto')).toBeInTheDocument()
  })

  it('自动识别命中任务后进入持续回答动画', async () => {
    window.localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, 'true')
    mockedRunRagQueryStream.mockImplementation(() => new Promise(() => {}) as any)

    render(<SystemFloatingAssist />)

    await act(async () => {
      vi.advanceTimersByTime(AUTO_TASK_SCAN_INITIAL_DELAY_MS)
      await flushMicrotasks()
    })

    expect(captureOcrCalls()).toHaveLength(1)
    expect(assistButton()).toHaveClass('system-floating-assist__ball--answering')
  })

  it('自动识别只把疑似任务片段发送给 RAG', async () => {
    window.localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, 'true')
    mockedInvoke.mockImplementation(async (command: string) => {
      if (command === 'capture_screen_ocr_for_floating_assist') {
        return {
          ...taskOcrResult,
          text: '飞书\n闲聊：这个账号密码稍后私发\n老板：帮我修复登录验证码异常，明天下午前给结论',
        }
      }
      if (command === 'read_floating_assist_image_data_url') return ''
      return undefined
    })

    render(<SystemFloatingAssist />)

    await completeNextAutoScan(AUTO_TASK_SCAN_INITIAL_DELAY_MS)

    const sentQuery = mockedRunRagQueryStream.mock.calls[0]?.[2] as string
    expect(sentQuery).toContain('修复登录验证码异常')
    expect(sentQuery).not.toContain('账号密码')
  })

  it('自动识别忽略非 IM 文档页任务', async () => {
    window.localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, 'true')
    mockedInvoke.mockImplementation(async (command: string) => {
      if (command === 'capture_screen_ocr_for_floating_assist') return documentTaskOcrResult
      if (command === 'read_floating_assist_image_data_url') return ''
      return undefined
    })

    render(<SystemFloatingAssist />)

    await act(async () => {
      vi.advanceTimersByTime(AUTO_TASK_SCAN_INITIAL_DELAY_MS)
      await flushMicrotasks()
    })

    expect(captureOcrCalls()).toHaveLength(1)
    expect(mockedRunRagQueryStream).not.toHaveBeenCalled()
    expect(screen.queryByText('发现可能任务')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '咨询' })).not.toBeInTheDocument()
  })

  it('手动任务执行中时跳过自动识别的新任务', async () => {
    window.localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, 'true')
    mockedRunRagQueryStream.mockImplementation(() => new Promise(() => {}) as any)
    render(<SystemFloatingAssist />)

    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })

    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '帮我写周报' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })

    expect(assistButton()).toHaveClass('system-floating-assist__ball--answering')

    await act(async () => {
      vi.advanceTimersByTime(AUTO_TASK_SCAN_INITIAL_DELAY_MS)
      await flushMicrotasks()
    })

    expect(captureOcrCalls()).toHaveLength(0)
  })

  it('自动识别不会因为中间出现其他任务而重复生成同一个任务', async () => {
    window.localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, 'true')
    const ocrResults = [taskOcrResult, anotherTaskOcrResult, taskOcrResult]
    let captureIndex = 0
    mockedInvoke.mockImplementation(async (command: string) => {
      if (command === 'capture_screen_ocr_for_floating_assist') {
        return ocrResults[captureIndex++] ?? taskOcrResult
      }
      if (command === 'read_floating_assist_image_data_url') return ''
      return undefined
    })

    render(<SystemFloatingAssist />)

    await completeNextAutoScan(AUTO_TASK_SCAN_INITIAL_DELAY_MS)
    expect(mockedRunRagQueryStream).toHaveBeenCalledTimes(1)

    await closeDoneSurfaceAndReturnIdle()
    await completeNextAutoScan(AUTO_TASK_SCAN_INTERVAL_MS)
    expect(mockedRunRagQueryStream).toHaveBeenCalledTimes(2)

    await closeDoneSurfaceAndReturnIdle()
    await act(async () => {
      vi.advanceTimersByTime(AUTO_TASK_SCAN_INTERVAL_MS)
      await flushMicrotasks()
    })

    expect(captureOcrCalls()).toHaveLength(3)
    expect(mockedRunRagQueryStream).toHaveBeenCalledTimes(2)
  })
})
