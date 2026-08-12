import type { BreadcrumbAward, BreadcrumbRule } from '../types'
import {
  awardBreadcrumb,
  fetchLocalBreadcrumbRules,
  fetchRemoteBreadcrumbRules,
  notifyBreadcrumbsChanged,
  syncLocalBreadcrumbRules,
} from './breadcrumbApi'
import {
  fetchWorkProfileRange,
  type WorkAchievementMetrics,
  type WorkProfileSummary,
} from './workProfile'

const WEEKLY_SESSION_METRICS: Partial<Record<string, keyof WorkAchievementMetrics>> = {
  longest_work_session_minutes: 'longest_work_session_minutes',
  max_overnight_work_minutes: 'max_overnight_work_minutes',
  coding_minutes: 'coding_minutes',
  design_minutes: 'design_minutes',
  focus_minutes: 'focus_minutes',
  knowledge_minutes: 'knowledge_minutes',
}

// 连续活跃天数可能跨越本周边界，单独查询较长的本地窗口。
const STREAK_LOOKBACK_DAYS = 30

const isoWeekKey = (date: Date) => {
  const normalized = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()))
  const weekday = normalized.getUTCDay() || 7
  normalized.setUTCDate(normalized.getUTCDate() + 4 - weekday)
  const weekYear = normalized.getUTCFullYear()
  const yearStart = new Date(Date.UTC(weekYear, 0, 1))
  const week = Math.ceil((((normalized.getTime() - yearStart.getTime()) / 86_400_000) + 1) / 7)
  return `${weekYear}-W${String(week).padStart(2, '0')}`
}

export const getCurrentWeeklyBreadcrumbPeriod = (now = new Date()) => {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7))
  const end = new Date(start)
  end.setDate(end.getDate() + 7)
  return {
    from: start.getTime(),
    to: end.getTime(),
    timezoneOffsetMinutes: -now.getTimezoneOffset(),
    includeAchievementMetrics: true,
    periodKey: isoWeekKey(start),
  }
}

const observedValueForRule = (
  rule: BreadcrumbRule,
  profile: WorkProfileSummary,
  streakProfile: WorkProfileSummary | null,
): number | null => {
  const calculation = rule.calculation
  if (calculation.period !== 'weekly') return null

  const sessionMetricField = WEEKLY_SESSION_METRICS[calculation.metric_key]
  if (sessionMetricField) {
    if (calculation.metric_unit !== 'minute') return null
    const observedValue = profile.achievement_metrics?.[sessionMetricField]
    return observedValue != null && Number.isFinite(observedValue) ? observedValue : null
  }

  if (calculation.metric_key === 'work_minutes') {
    if (calculation.metric_unit !== 'minute') return null
    return Number.isFinite(profile.total_minutes) ? profile.total_minutes : null
  }

  if (calculation.metric_key === 'active_streak_days') {
    if (calculation.metric_unit !== 'day') return null
    const streak = streakProfile?.current_streak
    return streak != null && Number.isFinite(streak) ? streak : null
  }

  return null
}

interface SyncBreadcrumbRulesOptions {
  adminApiBaseUrl: string
  apiBaseUrl: string
  authToken: string
  now?: Date
  signal?: AbortSignal
}

/**
 * 同步服务端规则，然后完全在本机计算并幂等累积面包屑。
 *
 * 服务端不可达时使用已经缓存到本地 SQLite 的规则；不会上传指标、数量或佩戴状态。
 */
export const syncEligibleBreadcrumbRules = async ({
  adminApiBaseUrl,
  apiBaseUrl,
  authToken,
  now = new Date(),
  signal,
}: SyncBreadcrumbRulesOptions): Promise<BreadcrumbAward[]> => {
  const period = getCurrentWeeklyBreadcrumbPeriod(now)
  const workProfile = await fetchWorkProfileRange(apiBaseUrl, period, signal).catch(() => null)
  if (!workProfile) return []

  let rules: BreadcrumbRule[]
  try {
    rules = await fetchRemoteBreadcrumbRules(adminApiBaseUrl, authToken, signal)
    await syncLocalBreadcrumbRules(apiBaseUrl, rules, signal)
  } catch {
    rules = await fetchLocalBreadcrumbRules(apiBaseUrl, signal).catch(() => [])
  }

  const needsStreak = rules.some(
    rule => rule.calculation.metric_key === 'active_streak_days'
      && rule.calculation.period === 'weekly',
  )
  const streakProfile = needsStreak
    ? await fetchWorkProfileRange(apiBaseUrl, {
        from: period.from - STREAK_LOOKBACK_DAYS * 86_400_000,
        to: period.to,
        timezoneOffsetMinutes: period.timezoneOffsetMinutes,
      }, signal).catch(() => null)
    : null

  const awardsByBreadcrumbId = new Map<string, BreadcrumbAward>()
  for (const rule of rules) {
    const observedValue = observedValueForRule(rule, workProfile, streakProfile)
    const threshold = Number(rule.calculation.threshold)
    if (observedValue == null || !Number.isFinite(threshold) || observedValue < threshold) continue

    const result = await awardBreadcrumb(
      apiBaseUrl,
      rule.id,
      period.periodKey,
      observedValue,
      signal,
    ).catch(() => null)
    if (!result?.awarded) continue

    const previous = awardsByBreadcrumbId.get(result.breadcrumb.id)
    awardsByBreadcrumbId.set(result.breadcrumb.id, {
      breadcrumb: result.breadcrumb,
      increment: (previous?.increment ?? 0) + result.increment,
      total_quantity: Math.max(previous?.total_quantity ?? 0, result.total_quantity),
    })
  }
  const awards = Array.from(awardsByBreadcrumbId.values())
  if (awards.length > 0) notifyBreadcrumbsChanged()
  return awards
}
