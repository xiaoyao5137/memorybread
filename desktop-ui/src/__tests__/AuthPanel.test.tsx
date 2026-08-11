import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import AuthPanel from '../components/AuthPanel'
import { useAppStore } from '../store/useAppStore'
import { toLocalDateKey } from '../utils/workProfile'

beforeEach(() => {
  useAppStore.getState().reset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

const fillPasswordLogin = () => {
  fireEvent.change(screen.getByPlaceholderText('you@example.com'), {
    target: { value: 'admin@memorybread.local' },
  })
  fireEvent.change(screen.getByPlaceholderText('至少 8 个字符'), {
    target: { value: 'MemoryBread@2026!' },
  })
}

const mockSignedInProfileFetch = () => vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input)
  if (url.includes('/api/work-profile')) {
    const includeDayDetails = new URL(url).searchParams.get('include_day_details') === 'true'
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    const previousDay = new Date(today)
    previousDay.setDate(previousDay.getDate() - 1)
    return {
      ok: true,
      json: async () => ({
        range_start: Date.now() - 365 * 86400_000,
        range_end: Date.now() + 86400_000,
        idle_gap_cap_minutes: 5,
        total_minutes: 1860,
        active_days: 16,
        current_streak: 4,
        longest_streak: 9,
        longest_day_minutes: 382,
        today: {
          date: toLocalDateKey(today),
          total_minutes: 205,
          capture_count: 42,
          active_period_count: 7,
          first_capture_at: today.getTime() + 9 * 3_600_000 + 12 * 60_000,
          last_capture_at: today.getTime() + 16 * 3_600_000 + 48 * 60_000,
          apps: [
            { name: 'Code', minutes: 138, capture_count: 24 },
            { name: '飞书', minutes: 67, capture_count: 18 },
          ],
          mood: {
            inferred: true,
            mood: 'focused',
            expression_count: 12,
            source_apps: ['飞书', 'Slack'],
          },
        },
        days: [
          {
            date: toLocalDateKey(previousDay),
            minutes: 280,
            capture_count: 56,
            ...(includeDayDetails ? {
              active_period_count: 6,
              first_capture_at: previousDay.getTime() + 8 * 3_600_000 + 35 * 60_000,
              last_capture_at: previousDay.getTime() + 25 * 3_600_000 + 10 * 60_000,
              apps: [
                { name: 'Figma', minutes: 170, capture_count: 34 },
                { name: '飞书', minutes: 110, capture_count: 22 },
              ],
            } : {}),
          },
          {
            date: toLocalDateKey(today),
            minutes: 205,
            capture_count: 42,
            active_period_count: 7,
            first_capture_at: today.getTime() + 9 * 3_600_000 + 12 * 60_000,
            last_capture_at: today.getTime() + 16 * 3_600_000 + 48 * 60_000,
            apps: [
              { name: 'Code', minutes: 138, capture_count: 24 },
              { name: '飞书', minutes: 67, capture_count: 18 },
            ],
          },
        ],
      }),
    }
  }

  if (url.includes('/v1/achievements')) {
    const badge = {
      id: '01910000-0000-7000-8000-000000000001',
      badge_key: 'code_elite',
      name: '代码精英',
      tagline: '逻辑在指尖升温',
      description: '一周内累计完成 50 小时代码编写工作。',
      icon_key: 'code',
      palette_key: 'ember',
      rarity: 'epic',
    }
    return {
      ok: true,
      json: async () => ({
        data: {
          badges: [{
            badge,
            quantity: 3,
            total_credit_earned: '600.0000',
            first_earned_at: '2026-07-01T00:00:00Z',
            last_earned_at: '2026-07-18T00:00:00Z',
          }],
          equipped: init?.method === 'PUT' ? { profile_avatar: badge } : {},
        },
      }),
    }
  }

  if (url.endsWith('/v1/auth/profile') && init?.method === 'PUT') {
    return {
      ok: true,
      json: async () => ({
        data: {
          id: '018f0000-0000-7000-8000-000000000003',
          username: '烘焙师土豆',
          nickname: '小麦',
          company_name: '记忆面包科技',
          email: 'tudou@memorybread.local',
          status: 'active',
          roles: ['user'],
          locale: 'zh-CN',
          timezone: 'Asia/Shanghai',
          created_at: new Date().toISOString(),
        },
      }),
    }
  }

  return {
    ok: true,
    json: async () => ({
      data: {
        balance: {
          available: '120.0000',
          reserved: '0.0000',
          currency: 'CREDIT',
          as_of: new Date().toISOString(),
        },
        current_subscription: {
          id: 'sub_001',
          status: 'active',
          plan_key: 'gold',
          name: '黄金',
        },
      },
    }),
  }
})

