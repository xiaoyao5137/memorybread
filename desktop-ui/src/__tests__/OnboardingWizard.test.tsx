import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OnboardingWizard from '../components/OnboardingWizard'
import { useAppStore } from '../store/useAppStore'
import { fetchInitializationStatus, type InitializationStatus } from '../utils/initialization'

const stageIds = [
  ['preflight', '检查运行环境'],
  ['inference_engine', '准备本地 AI 引擎'],
  ['capture_model', '准备采集提炼能力'],
  ['vector_model', '准备语义检索能力'],
  ['database', '准备本地记忆库'],
  ['skills_tools', '准备技能与工具'],
  ['quality_gate', '执行完整质检'],
  ['feature_smoke_tests', '验证核心功能'],
] as const

function initialization(
  state: InitializationStatus['state'],
  patch: Partial<InitializationStatus> = {},
): InitializationStatus {
  return {
    schema_version: 'initialization.v1',
    run_id: state === 'not_started' ? null : 'init-run-001',
    mode: 'normal',
    state,
    progress: state === 'completed' ? 100 : state === 'running' ? 43 : 0,
    current_stage: state === 'running' ? 'capture_model' : 'preflight',
    message: state === 'running' ? '正在下载必要组件' : '需要完成初始化',
    stages: stageIds.map(([id, label], index) => ({
      id,
      label,
      status: state === 'completed'
        ? 'succeeded'
        : state === 'running' && index < 2
          ? 'succeeded'
          : state === 'running' && index === 2
            ? 'running'
            : 'pending',
      progress: state === 'completed' || index < 2 ? 100 : index === 2 && state === 'running' ? 61 : 0,
      detail: index === 2 && state === 'running' ? '正在下载采集提炼能力' : '等待执行',
      error_code: null,
      duration_ms: null,
    })),
    quality_gate: {
      passed: state === 'completed',
      checks: [],
    },
    smoke_tests: [],
    can_retry: state === 'failed',
    can_report: state === 'failed',
    test_mode_enabled: false,
    ...patch,
  }
}

function response(body: unknown, ok = true, status = ok ? 200 : 500): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as Response
}

