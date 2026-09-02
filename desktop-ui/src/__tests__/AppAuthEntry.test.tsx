import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from '../App'
import { useAppStore } from '../store/useAppStore'

const initializationMocks = vi.hoisted(() => ({
  fetchInitializationStatus: vi.fn(),
  fetchRuntimeReadiness: vi.fn(),
}))

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn(async () => vi.fn()),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(async () => undefined),
}))

vi.mock('../components/RagPanel.v2', () => ({
  default: () => <section data-testid="rag-panel" />,
}))

vi.mock('../components/CreationPanel', () => ({
  default: () => <section data-testid="creation-panel" />,
}))

vi.mock('../components/BakePanel', () => ({
  default: () => <section data-testid="bake-panel" />,
}))

vi.mock('../components/RepositoryPanel', () => ({
  default: () => <section data-testid="repository-panel" />,
}))

vi.mock('../components/SystemFloatingAssist', () => ({
  default: () => <section data-testid="floating-assist" />,
}))

vi.mock('../utils/initialization', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/initialization')>()),
  fetchInitializationStatus: initializationMocks.fetchInitializationStatus,
  fetchRuntimeReadiness: initializationMocks.fetchRuntimeReadiness,
}))

const initializationStatus = (state: 'not_started' | 'completed') => ({
  schema_version: 'initialization.v1',
  mode: 'normal' as const,
  state,
  progress: state === 'completed' ? 100 : 0,
  current_stage: state === 'completed' ? 'feature_smoke_tests' : 'preflight',
  message: state === 'completed' ? '初始化完成' : '等待初始化',
  stages: [],
  quality_gate: { passed: state === 'completed', checks: [] },
  smoke_tests: [],
  can_retry: false,
  can_report: false,
  test_mode_enabled: false,
})

beforeEach(() => {
  window.history.replaceState({}, '', '/')
  Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: true })
  useAppStore.getState().reset()
  useAppStore.getState().setHasCompletedSetup(true)
  useAppStore.getState().clearAuthSession()
  initializationMocks.fetchInitializationStatus.mockResolvedValue(initializationStatus('completed'))
  initializationMocks.fetchRuntimeReadiness.mockResolvedValue(true)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })))
})

