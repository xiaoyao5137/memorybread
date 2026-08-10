import { afterEach, describe, expect, it, vi } from 'vitest'
import { ACHIEVEMENTS_CHANGED_KEY } from '../utils/authApi'
import { getCurrentWeeklyRewardPeriod, syncEligibleAchievementTasks } from '../utils/achievementTasks'

afterEach(() => {
  vi.unstubAllGlobals()
})

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

const task = (
  id: string,
  taskKey: string,
  metricKey: string,
  threshold: string,
  metricUnit = 'minute',
) => ({
  id,
  task_key: taskKey,
  title: taskKey,
  description: taskKey,
  status: 'active',
  approval_status: 'approved',
  period: 'weekly',
  metric_key: metricKey,
  threshold,
  metric_unit: metricUnit,
  reward: {
    badge: {
      id: `${id}-badge`,
      badge_key: taskKey,
      name: taskKey,
      tagline: taskKey,
      description: taskKey,
      icon_key: 'moon',
      palette_key: 'midnight',
      rarity: 'common',
    },
    badge_quantity: 1,
    credit: '40.0000',
  },
})

const workProfile = (withMetrics = true, overrides: Partial<{
  total_minutes: number
  current_streak: number
  days: Array<{ date: string, minutes: number, capture_count: number }>
  achievement_metrics: Record<string, number>
}> = {}) => ({
  range_start: 1,
  range_end: 2,
  idle_gap_cap_minutes: 5,
  total_minutes: overrides.total_minutes ?? 400,
  active_days: 1,
  current_streak: overrides.current_streak ?? 1,
  longest_streak: 1,
  longest_day_minutes: 400,
  achievement_metrics: withMetrics ? {
    longest_work_session_minutes: 241,
    max_overnight_work_minutes: 360,
    interruption_gap_minutes: 5,
    overnight_start_hour: 0,
    overnight_end_hour: 6,
    ...overrides.achievement_metrics,
  } : undefined,
  today: {
    date: '2026-07-20',
    total_minutes: 400,
    capture_count: 80,
    first_capture_at: 1,
    last_capture_at: 2,
    apps: [],
    mood: {
      inferred: false,
      mood: null,
      expression_count: 0,
      source_apps: [],
    },
  },
  days: overrides.days ?? [],
})

