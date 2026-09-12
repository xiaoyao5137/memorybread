import { useConsultationReadiness } from '../hooks/useConsultationReadiness'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import SystemFloatingAssist from '../components/SystemFloatingAssist'
import { useAppStore } from '../store/useAppStore'
import { runGatewayRagQueryStream, runRagQueryStream, saveConsultationHistory } from '../hooks/useApi'
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

vi.mock('../hooks/useConsultationReadiness', () => ({
  useConsultationReadiness: vi.fn(() => ({
    status: { ready: true, message: '已就绪' }, ready: true, loading: false,
    refresh: vi.fn().mockResolvedValue(true),
  })),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn().mockResolvedValue(() => {}),
}))

vi.mock('../hooks/useApi', () => ({
  RAG_REFERENCE_LIMIT: 5,
  saveConsultationHistory: vi.fn().mockResolvedValue(141),
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
    answer: '自动识别任务的咨询结果',
    contexts: [],
    output_truncated: false,
  } as any)
  mockedRunGatewayRagQueryStream.mockReset()
  mockedRunGatewayRagQueryStream.mockResolvedValue({
    answer: '云端咨询结果',
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
  it('OCR 超时后点击重试会重新识别原附件并保留原问题，不改为屏幕识别', async () => {
    vi.useRealTimers()
    let attempts = 0
    mockedInvoke.mockImplementation(async (command) => {
      if (command === 'save_consultation_attachment') {
        if (++attempts === 1) throw new Error('图片已保存，但文字识别失败：屏幕文字识别等待超时，请稍后重试')
        return { path: '/local/retry.png', ocr_text: '生日提醒，请送贺卡' } as any
      }
      return undefined
    })
    const { container } = render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    const textarea = await screen.findByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(['a'], 'a.png', { type: 'image/png' })] } })
    await screen.findByRole('button', { name: '查看图片 图片1' })
    fireEvent.change(textarea, { target: { value: '他表达的是什么含义？ @图片1' } })
    fireEvent.submit(textarea.closest('form')!)
    await screen.findByText(/图片已保存，但文字识别失败/)
    expect(mockedRunRagQueryStream).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(mockedRunRagQueryStream).toHaveBeenCalledOnce())
    const metadata = mockedRunRagQueryStream.mock.calls[0][4] as any
    expect(metadata.manual_instruction).toBe('他表达的是什么含义？ @图片1')
    expect(metadata.ocr_text).toContain('生日提醒，请送贺卡')
    expect(attempts).toBe(2)
    expect(mockedInvoke.mock.calls.some(([command]) => command === 'capture_screen_ocr_for_floating_assist')).toBe(false)
  })

  it.each(['manual', 'screen'] as const)('%s 咨询成功后清空输入区图片，下一次咨询不携带旧附件', async (mode) => {
    vi.useRealTimers()
    const settings = readInteractionSettings()
    window.localStorage.setItem(INTERACTION_SETTINGS_KEY, JSON.stringify({
      ...settings,
      floatingBall: { ...settings.floatingBall, doubleClick: 'recognize_screen_task' },
    }))
    mockedInvoke.mockImplementation(async (command) => {
      if (command === 'save_consultation_attachment') return { path: '/local/a.png', ocr_text: '仅属于第一轮的图片文字' } as any
      if (command === 'capture_screen_ocr_for_floating_assist') return taskOcrResult as any
      return undefined
    })
    const { container } = render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    const textarea = await screen.findByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(['a'], 'a.png', { type: 'image/png' })] } })
    await screen.findByRole('button', { name: '查看图片 图片1' })
    if (mode === 'manual') {
      fireEvent.change(textarea, { target: { value: '解释 @图片1' } })
      fireEvent.submit(textarea.closest('form')!)
    } else {
      fireEvent.doubleClick(assistButton())
    }
    await waitFor(() => expect(mockedRunRagQueryStream).toHaveBeenCalledOnce(), { timeout: 3000 })
    expect((mockedRunRagQueryStream.mock.calls[0][4] as any).attachments).toHaveLength(1)
    await waitFor(() => expect(screen.queryByRole('button', { name: '查看图片 图片1' })).not.toBeInTheDocument())
    fireEvent.change(textarea, { target: { value: '这是一个新的问题' } })
    fireEvent.submit(textarea.closest('form')!)
    await waitFor(() => expect(mockedRunRagQueryStream).toHaveBeenCalledTimes(2))
    expect((mockedRunRagQueryStream.mock.calls[1][4] as any).attachments).toEqual([])
    expect(mockedRunRagQueryStream.mock.calls[1][2]).not.toContain('仅属于第一轮的图片文字')
  })

  it('图片文字缺失时保留问题并阻止无依据生成', async () => {
    vi.useRealTimers()
    mockedInvoke.mockImplementation(async (command) => {
      if (command === 'save_consultation_attachment') return { path: '/local/a.png', ocr_text: '' } as any
      return undefined
    })
    const { container } = render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    const textarea = await screen.findByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(['a'], 'a.png', { type: 'image/png' })] } })
    await screen.findByRole('button', { name: '查看图片 图片1' })
    fireEvent.change(textarea, { target: { value: '他表达了什么？' } })
    fireEvent.submit(textarea.closest('form')!)
    await screen.findByText(/部分图片未识别到文字/)
    expect(mockedRunRagQueryStream).not.toHaveBeenCalled()
    expect(textarea).toHaveValue('他表达了什么？')
  })

  it('上传多图后展示缩略图，先保存历史再提问；生成失败仍保留问题和附件', async () => {
    vi.useRealTimers()
    vi.mocked(saveConsultationHistory).mockClear()
    mockedInvoke.mockImplementation(async (command, args: any) => {
      if (command === 'save_consultation_attachment') return { path: `/local/${args.dataUrl.endsWith('YQ==') ? 'a' : 'b'}.png`, ocr_text: '图片内的测试文字' } as any
      return undefined
    })
    mockedRunRagQueryStream.mockRejectedValue(new Error('模拟生成失败'))
    const { container } = render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    const textarea = await screen.findByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(['a'], 'a.png', { type: 'image/png' }), new File(['b'], 'b.png', { type: 'image/png' })] } })
    await screen.findByRole('button', { name: '查看图片 图片2' })
    fireEvent.click(screen.getByRole('button', { name: '查看图片 图片2' }))
    expect(screen.getByRole('dialog', { name: '图片原图预览' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(textarea).toBeVisible()
    fireEvent.change(textarea, { target: { value: '比较 @' } })
    fireEvent.click(screen.getByRole('option', { name: '@图片2' }))
    fireEvent.submit(textarea.closest('form')!)
    await waitFor(() => expect(mockedRunRagQueryStream).toHaveBeenCalledOnce())
    const payload = mockedRunRagQueryStream.mock.calls[0][4] as any
    expect(payload.attachments.map((item: any) => item.name)).toEqual(['图片1', '图片2'])
    expect(mockedRunRagQueryStream.mock.calls[0][2]).toContain('@图片2')
    expect(payload.attachments).toHaveLength(2)
    expect(payload.history_id).toBe(141)
    expect(JSON.stringify(payload)).not.toContain('base64')
    await waitFor(() => expect(vi.mocked(saveConsultationHistory).mock.calls).toHaveLength(2))
    expect(vi.mocked(saveConsultationHistory).mock.calls[1][5]).toBe(141)
    expect(screen.getByRole('button', { name: '查看图片 图片1' })).toBeInTheDocument()
    expect(textarea).toHaveValue('比较 @图片2 ')
    expect(screen.getByText('模拟生成失败')).toBeInTheDocument()
  })

  it('咨询正文、表头和单元格的外链交给浏览器打开，页内锚点保留', async () => {
    const answer = [
      '[正文链接](https://example.com/article) 与 [页内位置](#details)',
      '',
      '| [表头链接](https://example.com/header) | 说明 |',
      '| --- | --- |',
      '| [**表格链接**](http://example.com/cell) | 内容 |',
    ].join('\n')
    mockedRunRagQueryStream.mockResolvedValue({ answer, contexts: [] } as any)
    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => vi.advanceTimersByTime(220))
    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '查询相关链接' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })

    for (const [name, href] of [
      ['正文链接', 'https://example.com/article'],
      ['表头链接', 'https://example.com/header'],
      ['表格链接', 'http://example.com/cell'],
    ]) {
      const link = screen.getByRole('link', { name })
      expect(link).toHaveAttribute('href', href)
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    }
    expect(screen.getByRole('link', { name: '页内位置' })).not.toHaveAttribute('target')
  })

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
      `/?view=floating-assist&debugPhase=done&debugAnswer=${encodeURIComponent('已生成的咨询结果')}`,
    )
    render(<SystemFloatingAssist />)

    const button = assistButton()
    expect(button).toHaveClass('system-floating-assist__ball--done')

    fireEvent.click(button)
    act(() => {
      vi.advanceTimersByTime(220)
    })

    expect(button).toHaveClass('system-floating-assist__ball--idle')
    expect(screen.getByText('咨询结果')).toBeInTheDocument()
    expect(screen.getByText('已生成的咨询结果')).toBeInTheDocument()
  })

  it('初始化失败时保留问题、拦截请求并能打开修复入口', async () => {
    const readiness = vi.mocked(useConsultationReadiness)
    const original = readiness.getMockImplementation()!
    readiness.mockImplementation(() => ({
      status: { ready: false, runtime: false, llm: false, embedding: false, message: '引擎下载失败', action: 'initialization' },
      ready: false, loading: false, refresh: vi.fn().mockResolvedValue(false),
    }))
    try {
      render(<SystemFloatingAssist />)
      fireEvent.click(assistButton())
      act(() => { vi.advanceTimersByTime(220) })
      const input = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
      fireEvent.change(input, { target: { value: '保留这个问题' } })
      expect(screen.getByRole('button', { name: '发送手工咨询' })).toBeDisabled()
      fireEvent.submit(input.closest('form')!)
      await act(async () => flushMicrotasks())
      expect(input).toHaveValue('保留这个问题')
      expect(mockedRunRagQueryStream).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: '继续初始化 / 修复' }))
      await act(async () => flushMicrotasks())
      expect(mockedInvoke).toHaveBeenCalledWith('show_main_panel_from_floating_assist')
    } finally { readiness.mockImplementation(original) }
  })

  it('预热期间保留问题并重新检查，不误导用户重做初始化', async () => {
    const readiness = vi.mocked(useConsultationReadiness)
    const original = readiness.getMockImplementation()!
    const refresh = vi.fn().mockResolvedValue(false)
    readiness.mockImplementation(() => ({
      status: { ready: false, runtime: true, llm: true, embedding: true,
        message: '正在准备咨询能力', action: 'retry', error_code: 'LOCAL_AI_WARMING_UP' },
      ready: false, loading: false, refresh,
    }))
    try {
      render(<SystemFloatingAssist />)
      fireEvent.click(assistButton())
      act(() => { vi.advanceTimersByTime(220) })
      const input = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
      fireEvent.change(input, { target: { value: '预热后继续回答' } })
      expect(screen.queryByRole('button', { name: '继续初始化 / 修复' })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: '发送手工咨询' })).toBeDisabled()
      fireEvent.click(screen.getByRole('button', { name: '重新检查' }))
      await act(async () => flushMicrotasks())
      expect(refresh).toHaveBeenCalledOnce()
      expect(input).toHaveValue('预热后继续回答')
      expect(mockedRunRagQueryStream).not.toHaveBeenCalled()
    } finally { readiness.mockImplementation(original) }
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
      answer: '已生成的咨询结果',
      contexts: Array.from({ length: 7 }, (_, index) => ({
        capture_id: index + 1,
        doc_key: `${sourceTypes[index]}:${index + 1}`,
        title: `参考资料 ${index + 1}`,
        source_url: 'https://example.com/original',
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
    fireEvent.click(screen.getByText('参考资料 2').closest('button')!)
    expect(mockedInvoke).toHaveBeenCalledWith('open_floating_assist_reference', {
      detail: expect.objectContaining({ type: 'document', sourceUrl: 'https://example.com/original' }),
    })
    expect(screen.getByText('参考资料 3').closest('button')?.firstElementChild).toHaveTextContent('操作')
    expect(screen.getByText('参考资料 4').closest('button')?.firstElementChild).toHaveTextContent('时间线')

    fireEvent.click(screen.getByRole('button', { name: '展开更多（2）' }))
    expect(screen.getByText('参考资料 6')).toBeInTheDocument()
    expect(screen.getByText('参考资料 7')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.queryByText('参考资料 6')).not.toBeInTheDocument()
  })

  it('未被答案标注采用的召回记忆仍然展示，并区分采用状态', async () => {
    mockedRunRagQueryStream.mockResolvedValue({
      answer: 'SMACT 衡量 SM 活跃时间比例，英伟达阈值 80%。',
      contexts: [
        {
          capture_id: 2,
          doc_key: 'document:2',
          title: 'SMACT 指标定义',
          text: '正文 2',
          score: 0.9,
          source: 'document',
          source_type: 'document',
          cited: true,
          recall_index: 2,
        },
        {
          capture_id: 1,
          doc_key: 'document:1',
          title: 'GPU 利用率日报',
          text: '正文 1',
          score: 0.8,
          source: 'document',
          source_type: 'document',
          cited: false,
          recall_index: 1,
        },
      ],
      output_truncated: false,
    } as any)

    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })

    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: 'SMACT文档' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })
    await act(async () => {
      vi.advanceTimersByTime(900)
      await flushMicrotasks()
    })

    expect(screen.getByText('参考资料（2）')).toBeInTheDocument()
    expect(screen.getByText('答案采用 1 条')).toBeInTheDocument()
    const adopted = screen.getByText('SMACT 指标定义').closest('button')!
    const recalledOnly = screen.getByText('GPU 利用率日报').closest('button')!
    expect(adopted).toHaveTextContent('已采用')
    expect(adopted).toHaveTextContent('M2')
    // 模型漏标注时召回证据不得消失，且保留召回序号供用户核对
    expect(recalledOnly).toHaveTextContent('M1')
    expect(recalledOnly).not.toHaveTextContent('已采用')
    expect(recalledOnly).toHaveAttribute('title', '已召回，本次答案未标注采用')
  })

  it('召回记忆全部未被采用时仍然展示并明确标注', async () => {
    mockedRunRagQueryStream.mockResolvedValue({
      answer: '小米体重计通过生物电阻抗测量体脂。',
      contexts: [
        {
          capture_id: 1,
          doc_key: 'document:1',
          title: 'GPU 利用率日报',
          text: '正文 1',
          score: 0.7,
          source: 'document',
          source_type: 'document',
          cited: false,
          recall_index: 1,
        },
      ],
      output_truncated: false,
    } as any)

    render(<SystemFloatingAssist />)
    fireEvent.click(assistButton())
    act(() => {
      vi.advanceTimersByTime(220)
    })

    const textarea = screen.getByPlaceholderText('帮我回顾一下上周项目评审的关键结论')
    fireEvent.change(textarea, { target: { value: '小米体重计的工作原理' } })
    await act(async () => {
      fireEvent.submit(textarea.closest('form')!)
      await flushMicrotasks()
    })
    await act(async () => {
      vi.advanceTimersByTime(900)
      await flushMicrotasks()
    })

    expect(screen.getByText('参考资料（1）')).toBeInTheDocument()
    expect(screen.getByText('答案未采用')).toBeInTheDocument()
    expect(screen.getByText('GPU 利用率日报')).toBeInTheDocument()
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
    // 生成过程中还不能判定采用情况，不得提前给出结论
    expect(screen.queryByText('答案未采用')).not.toBeInTheDocument()
    expect(screen.getByText('咨询结果 · 正在生成').closest('.system-floating-assist__answer'))
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