describe('App auth entry', () => {
  it('全新安装没有新版质检完成标记时显示强制初始化门禁', async () => {
    useAppStore.setState({ hasCompletedSetup: false, setupSkipped: false })
    initializationMocks.fetchInitializationStatus.mockResolvedValue(initializationStatus('not_started'))

    render(<App />)
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByText('烤面包')).toBeInTheDocument()
    expect(screen.queryByText(/跳过/)).not.toBeInTheDocument()
  })

  it('已完成初始化的启动只显示轻量 Loading，等待 sidecar 核验通过', async () => {
    let resolveStatus: ((status: ReturnType<typeof initializationStatus>) => void) | undefined
    initializationMocks.fetchInitializationStatus.mockImplementation(() => new Promise(resolve => {
      resolveStatus = resolve
    }))

    render(<App />)

    expect(screen.getByTestId('startup-loading')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '记忆面包正在启动' })).toBeInTheDocument()
    expect(screen.getByText('烘焙中....')).toBeInTheDocument()
    expect(document.querySelector('.startup-loading__icon')).toHaveAttribute(
      'src',
      '/brand/memorybread-bread-mark.png',
    )
    expect(screen.queryByTestId('initialization-gate')).not.toBeInTheDocument()
    expect(screen.queryByTestId('rag-panel')).not.toBeInTheDocument()

    await act(async () => {
      resolveStatus?.(initializationStatus('completed'))
    })

    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()
    expect(initializationMocks.fetchInitializationStatus).toHaveBeenCalled()
  })

  it('初始化已完成且本地服务可用时进入主界面', async () => {
    initializationMocks.fetchRuntimeReadiness.mockResolvedValue(true)

    render(<App />)

    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()
    expect(initializationMocks.fetchRuntimeReadiness).toHaveBeenCalled()
    expect(screen.queryByTestId('startup-loading')).not.toBeInTheDocument()
  })

  it('已完成初始化的后台核验发现能力未就绪时回到 Loading 而非初始化页', async () => {
    let resolveStatus: ((status: ReturnType<typeof initializationStatus>) => void) | undefined
    initializationMocks.fetchInitializationStatus.mockImplementation(() => new Promise(resolve => {
      resolveStatus = resolve
    }))

    render(<App />)

    await act(async () => {
      resolveStatus?.(initializationStatus('completed'))
    })
    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()

    // 主界面获得焦点时会再次核验，此时返回能力未就绪。
    act(() => window.dispatchEvent(new Event('focus')))
    await waitFor(() => {
      expect(initializationMocks.fetchInitializationStatus.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
    await act(async () => {
      resolveStatus?.(initializationStatus('not_started'))
    })

    expect(await screen.findByTestId('startup-loading')).toBeInTheDocument()
    expect(screen.queryByText('烤面包')).not.toBeInTheDocument()
    expect(useAppStore.getState().hasCompletedSetup).toBe(true)
  })

  it('未登录也直接进入主界面，并在侧栏显示本地昵称', async () => {
    render(<App />)

    expect(await screen.findByTestId('floating-buddy')).toBeInTheDocument()
    const entry = screen.getByRole('button', { name: /打开.+的个人中心/ })
    expect(entry).toHaveTextContent('本地模式')
    expect(entry).not.toHaveTextContent('登录账户')
    expect(screen.queryByTestId('auth-panel')).not.toBeInTheDocument()

    fireEvent.click(entry)
    expect(await screen.findByRole('tab', { name: '个人信息' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '面包屑' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '工作投入' })).toBeInTheDocument()
    expect(screen.getByText('登录账户')).toBeInTheDocument()
  })

  it('离线启动时不访问云端，缓存账号和本地主界面仍然可用', async () => {
    const cachedUser = {
      id: '018f0000-0000-7000-8000-000000000011',
      username: '离线用户',
      status: 'active',
      roles: ['user'],
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      created_at: new Date().toISOString(),
    }
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_offline_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: cachedUser,
    })
    Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: false })
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => ({
      ok: false,
      status: 503,
      json: async () => ({}),
    }))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开离线用户的个人中心' })).toBeInTheDocument()
    expect(useAppStore.getState().authToken).toBe('mbs_offline_token')
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/v1/'))).toBe(false)
  })

  it('账户服务连接失败时保留缓存会话，不影响本地使用', async () => {
    const cachedUser = {
      id: '018f0000-0000-7000-8000-000000000012',
      username: '弱依赖用户',
      status: 'active',
      roles: ['user'],
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      created_at: new Date().toISOString(),
    }
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_degraded_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: cachedUser,
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/auth/me')) throw new TypeError('offline')
      return { ok: false, status: 503, json: async () => ({}) }
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/v1/auth/me'))).toBe(true)
    })
    expect(useAppStore.getState().authToken).toBe('mbs_degraded_token')
    expect(useAppStore.getState().currentUser?.username).toBe('弱依赖用户')
  })

  it('账户服务明确拒绝缓存凭据时才清理会话', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_expired_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: {
        id: '018f0000-0000-7000-8000-000000000013',
        username: '过期用户',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/auth/me')) {
        return {
          ok: false,
          status: 401,
          json: async () => ({ error: { code: 'AUTH_SESSION_INVALID', message: '会话已失效' } }),
        }
      }
      return { ok: false, status: 503, json: async () => ({}) }
    }))

    render(<App />)

    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()
    await waitFor(() => expect(useAppStore.getState().authToken).toBeNull())
    expect(screen.getByRole('button', { name: /打开.+的个人中心/ })).toHaveTextContent('本地模式')
  })

  it('旧消息页面状态会在冷启动门禁后回到咨询主界面', async () => {
    useAppStore.getState().setWindowMode('messages')

    render(<App />)

    expect(await screen.findByTestId('rag-panel')).toBeInTheDocument()
    expect(useAppStore.getState().windowMode).toBe('rag')
  })

  it('已有登录会话启动后工作画像仍只读取本机数据', async () => {
    const user = {
      id: '018f0000-0000-7000-8000-000000000008',
      username: '同步测试用户',
      email: 'sync@memorybread.local',
      status: 'active',
      roles: ['user'],
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      created_at: new Date().toISOString(),
    }
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_sync_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user,
    })
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    const date = [
      today.getFullYear(),
      String(today.getMonth() + 1).padStart(2, '0'),
      String(today.getDate()).padStart(2, '0'),
    ].join('-')
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/auth/me')) {
        return { ok: true, json: async () => ({ data: user }) }
      }
      if (url.endsWith('/v1/console/summary')) {
        return { ok: true, json: async () => ({ data: {} }) }
      }
      if (url.includes('/api/work-profile')) {
        return {
          ok: true,
          json: async () => ({
            range_start: today.getTime() - 370 * 86400_000,
            range_end: today.getTime() + 86400_000,
            idle_gap_cap_minutes: 5,
            total_minutes: 30,
            active_days: 1,
            current_streak: 1,
            longest_streak: 1,
            longest_day_minutes: 30,
            today: {
              date,
              total_minutes: 30,
              capture_count: 6,
              first_capture_at: today.getTime() + 9 * 3600_000,
              last_capture_at: today.getTime() + 9.5 * 3600_000,
              apps: [{ name: 'Code', minutes: 30, capture_count: 6 }],
              mood: {
                inferred: true,
                mood: 'focused',
                expression_count: 2,
                source_apps: ['Slack'],
              },
            },
            days: [{ date, minutes: 30, capture_count: 6 }],
          }),
        }
      }
      if (url.endsWith('/v1/work-profile') && init?.method === 'PUT') {
        return {
          ok: true,
          json: async () => ({
            data: {
              applied: true,
              profile: {
                range_start_date: date,
                range_end_date: date,
                synced_at: new Date().toISOString(),
                days: [{
                  date,
                  minutes: 30,
                  capture_count: 6,
                  first_capture_at: today.getTime() + 9 * 3600_000,
                  last_capture_at: today.getTime() + 9.5 * 3600_000,
                  apps: [{ name: 'Code', minutes: 30, capture_count: 6 }],
                  mood: {
                    inferred: true,
                    mood: 'focused',
                    expression_count: 2,
                    source_apps: ['Slack'],
                  },
                }],
              },
            },
          }),
        }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('/api/work-profile')
    ))).toBe(true))
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('/v1/work-profile')
    ))).toBe(false)
  })

  it('打开具体 RAG 引用时才生成返回栈', async () => {
    render(<App />)
    await screen.findByTestId('rag-panel')

    act(() => {
      window.dispatchEvent(new CustomEvent('view-rag-reference', {
        detail: { type: 'document', documentId: '42' },
      }))
    })

    expect(screen.getByTestId('bake-panel')).toBeInTheDocument()
    expect(useAppStore.getState().bakeNavigationStack).toEqual([{ windowMode: 'rag' }])
  })

  it.each([
    ['文档', { type: 'document', documentId: '42' }, 'bake-panel'],
    ['知识', { type: 'bake_knowledge', artifactId: '86' }, 'bake-panel'],
    ['操作', { type: 'operation', artifactId: '73' }, 'bake-panel'],
    ['数据', { type: 'data', dataSourceId: '60' }, 'bake-panel'],
    ['采集', { type: 'capture', captureId: '19' }, 'repository-panel'],
  ])('从创作记录打开%s引用时把创作页写入返回栈', async (_label, detail, targetPanel) => {
    render(<App />)
    await screen.findByTestId('rag-panel')
    useAppStore.getState().setWindowMode('creation')

    act(() => {
      window.dispatchEvent(new CustomEvent('view-rag-reference', { detail }))
    })

    expect(screen.getByTestId(targetPanel)).toBeInTheDocument()
    expect(useAppStore.getState().bakeNavigationStack).toEqual([{ windowMode: 'creation' }])
  })

  it('打开数据记忆域引用时聚焦到记忆力的数据页', async () => {
    render(<App />)
    await screen.findByTestId('rag-panel')

    act(() => {
      window.dispatchEvent(new CustomEvent('view-rag-reference', {
        detail: { type: 'data', dataSourceId: '60' },
      }))
    })

    expect(screen.getByTestId('bake-panel')).toBeInTheDocument()
    expect(useAppStore.getState().bakeTab).toBe('data')
    expect(useAppStore.getState().bakeDataFocusId).toBe('60')
  })

  it('无具体目标的引用跳转会清除旧返回栈', async () => {
    useAppStore.getState().pushBakeNavigationTarget({ windowMode: 'creation' })
    render(<App />)
    await screen.findByTestId('rag-panel')

    act(() => {
      window.dispatchEvent(new CustomEvent('view-rag-reference', {
        detail: { type: 'document' },
      }))
    })

    expect(screen.getByTestId('bake-panel')).toBeInTheDocument()
    expect(useAppStore.getState().bakeNavigationStack).toHaveLength(0)
    expect(useAppStore.getState().captureBackTarget).toBeNull()
  })

  it('悬浮助手窗口不依赖 sidecar 初始化状态即可显示视觉主体', () => {
    window.history.replaceState({}, '', '/?view=floating-assist')
    initializationMocks.fetchInitializationStatus.mockClear()
    initializationMocks.fetchInitializationStatus.mockRejectedValue(new Error('sidecar unavailable'))

    render(<App />)

    expect(screen.getByTestId('floating-assist')).toBeInTheDocument()
    expect(initializationMocks.fetchInitializationStatus).not.toHaveBeenCalled()
  })
})