describe('AuthPanel', () => {
  it('普通启动时隐藏账户连接地址输入框', () => {
    render(<AuthPanel />)

    expect(screen.queryByLabelText('账户连接地址')).not.toBeInTheDocument()
  })

  it('调试模式下账户连接地址仍由测试环境固定提供', () => {
    useAppStore.getState().setDebugModeEnabled(true)

    render(<AuthPanel />)

    expect(screen.queryByLabelText('账户连接地址')).not.toBeInTheDocument()
    expect(useAppStore.getState().adminApiBaseUrl).toBe('http://127.0.0.1:18080')
  })

  it('注册时按邮箱和手机号区分入口，不再显示独立账户名或灰色提示', () => {
    render(<AuthPanel />)

    fireEvent.click(screen.getByRole('tab', { name: '注册' }))

    expect(screen.getByText('注册账户')).toBeInTheDocument()
    expect(screen.getByRole('tablist', { name: '注册方式' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '邮箱注册' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '手机号注册' })).toBeInTheDocument()
    expect(screen.queryByLabelText('账户名')).not.toBeInTheDocument()
    expect(screen.getByLabelText('邮箱地址')).not.toHaveAttribute('placeholder')
    expect(screen.getByLabelText('昵称')).not.toHaveAttribute('placeholder')

    fireEvent.click(screen.getByRole('tab', { name: '手机号注册' }))
    expect(screen.getByLabelText('手机号')).not.toHaveAttribute('placeholder')
  })

  it('邮箱注册仅提交邮箱标识，不再提交独立账户名', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input).endsWith('/v1/auth/email/send-code')) {
        return {
          ok: true,
          json: async () => ({
            data: {
              challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
              retry_after_seconds: 60,
              expires_in_seconds: 600,
            },
          }),
        }
      }
      return {
        ok: true,
        json: async () => ({
        data: {
          access_token: 'mbs_register_token',
          expires_at: new Date(Date.now() + 86400_000).toISOString(),
          user: {
            id: '018f0000-0000-7000-8000-000000000007',
            nickname: '小麦',
            email: 'xiaomai@memorybread.local',
            status: 'active',
            roles: ['user'],
            locale: 'zh-CN',
            timezone: 'Asia/Shanghai',
            created_at: new Date().toISOString(),
          },
        },
        }),
      }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AuthPanel />)

    fireEvent.click(screen.getByRole('tab', { name: '注册' }))
    fireEvent.change(screen.getByLabelText('邮箱地址'), { target: { value: 'xiaomai@memorybread.local' } })
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByRole('status')).toHaveTextContent('验证码已发送')
    fireEvent.change(screen.getByLabelText('邮箱验证码'), { target: { value: '123456' } })
    fireEvent.change(screen.getByLabelText('昵称'), { target: { value: '小麦' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'MemoryBread@2026!' } })
    fireEvent.click(screen.getByRole('button', { name: '注册并登录' }))

    await waitFor(() => {
      expect(useAppStore.getState().authToken).toBe('mbs_register_token')
    })
    expect(fetchMock.mock.calls[0]?.[0]).toBe('https://memorybread.work/v1/auth/email/send-code')
    const request = fetchMock.mock.calls[1]?.[1] as RequestInit
    expect(JSON.parse(String(request.body))).toEqual({
      email: 'xiaomai@memorybread.local',
      password: 'MemoryBread@2026!',
      nickname: '小麦',
      email_verification: {
        challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
        code: '123456',
      },
    })
  })

  it('默认以未登录界面启动，并在登录后保存管理员会话', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        data: {
          access_token: 'mbs_test_token',
          expires_at: new Date(Date.now() + 86400_000).toISOString(),
          user: {
            id: '018f0000-0000-7000-8000-000000000001',
            email: 'admin@memorybread.local',
            status: 'active',
            roles: ['platform_admin'],
            locale: 'zh-CN',
            timezone: 'Asia/Shanghai',
            created_at: new Date().toISOString(),
          },
        },
      }),
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AuthPanel />)

    expect(screen.getByTestId('auth-panel')).toBeInTheDocument()
    expect(useAppStore.getState().authToken).toBeNull()

    fillPasswordLogin()
    fireEvent.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => {
      expect(useAppStore.getState().authToken).toBe('mbs_test_token')
    })
    expect(useAppStore.getState().accountType).toBe('platform_admin')
    expect(fetchMock).toHaveBeenCalledWith('https://memorybread.work/v1/auth/login', expect.any(Object))
  })

  it('账户服务不可达时显示可操作的错误提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Load failed')))

    render(<AuthPanel />)
    fillPasswordLogin()
    fireEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('登录失败，请检查网络或账户信息')
    expect(screen.getByRole('alert')).not.toHaveTextContent('http://127.0.0.1:8080')
  })

  it('账户服务未就绪时提示稍后重试', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        error: {
          code: 'DATABASE_NOT_CONFIGURED',
          message: '账户服务尚未配置数据库',
        },
      }),
    }))

    render(<AuthPanel />)
    fillPasswordLogin()
    fireEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('账户服务暂时未就绪')
    expect(screen.getByRole('alert')).not.toHaveTextContent('mb-admin/.env')
  })

  it('通过邮箱验证码重置密码并返回密码登录', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input).endsWith('/v1/auth/password-reset/send-code')) {
        return {
          ok: true,
          json: async () => ({
            data: {
              challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb61',
              retry_after_seconds: 60,
              expires_in_seconds: 600,
            },
          }),
        }
      }
      return {
        ok: true,
        json: async () => ({ data: { ok: true } }),
      }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AuthPanel />)

    fireEvent.click(screen.getByRole('button', { name: '忘记密码？' }))
    expect(screen.getByRole('tablist', { name: '验证方式' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('邮箱地址'), { target: { value: 'xiaomai@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByRole('status')).toHaveTextContent('验证码已发送')
    fireEvent.change(screen.getByLabelText('验证码'), { target: { value: '123456' } })
    fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'MemoryBread@2026!' } })
    fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'MemoryBread@2026!' } })
    fireEvent.click(screen.getByRole('button', { name: '重置密码' }))

    expect(await screen.findByRole('status')).toHaveTextContent('密码已重置')
    expect(screen.getByLabelText('邮箱或手机号')).toHaveValue('xiaomai@example.com')
    expect(useAppStore.getState().authToken).toBeNull()
    const sendRequest = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(JSON.parse(String(sendRequest.body))).toEqual({
      channel: 'email',
      identifier: 'xiaomai@example.com',
    })
    const confirmRequest = fetchMock.mock.calls[1]?.[1] as RequestInit
    expect(JSON.parse(String(confirmRequest.body))).toEqual({
      challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb61',
      channel: 'email',
      identifier: 'xiaomai@example.com',
      code: '123456',
      new_password: 'MemoryBread@2026!',
    })
  })

  it('使用顶部多 Tab 展示全宽内容，并在个人信息中显示账户资料', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_test_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: {
        id: '018f0000-0000-7000-8000-000000000003',
        username: '烘焙师土豆',
        display_name: '土豆账户',
        nickname: '土豆',
        company_name: '旧公司',
        email: 'tudou@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    const fetchMock = mockSignedInProfileFetch()
    vi.stubGlobal('fetch', fetchMock)
    render(<AuthPanel />)

    expect(await screen.findByText('120.0000')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '土豆' })).toBeInTheDocument()
    expect(screen.getAllByText('烘焙师土豆')).not.toHaveLength(0)
    expect(screen.queryByText('土豆账户')).not.toBeInTheDocument()
    expect(screen.getByText('旧公司')).toBeInTheDocument()
    expect(screen.getByText(/每项每个自然月最多修改 3 次/)).toBeInTheDocument()
    expect(screen.getByRole('tablist', { name: '个人信息页面导航' })).toBeInTheDocument()
    expect(screen.getAllByRole('tab')).toHaveLength(5)
    expect(screen.getByRole('tab', { name: '个人信息' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: '消息' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '标签卡片' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '工作热力图' })).not.toBeInTheDocument()
    expect(screen.getByText('运行模式')).toBeInTheDocument()
    expect(await screen.findAllByText('增强模式')).not.toHaveLength(0)
    expect(screen.getAllByRole('button', { name: '退出登录' })).toHaveLength(1)
    expect(screen.queryByText('退出后需要重新登录，本机工作记录会继续保留。')).not.toBeInTheDocument()
    expect(screen.queryByText('会员套餐')).not.toBeInTheDocument()
    expect(screen.queryByText('冻结 Credit')).not.toBeInTheDocument()
    expect(screen.queryByText('币种')).not.toBeInTheDocument()
    expect(screen.queryByText('更新时间')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '充值' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '刷新' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '编辑个人资料' }))
    fireEvent.change(screen.getByLabelText('昵称'), { target: { value: '小麦' } })
    fireEvent.change(screen.getByLabelText('公司名称（可选）'), { target: { value: '记忆面包科技' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByRole('status')).toHaveTextContent('个人资料已更新')
    expect(screen.getByRole('heading', { name: '小麦' })).toBeInTheDocument()
    expect(useAppStore.getState().currentUser?.nickname).toBe('小麦')
    expect(fetchMock).toHaveBeenCalledWith(
      'https://memorybread.work/v1/auth/profile',
      expect.objectContaining({ method: 'PUT' }),
    )

    fireEvent.click(screen.getByRole('tab', { name: '标签卡片' }))
    expect(await screen.findByText('代码精英')).toBeInTheDocument()
    expect(screen.getByText('×3')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看「代码精英」卡片详情' }))
    fireEvent.click(screen.getByRole('button', { name: '佩戴到头像' }))
    expect(await screen.findByText('已将「代码精英」佩戴到个人头像。')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      'https://memorybread.work/v1/achievements/equipped',
      expect.objectContaining({ method: 'PUT' }),
    )

    fireEvent.click(screen.getByRole('tab', { name: '工作投入' }))
    expect(await screen.findByText('3 小时 25 分钟')).toBeInTheDocument()
    expect(screen.getByText('应用分布')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '工作热力图' })).toBeInTheDocument()
    const heatmapCell = screen.getByRole('button', { name: /查看.*工作数据.*工作时长 4 小时 40 分钟.*56 条工作记录/ })
    fireEvent.mouseEnter(heatmapCell)
    expect(within(heatmapCell).getByRole('tooltip')).toHaveTextContent('工作时长 4 小时 40 分钟')
    expect(within(heatmapCell).getByRole('tooltip')).toHaveTextContent('56 条工作记录')
    fireEvent.click(heatmapCell)
    expect(heatmapCell).toHaveAttribute('aria-pressed', 'true')
    expect(await screen.findByText('6 段活跃 · 首次 08:35 · 最后 次日 01:10')).toBeInTheDocument()
    expect(screen.getByText('4 小时 40 分钟')).toBeInTheDocument()
    const captureLink = screen.getByRole('button', { name: /查看.*56条工作记录/ })
    expect(captureLink).toHaveTextContent('查看 56 条工作记录')
    expect(screen.getByText('Figma')).toBeInTheDocument()
    expect(screen.queryByText('Code')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('include_day_details=true')
    ))).toBe(true)
    fireEvent.click(captureLink)
    const expectedPreviousDay = new Date()
    expectedPreviousDay.setHours(0, 0, 0, 0)
    expectedPreviousDay.setDate(expectedPreviousDay.getDate() - 1)
    expect(useAppStore.getState()).toMatchObject({
      windowMode: 'knowledge',
      repositoryTab: 'capture',
      repositoryCaptureFrom: toLocalDateKey(expectedPreviousDay),
      repositoryCaptureTo: toLocalDateKey(expectedPreviousDay),
      repositoryCaptureQuery: '',
      bakeCaptureOffset: 0,
    })
    fireEvent.click(heatmapCell)
    expect(heatmapCell).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('今日工作时长')).toBeInTheDocument()
    expect(screen.getByText('3 小时 25 分钟')).toBeInTheDocument()
    expect(screen.getByText('Code')).toBeInTheDocument()
    expect(screen.queryByText('近一年工作时长')).not.toBeInTheDocument()
    expect(screen.queryByText('当前连续天数')).not.toBeInTheDocument()
    expect(screen.queryByText('最长连续天数')).not.toBeInTheDocument()
    expect(screen.queryByText('单日最长时长')).not.toBeInTheDocument()
    expect(screen.queryByText('根据本机采集间隔聚合，连续空闲超过 5 分钟不计入工作时长。')).not.toBeInTheDocument()
  })

  it('展示根据工作 IM 推测的心情，不提供手工工作标签', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_test_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: {
        id: '018f0000-0000-7000-8000-000000000004',
        username: '烘焙师土豆',
        email: 'tudou@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    vi.stubGlobal('fetch', mockSignedInProfileFetch())
    render(<AuthPanel />)

    await waitFor(() => {
      expect(useAppStore.getState().cloudBalance?.available).toBe('120.0000')
    })
    fireEvent.click(screen.getByRole('tab', { name: '工作心情' }))
    expect(await screen.findByText('专注投入')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('条工作 IM 表达')).toBeInTheDocument()
    expect(screen.getByText('飞书、Slack')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '专注投入' })).not.toBeInTheDocument()
    expect(screen.queryByText('心情和标签仅保存在本机，可随时修改。')).not.toBeInTheDocument()
    expect(screen.queryByText('今日工作标签')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('添加自定义标签')).not.toBeInTheDocument()
  })

  it('没有有效工作 IM 输入时默认展示心情良好', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_test_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: {
        id: '018f0000-0000-7000-8000-000000000006',
        username: '烘焙师土豆',
        email: 'tudou@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    const fetchMock = mockSignedInProfileFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const response = await fetchMock(input)
      if (!String(input).includes('/api/work-profile')) return response
      const profile = await response.json() as Record<string, unknown> & {
        today: Record<string, unknown>
      }
      return {
        ...response,
        json: async () => ({
          ...profile,
          today: {
            ...profile.today,
            mood: {
              inferred: false,
              mood: null,
              expression_count: 0,
              source_apps: [],
            },
          },
        }),
      }
    }))
    render(<AuthPanel />)

    await waitFor(() => {
      expect(useAppStore.getState().cloudBalance?.available).toBe('120.0000')
    })
    fireEvent.click(screen.getByRole('tab', { name: '工作心情' }))
    expect(await screen.findByText('心情良好')).toBeInTheDocument()
    expect(screen.queryByText('今天还没有可分析的工作 IM 输入。')).not.toBeInTheDocument()
    expect(screen.queryByText('暂无今日心情推测')).not.toBeInTheDocument()
  })

  it('退出登录前要求二次确认，并可取消操作', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_test_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: {
        id: '018f0000-0000-7000-8000-000000000005',
        username: '烘焙师土豆',
        email: 'tudou@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    vi.stubGlobal('fetch', mockSignedInProfileFetch())
    render(<AuthPanel />)

    fireEvent.click(screen.getByRole('button', { name: '退出登录' }))

    const dialog = screen.getByRole('alertdialog', { name: '确认退出登录？' })
    expect(dialog).toBeInTheDocument()
    expect(within(dialog).getByText('确定要退出当前账号吗？')).toBeInTheDocument()
    const cancelButton = within(dialog).getByRole('button', { name: '取消' })
    const confirmButton = within(dialog).getByRole('button', { name: '确认退出' })
    await waitFor(() => expect(cancelButton).toHaveFocus())
    fireEvent.keyDown(cancelButton, { key: 'Tab', shiftKey: true })
    expect(confirmButton).toHaveFocus()
    fireEvent.keyDown(confirmButton, { key: 'Tab' })
    expect(cancelButton).toHaveFocus()
    fireEvent.click(cancelButton)
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(useAppStore.getState().authToken).toBe('mbs_test_token')
  })
})
