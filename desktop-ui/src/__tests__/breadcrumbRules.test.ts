import { afterEach, describe, expect, it, vi } from 'vitest'
import { BREADCRUMBS_CHANGED_KEY } from '../utils/breadcrumbApi'
import {
  getCurrentWeeklyBreadcrumbPeriod,
  syncEligibleBreadcrumbRules,
} from '../utils/breadcrumbRules'
import type { BreadcrumbRule } from '../types'

const jsonResponse = (body: unknown, ok = true) => ({
  ok,
  status: ok ? 200 : 503,
  json: async () => body,
}) as Response

const rule = (overrides: Partial<BreadcrumbRule> = {}): BreadcrumbRule => ({
  id: 'rule-focus',
  rule_key: 'weekly_focus',
  title: '本周专注',
  description: '本周专注达到一百分钟。',
  breadcrumb: {
    id: 'breadcrumb-focus',
    breadcrumb_key: 'focused',
    name: '专注面包屑',
    tagline: '专注留下痕迹',
    description: '记录稳定的专注投入。',
    icon_key: 'focus',
    palette_key: 'forest',
    rarity: 'common',
  },
  calculation: {
    period: 'weekly',
    metric_key: 'focus_minutes',
    threshold: '100',
    metric_unit: 'minute',
    increment: 1,
  },
  starts_at: null,
  expires_at: null,
  version: 1,
  ...overrides,
})

const workProfile = {
  range_start: 1,
  range_end: 2,
  idle_gap_cap_minutes: 5,
  total_minutes: 180,
  active_days: 1,
  current_streak: 1,
  longest_streak: 1,
  longest_day_minutes: 180,
  achievement_metrics: {
    longest_work_session_minutes: 180,
    max_overnight_work_minutes: 0,
    coding_minutes: 0,
    design_minutes: 0,
    focus_minutes: 120,
    knowledge_minutes: 0,
    interruption_gap_minutes: 5,
    overnight_start_hour: 0,
    overnight_end_hour: 6,
  },
  today: {
    date: '2026-08-11',
    total_minutes: 180,
    capture_count: 20,
    first_capture_at: 1,
    last_capture_at: 2,
    apps: [],
    mood: { inferred: false, mood: null, expression_count: 0, source_apps: [] },
  },
  days: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('breadcrumb rule sync', () => {
  it('uses a local Monday boundary and ISO week key', () => {
    const period = getCurrentWeeklyBreadcrumbPeriod(new Date(2026, 0, 1, 12))
    const start = new Date(period.from)
    const end = new Date(period.to)

    expect(period.periodKey).toBe('2026-W01')
    expect([start.getFullYear(), start.getMonth(), start.getDate(), start.getDay()])
      .toEqual([2025, 11, 29, 1])
    expect(end.getTime() - start.getTime()).toBe(7 * 86_400_000)
  })

  it('downloads rules but computes and stores the quantity only in the local core', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile)
      if (url.endsWith('/v1/breadcrumb-rules')) return jsonResponse({ data: [rule()] })
      if (url.endsWith('/api/breadcrumbs/rules/sync')) {
        return jsonResponse({ breadcrumbs: [], equipped: {} })
      }
      if (url.endsWith('/api/breadcrumbs/awards')) {
        return jsonResponse({
          awarded: true,
          breadcrumb: rule().breadcrumb,
          increment: 1,
          total_quantity: 1,
        })
      }
      throw new Error(`unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const changed = vi.fn()
    window.addEventListener(BREADCRUMBS_CHANGED_KEY, changed)

    const awards = await syncEligibleBreadcrumbRules({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 7, 11, 12),
    })

    expect(awards).toEqual([expect.objectContaining({
      breadcrumb: expect.objectContaining({ breadcrumb_key: 'focused' }),
      increment: 1,
      total_quantity: 1,
    })])
    expect(changed).toHaveBeenCalledTimes(1)
    window.removeEventListener(BREADCRUMBS_CHANGED_KEY, changed)

    const awardCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/api/breadcrumbs/awards'))
    expect(JSON.parse(String(awardCall?.[1]?.body))).toEqual(expect.objectContaining({
      rule_id: 'rule-focus',
      observed_value: 120,
    }))
    expect(fetchMock.mock.calls.every(([input]) => !String(input).includes('/claims'))).toBe(true)
    expect(fetchMock.mock.calls.every(([, init]) => !String(init?.body).includes('credit'))).toBe(true)
  })

  it('uses cached local rules when the rule service is unavailable', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile)
      if (url.endsWith('/v1/breadcrumb-rules')) throw new TypeError('offline')
      if (url.endsWith('/api/breadcrumbs/rules')) return jsonResponse([rule()])
      if (url.endsWith('/api/breadcrumbs/awards')) {
        return jsonResponse({
          awarded: true,
          breadcrumb: rule().breadcrumb,
          increment: 1,
          total_quantity: 2,
        })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(syncEligibleBreadcrumbRules({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
      now: new Date(2026, 7, 11, 12),
    })).resolves.toEqual([expect.objectContaining({ total_quantity: 2 })])
  })

  it('does not celebrate an already-recorded local period', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/work-profile')) return jsonResponse(workProfile)
      if (url.endsWith('/v1/breadcrumb-rules')) return jsonResponse({ data: [rule()] })
      if (url.endsWith('/api/breadcrumbs/rules/sync')) {
        return jsonResponse({ breadcrumbs: [], equipped: {} })
      }
      if (url.endsWith('/api/breadcrumbs/awards')) {
        return jsonResponse({
          awarded: false,
          breadcrumb: rule().breadcrumb,
          increment: 0,
          total_quantity: 1,
        })
      }
      throw new Error(`unexpected request: ${url}`)
    }))

    await expect(syncEligibleBreadcrumbRules({
      adminApiBaseUrl: 'http://127.0.0.1:8080',
      apiBaseUrl: 'http://127.0.0.1:7070',
      authToken: 'mbs_token',
    })).resolves.toEqual([])
  })
})