describe('achievement task sync', () => {
  it('uses a local Monday boundary and ISO week key', () => {
    const period = getCurrentWeeklyRewardPeriod(new Date(2026, 0, 1, 12))
    const start = new Date(period.from)
    const end = new Date(period.to)

    expect(period.periodKey).toBe('2026-W01')
    expect([start.getFullYear(), start.getMonth(), start.getDate(), start.getDay()])
      .toEqual([2025, 11, 29, 1])
    expect(end.getTime() - start.getTime()).toBe(7 * 86_400_000)
  })

  it('claims each supported task whose local aggregate reaches the threshold', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('overnight', 'weekly_overnight_writer', 'max_overnight_work_minutes', '360'),
          task('session', 'weekly_uninterrupted_four_hours', 'longest_work_session_minutes', '240'),
          task('unsupported', 'weekly_code_elite', 'coding_minutes', '1'),
        ] })
      }
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile())
      if (url.includes('/claims')) {
        const isOvernight = url.includes('overnight')
        return jsonResponse({ data: {
          task_id: isOvernight ? 'overnight' : 'session',
          period_key: '2026-W30',
          observed_value: isOvernight ? '360' : '241',
          badge: {
            id: isOvernight ? 'overnight-badge' : 'session-badge',
            badge_key: isOvernight ? 'overnight_writer' : 'uninterrupted_four_hours',
            name: isOvernight ? '通宵赶稿' : '憋尿达人',
            tagline: '',
            description: '',
            icon_key: isOvernight ? 'moon' : 'focus',
            palette_key: isOvernight ? 'midnight' : 'honey',
            rarity: 'common',
          },
          badge_quantity: 1,
          total_badge_quantity: 1,
          credit_granted: '40.0000',
        } })
      }
      throw new Error(`unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const achievementsChanged = vi.fn()
    window.addEventListener(ACHIEVEMENTS_CHANGED_KEY, achievementsChanged)

    const claimed = await syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })

    expect(claimed.map(({ badge }) => badge.name)).toEqual(['通宵赶稿', '憋尿达人'])
    expect(claimed.map(({ badge_quantity: quantity }) => quantity)).toEqual([1, 1])
    expect(achievementsChanged).toHaveBeenCalledTimes(1)
    window.removeEventListener(ACHIEVEMENTS_CHANGED_KEY, achievementsChanged)
    const workProfileCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/work-profile'))
    expect(String(workProfileCall?.[0])).toContain('include_achievement_metrics=true')
    const claimCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/claims'))
    expect(claimCalls).toHaveLength(2)
    expect(JSON.parse(String(claimCalls[0][1]?.body))).toMatchObject({
      period_key: '2026-W30',
      observed_value: '360',
    })
    expect(JSON.parse(String(claimCalls[1][1]?.body))).toMatchObject({
      period_key: '2026-W30',
      observed_value: '241',
    })
  })

  it('claims the seven-day streak task using a lookback window for the streak metric', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('streak', 'seven_day_streak', 'active_streak_days', '7', 'day'),
        ] })
      }
      if (url.includes('/api/work-profile')) {
        // 回溯窗口请求返回更长的连续天数；本周窗口返回较短的连续天数。
        const parsed = new URL(url)
        const usesLookback = Number(parsed.searchParams.get('from')) < new Date(2026, 6, 20).getTime()
        return jsonResponse(workProfile(true, { current_streak: usesLookback ? 9 : 2 }))
      }
      if (url.includes('/claims')) {
        return jsonResponse({ data: {
          task_id: 'streak',
          period_key: '2026-W30',
          observed_value: '9',
          badge: {
            id: 'streak-badge',
            badge_key: 'seven_day_streak',
            name: '七日恒温',
            tagline: '',
            description: '',
            icon_key: 'flame',
            palette_key: 'rose',
            rarity: 'common',
          },
          badge_quantity: 1,
          total_badge_quantity: 1,
          credit_granted: '80.0000',
        } })
      }
      throw new Error(`unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const claimed = await syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })

    expect(claimed.map(({ badge }) => badge.name)).toEqual(['七日恒温'])
    const claimCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/claims'))
    expect(claimCalls).toHaveLength(1)
    expect(JSON.parse(String(claimCalls[0][1]?.body))).toMatchObject({
      period_key: '2026-W30',
      observed_value: '9',
    })
  })

  it('claims the weekly total work minutes task from the profile total', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('sleepless', 'weekly_sleepless_warrior', 'work_minutes', '300'),
        ] })
      }
      if (url.includes('/api/work-profile')) {
        return jsonResponse(workProfile(true, {
          total_minutes: 400,
          days: [{ date: '2026-07-20', minutes: 400, capture_count: 80 }],
        }))
      }
      if (url.includes('/claims')) {
        return jsonResponse({ data: {
          task_id: 'sleepless',
          period_key: '2026-W30',
          observed_value: '400',
          badge: {
            id: 'sleepless-badge',
            badge_key: 'sleepless_warrior',
            name: '不睡战神',
            tagline: '',
            description: '',
            icon_key: 'moon',
            palette_key: 'midnight',
            rarity: 'legendary',
          },
          badge_quantity: 1,
          total_badge_quantity: 1,
          credit_granted: '500.0000',
        } })
      }
      throw new Error(`unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const claimed = await syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })

    expect(claimed.map(({ badge }) => badge.name)).toEqual(['不睡战神'])
    const claimCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/claims'))
    expect(JSON.parse(String(claimCalls[0][1]?.body))).toMatchObject({ observed_value: '400' })
  })

  it('uses a fresh idempotency key on later checks so a replay is not celebrated again', async () => {
    let claimedIdempotencyKey: string | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('session', 'weekly_uninterrupted_four_hours', 'longest_work_session_minutes', '240'),
        ] })
      }
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile())
      if (url.includes('/claims')) {
        const { idempotency_key: idempotencyKey } = JSON.parse(String(init?.body)) as {
          idempotency_key: string
        }
        if (claimedIdempotencyKey === null) {
          claimedIdempotencyKey = idempotencyKey
        } else if (idempotencyKey !== claimedIdempotencyKey) {
          return jsonResponse({
            error: {
              code: 'TASK_ALREADY_CLAIMED',
              message: 'task was already claimed for this period',
            },
          }, 409)
        }
        return jsonResponse({ data: {
          task_id: 'session',
          period_key: '2026-W30',
          observed_value: '241',
          badge: {
            id: 'session-badge',
            badge_key: 'uninterrupted_four_hours',
            name: '憋尿达人',
            tagline: '',
            description: '',
            icon_key: 'focus',
            palette_key: 'honey',
            rarity: 'common',
          },
          badge_quantity: 1,
          total_badge_quantity: 1,
          credit_granted: '40.0000',
        } })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const options = {
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    }

    await expect(syncEligibleAchievementTasks(options)).resolves.toHaveLength(1)
    await expect(syncEligibleAchievementTasks(options)).resolves.toEqual([])

    const claimBodies = fetchMock.mock.calls
      .filter(([input]) => String(input).includes('/claims'))
      .map(([, init]) => JSON.parse(String(init?.body)) as { idempotency_key: string })
    expect(claimBodies).toHaveLength(2)
    expect(claimBodies[1].idempotency_key).not.toBe(claimBodies[0].idempotency_key)
  })

  it('combines multiple rewards for the same card and keeps the cumulative quantity', async () => {
    let claimCount = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('session-a', 'weekly_session_a', 'longest_work_session_minutes', '240'),
          task('session-b', 'weekly_session_b', 'longest_work_session_minutes', '240'),
        ] })
      }
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile())
      if (url.includes('/claims')) {
        claimCount += 1
        return jsonResponse({ data: {
          task_id: claimCount === 1 ? 'session-a' : 'session-b',
          period_key: '2026-W30',
          observed_value: '241',
          badge: {
            id: 'session-badge',
            badge_key: 'uninterrupted_four_hours',
            name: '憋尿达人',
            tagline: '',
            description: '',
            icon_key: 'focus',
            palette_key: 'honey',
            rarity: 'common',
          },
          badge_quantity: claimCount === 1 ? 2 : 3,
          total_badge_quantity: claimCount === 1 ? 4 : 7,
          credit_granted: '40.0000',
        } })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })).resolves.toEqual([{
      badge: expect.objectContaining({
        badge_key: 'uninterrupted_four_hours',
        name: '憋尿达人',
      }),
      badge_quantity: 5,
      total_badge_quantity: 7,
    }])
  })

  it('keeps compatibility with a core process that has no achievement metrics', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('overnight', 'weekly_overnight_writer', 'max_overnight_work_minutes', '360'),
        ] })
      }
      return jsonResponse(workProfile(false))
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
    })).resolves.toEqual([])
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/claims'))).toBe(false)
  })

  it('does not celebrate an achievement that was already claimed for the period', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('overnight', 'weekly_overnight_writer', 'max_overnight_work_minutes', '360'),
        ] })
      }
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile())
      if (url.includes('/claims')) {
        return jsonResponse({
          error: {
            code: 'TASK_ALREADY_CLAIMED',
            message: 'task was already claimed for this period',
          },
        }, 409)
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const achievementsChanged = vi.fn()
    window.addEventListener(ACHIEVEMENTS_CHANGED_KEY, achievementsChanged)

    await expect(syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })).resolves.toEqual([])

    expect(achievementsChanged).not.toHaveBeenCalled()
    window.removeEventListener(ACHIEVEMENTS_CHANGED_KEY, achievementsChanged)
  })

  it('claims category minute tasks from the new core achievement metrics', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('coding', 'weekly_code_elite', 'coding_minutes', '3000'),
          task('focus', 'weekly_deep_focus', 'focus_minutes', '1800'),
        ] })
      }
      if (url.includes('/api/work-profile')) {
        return jsonResponse(workProfile(true, {
          achievement_metrics: { coding_minutes: 3200, focus_minutes: 1799 },
        }))
      }
      if (url.includes('/claims')) {
        return jsonResponse({ data: {
          task_id: 'coding',
          period_key: '2026-W30',
          observed_value: '3200',
          badge: {
            id: 'coding-badge',
            badge_key: 'code_elite',
            name: '代码精英',
            tagline: '',
            description: '',
            icon_key: 'code',
            palette_key: 'honey',
            rarity: 'common',
          },
          badge_quantity: 1,
          total_badge_quantity: 1,
          credit_granted: '40.0000',
        } })
      }
      throw new Error(`unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const claimed = await syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })

    // focus 未达阈值不领取；coding 达标领取。
    expect(claimed.map(({ badge }) => badge.name)).toEqual(['代码精英'])
    const claimCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/claims'))
    expect(claimCalls).toHaveLength(1)
    expect(JSON.parse(String(claimCalls[0][1]?.body))).toMatchObject({ observed_value: '3200' })
  })

  it('skips category minute tasks when an old core omits the category fields', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/v1/tasks')) {
        return jsonResponse({ data: [
          task('coding', 'weekly_code_elite', 'coding_minutes', '1'),
          task('knowledge', 'weekly_knowledge_baker', 'knowledge_minutes', '1'),
        ] })
      }
      // 旧核心不返回分类时长字段：不领取、不报错。
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile())
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })).resolves.toEqual([])
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/claims'))).toBe(false)
  })

  it('returns no awards without crashing when the local core engine is unreachable', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/work-profile')) {
        throw new TypeError('Failed to fetch')
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(syncEligibleAchievementTasks({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 6, 21, 12),
    })).resolves.toEqual([])
    // 本地核心不可达时不再请求任务清单和领取接口。
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