beforeEach(() => {
  window.localStorage.clear()
  useAppStore.getState().reset()
  useAppStore.setState({ hasCompletedSetup: false, setupSkipped: false, windowMode: 'rag' })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('首次启动一键初始化', () => {
  it('本地服务无法连接时返回可操作提示而不是浏览器底层错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Load failed')))

    await expect(fetchInitializationStatus()).rejects.toMatchObject({
      code: 'INITIALIZATION_SERVICE_UNAVAILABLE',
      message: expect.stringContaining('请退出并重新启动记忆面包'),
    })
  })

  it('未初始化时只提供一个初始化主操作且不能跳过', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({
      status: 'ok',
      initialization: initialization('not_started'),
    })))

    render(<OnboardingWizard />)

    expect(await screen.findByRole('button', { name: /^初始化/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '烤面包' })).toBeInTheDocument()
    expect(screen.getByText('已存在的组件不会重复安装')).toBeInTheDocument()
    expect(screen.getByText('可以最小化软件等待初始化完成，请保持网络畅通。')).toBeInTheDocument()
    expect(screen.getByText('10–30 分钟')).toBeInTheDocument()
    expect(screen.getByText('约 4 GB')).toBeInTheDocument()
    expect(screen.getByText('约 6 GB')).toBeInTheDocument()
    expect(screen.queryByText(/跳过/)).not.toBeInTheDocument()
    expect(screen.queryByText(/全程在本机完成/)).not.toBeInTheDocument()
    expect(screen.getByText(/采集、提炼、咨询和创作测试/)).toBeInTheDocument()
  })

  it('旧版 sidecar 缺少初始化路由时立即提示重启，而不是显示通用 404', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({}, false, 404)))

    render(<OnboardingWizard />)

    expect(await screen.findByText(/本地初始化服务版本较旧/)).toBeInTheDocument()
    expect(screen.queryByText(/HTTP 404/)).not.toBeInTheDocument()
  })

  it('一次点击启动完整后台任务并显示总进度和当前阶段', async () => {
    const now = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(now)
    const running = initialization('running', {
      started_at: new Date(now - 125_000).toISOString(),
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/initialization/status')) {
        return response({ status: 'ok', initialization: initialization('not_started') })
      }
      if (url.endsWith('/api/initialization/start') && init?.method === 'POST') {
        return response({ status: 'ok', initialization: running })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<OnboardingWizard />)
    fireEvent.click(await screen.findByRole('button', { name: /^初始化/ }))

    expect(await screen.findByRole('progressbar', { name: '初始化进度 43%' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '面包烘焙中' })).toBeInTheDocument()
    expect(screen.getAllByText('准备采集提炼能力').length).toBeGreaterThan(0)
    expect(screen.getByText('可以最小化，初始化会在后台继续')).toBeInTheDocument()
    expect(screen.getByText(/烘焙时间较长，请耐心等待/)).toBeInTheDocument()
    expect(screen.getByText('执行时长')).toBeInTheDocument()
    expect(screen.getByText('02:05')).toBeInTheDocument()
    expect(screen.getByText('预计剩余时间')).toBeInTheDocument()
    expect(screen.getByText('约 12 分钟')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:7071/api/initialization/start',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('可恢复异常会先显示自动修复而不要求用户操作', async () => {
    const repairing = initialization('running', {
      current_stage: 'database',
      message: '本地核心服务未响应，正在安全重启应用内置服务',
      recovery: {
        status: 'running',
        action: 'restart_core_service',
        attempt: 1,
        max_attempts: 1,
        error_code: 'DATABASE_INITIALIZATION_FAILED',
      },
    })
    repairing.stages[4] = {
      ...repairing.stages[4],
      status: 'running',
      detail: '本地核心服务未响应，正在安全重启应用内置服务',
    }
    vi.stubGlobal('fetch', vi.fn(async () => response({
      status: 'ok',
      initialization: repairing,
    })))

    render(<OnboardingWizard />)

    expect(await screen.findByRole('heading', { name: '正在自动修复' })).toBeInTheDocument()
    expect(screen.getByText('检测到异常，正在后台自动修复')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试失败阶段' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '上报诊断' })).not.toBeInTheDocument()
  })

  it('只有正式环境质检通过后才写入完成标记并开放主界面', async () => {
    const onValidated = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async () => response({
      status: 'ok',
      initialization: initialization('completed'),
    })))

    render(<OnboardingWizard onStatusValidated={onValidated} />)

    await waitFor(() => expect(onValidated).toHaveBeenCalledWith(true))
    expect(useAppStore.getState().hasCompletedSetup).toBe(true)
    expect(window.localStorage.getItem('memory-bread_initialization_v2_done')).toBe('true')
  })

  it('失败时支持重试和用户确认后的脱敏诊断上报', async () => {
    const failed = initialization('failed', {
      current_stage: 'capture_model',
      error_code: 'MODEL_DOWNLOAD_FAILED',
      message: '采集提炼模型下载失败',
      suggestion: '请检查网络和磁盘空间后重试。',
    })
    failed.stages[2] = {
      ...failed.stages[2],
      status: 'failed',
      error_code: 'MODEL_DOWNLOAD_FAILED',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/initialization/status')) {
        return response({ status: 'ok', initialization: failed })
      }
      if (url.endsWith('/api/initialization/report-bundle')) {
        return response({
          status: 'ok',
          report: {
            schema_version: 'initialization.v1',
            run_id: '018f0000-0000-7000-8000-000000000001',
            installation_id: '018f0000-0000-7000-8000-000000000002',
            error_code: 'MODEL_DOWNLOAD_FAILED',
          },
        })
      }
      if (url.endsWith('/v1/initialization-reports') && init?.method === 'POST') {
        return response({ data: { report_id: '018f0000-0000-7000-8000-000000000003' }, request_id: 'request-001' })
      }
      if (url.endsWith('/api/debug/log-files')) {
        return response({ items: [{ key: 'core', label: '核心服务日志', exists: true, size_bytes: 20 }] })
      }
      if (url.endsWith('/api/debug/log-files/core')) {
        return response({
          key: 'core', label: '核心服务日志', content: 'startup failed', truncated: false,
          total_size_bytes: 14, returned_bytes: 14,
        })
      }
      if (url.endsWith('/v1/customer-logs/upload-url') && init?.method === 'POST') {
        return response({ data: {
          upload_id: '018f0000-0000-7000-8000-000000000004',
          oss_object_key: 'customer-logs/production/report.zip',
          upload_url: 'https://oss.example.com/report.zip',
          required_headers: { 'content-type': 'application/zip' },
        } })
      }
      if (url === 'https://oss.example.com/report.zip' && init?.method === 'PUT') {
        return response({})
      }
      if (url.endsWith('/v1/customer-logs') && init?.method === 'POST') {
        return response({ data: {
          log_id: '018f0000-0000-7000-8000-000000000004',
          received_at: '2026-09-01T00:00:00Z',
          duplicate: false,
        } })
      }
      if (url.endsWith('/api/initialization/start') && init?.method === 'POST') {
        return response({ status: 'ok', initialization: initialization('running') })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<OnboardingWizard />)

    expect(await screen.findByText(/MODEL_DOWNLOAD_FAILED/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '上报诊断' }))

    const dialog = await screen.findByRole('alertdialog', { name: '确认上报诊断信息？' })
    expect(dialog).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: '确认上报' }))

    expect(await screen.findByText(/018f0000 · 日志 018f0000/)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      'https://memorybread.cn/v1/initialization-reports',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'Idempotency-Key': '018f0000-0000-7000-8000-000000000001' }),
      }),
    )
    const completeCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).endsWith('/v1/customer-logs') && init?.method === 'POST')
    expect(JSON.parse(String(completeCall?.[1]?.body))).toMatchObject({
      initialization_report_id: '018f0000-0000-7000-8000-000000000003',
      installation_id: '018f0000-0000-7000-8000-000000000002',
    })
  })

  it('失败页会自动发现外部重新启动的初始化任务', async () => {
    const failed = initialization('failed', {
      current_stage: 'database',
      error_code: 'DATABASE_INITIALIZATION_FAILED',
      message: '本地记忆库未能完成初始化',
    })
    const running = initialization('running', {
      mode: 'sandbox',
      test_mode_enabled: true,
      progress: 50,
      current_stage: 'capture_model',
    })
    let statusRequests = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      statusRequests += 1
      return response({
        status: 'ok',
        initialization: statusRequests === 1 ? failed : running,
      })
    }))

    render(<OnboardingWizard />)

    expect(await screen.findByText(/DATABASE_INITIALIZATION_FAILED/)).toBeInTheDocument()
    expect(
      await screen.findByRole(
        'progressbar',
        { name: '初始化进度 50%' },
        { timeout: 2_500 },
      ),
    ).toBeInTheDocument()
  })

  it('完成后的必需组件缺失时提供恢复操作但不允许误上报', async () => {
    const interrupted = initialization('interrupted', {
      error_code: 'INITIALIZATION_COMPONENT_MISSING',
      suggestion: '检测到本地能力不完整，点击恢复即可。',
      can_retry: true,
      can_report: false,
    })
    vi.stubGlobal('fetch', vi.fn(async () => response({
      status: 'ok',
      initialization: interrupted,
    })))

    render(<OnboardingWizard />)

    expect(await screen.findByRole('button', { name: '恢复初始化' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '上报诊断' })).not.toBeInTheDocument()
  })

  it('隔离模拟与真实初始化使用完全相同的主界面', async () => {
    const normal = initialization('not_started')
    vi.stubGlobal('fetch', vi.fn(async () => response({
      status: 'ok',
      initialization: normal,
    })))

    const normalView = render(<OnboardingWizard />)
    await screen.findByRole('button', { name: /^初始化/ })
    const normalMarkup = screen.getByTestId('initialization-card')
    const expectedMarkup = normalMarkup.innerHTML
    normalView.unmount()

    const sandbox = initialization('not_started', {
      mode: 'sandbox',
      test_mode_enabled: true,
      sandbox_isolation: {
        enforced: true,
        cold_start: true,
        normal_runtime_hidden: true,
        normal_models_hidden: true,
        normal_database_hidden: true,
      },
    })
    vi.stubGlobal('fetch', vi.fn(async () => response({
      status: 'ok',
      initialization: sandbox,
    })))

    render(<OnboardingWizard />)

    await screen.findByRole('button', { name: /^初始化/ })
    expect(screen.getByTestId('initialization-card').innerHTML).toBe(expectedMarkup)
    expect(screen.queryByText(/隔离初始化测试|SANDBOX|正式环境已完全隐藏/)).not.toBeInTheDocument()
  })

  it('隔离测试完成后从门禁关闭模式并恢复真实完成环境', async () => {
    const sandbox = initialization('completed', {
      mode: 'sandbox',
      test_mode_enabled: true,
      sandbox_isolation: {
        enforced: true,
        cold_start: true,
        normal_runtime_hidden: true,
        normal_models_hidden: true,
        normal_database_hidden: true,
      },
    })
    const normal = initialization('completed')
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/initialization/status')) {
        return response({ status: 'ok', initialization: sandbox })
      }
      if (url.endsWith('/api/initialization/test-mode') && init?.method === 'DELETE') {
        return response({ status: 'ok', initialization: normal })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<OnboardingWizard />)
    expect(await screen.findByText('面包烘焙完成')).toBeInTheDocument()
    expect(screen.getByText('模拟初始化已结束')).toBeInTheDocument()
    expect(screen.queryByText(/隔离初始化测试|SANDBOX|正式环境已完全隐藏/)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '关闭初始化测试模式' }))

    expect(screen.getByRole('dialog', { name: '关闭初始化测试模式' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith(
      'http://127.0.0.1:7071/api/initialization/test-mode',
      expect.objectContaining({ method: 'DELETE' }),
    )
    fireEvent.click(screen.getByRole('button', { name: '确认关闭' }))

    await waitFor(() => expect(useAppStore.getState().hasCompletedSetup).toBe(true))
    expect(useAppStore.getState().windowMode).toBe('debug')
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:7071/api/initialization/test-mode',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
