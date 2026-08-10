import type { AchievementAward, RewardTask } from '../types'
import { claimRewardTask, fetchRewardTasks, notifyAchievementsChanged } from './authApi'
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

// 连续活跃天数可能跨越本周边界（例如从上周三连续工作到今天），
// 只查询本周窗口会把连续天数截断，导致永远达不到阈值。
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

export const getCurrentWeeklyRewardPeriod = (now = new Date()) => {
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

const observedValueForTask = (
  task: RewardTask,
  profile: WorkProfileSummary,
  streakProfile: WorkProfileSummary | null,
): number | null => {
  if (task.period !== 'weekly') return null

  const sessionMetricField = WEEKLY_SESSION_METRICS[task.metric_key]
  if (sessionMetricField) {
    if (task.metric_unit !== 'minute') return null
    // 分类时长字段在旧核心上缺失（undefined），此时视为未达标、跳过该任务。
    const metrics = profile.achievement_metrics
    const observedValue = metrics ? metrics[sessionMetricField] : undefined
    return observedValue != null && Number.isFinite(observedValue) ? observedValue : null
  }

  if (task.metric_key === 'work_minutes') {
    if (task.metric_unit !== 'minute') return null
    return Number.isFinite(profile.total_minutes) ? profile.total_minutes : null
  }

  if (task.metric_key === 'active_streak_days') {
    if (task.metric_unit !== 'day') return null
    const streak = streakProfile?.current_streak
    return streak != null && Number.isFinite(streak) ? streak : null
  }

  return null
}

interface SyncAchievementTasksOptions {
  adminApiBaseUrl: string
  apiBaseUrl: string
  authToken: string
  now?: Date
  signal?: AbortSignal
}

/**
 * 用本地工作聚合自动领取已达标卡片。
 *
 * 只向账户服务提交任务 ID、周期和分钟数，不提交采集明细或工作内容。
 */
export const syncEligibleAchievementTasks = async ({
  adminApiBaseUrl,
  apiBaseUrl,
  authToken,
  now = new Date(),
  signal,
}: SyncAchievementTasksOptions): Promise<AchievementAward[]> => {
  const period = getCurrentWeeklyRewardPeriod(now)

  // 本地工作聚合是弱依赖：常驻核心进程未启动或不可达时直接跳过本轮检测，
  // 不抛错、不影响界面其它功能；核心恢复后由定时轮询自动重试。
  const workProfile = await fetchWorkProfileRange(apiBaseUrl, period, signal).catch(() => null)
  if (!workProfile) return []

  const tasks = await fetchRewardTasks(adminApiBaseUrl, authToken, signal)

  // 连续天数单独用更长的回溯窗口查询；查询失败时降级为不满足，不阻塞其它任务。
  const needsStreak = tasks.some(
    task => task.metric_key === 'active_streak_days' && task.period === 'weekly',
  )
  const streakProfile = needsStreak
    ? await fetchWorkProfileRange(apiBaseUrl, {
        from: period.from - STREAK_LOOKBACK_DAYS * 86_400_000,
        to: period.to,
        timezoneOffsetMinutes: period.timezoneOffsetMinutes,
      }, signal).catch(() => null)
    : null

  // 每轮检测使用新的幂等键，避免服务端重放历史成功响应时再次触发庆祝弹窗。
  const syncAttemptId = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
  const awardsByBadgeId = new Map<string, AchievementAward>()
  for (const task of tasks) {
    const observedValue = observedValueForTask(task, workProfile, streakProfile)
    const threshold = Number(task.threshold)
    if (observedValue == null || !Number.isFinite(threshold) || observedValue < threshold) continue

    // 单个任务领取失败（网络抖动、服务端不可达等）只跳过该任务，不影响后续任务。
    const result = await claimRewardTask(
      adminApiBaseUrl,
      authToken,
      task.id,
      period.periodKey,
      observedValue,
      `auto_${syncAttemptId}_${task.id}`.slice(0, 80),
      signal,
    ).catch(() => null)
    if (!result) continue

    const previous = awardsByBadgeId.get(result.badge.id)
    awardsByBadgeId.set(result.badge.id, {
      badge: result.badge,
      badge_quantity: (previous?.badge_quantity ?? 0) + result.badge_quantity,
      total_badge_quantity: Math.max(
        previous?.total_badge_quantity ?? 0,
        result.total_badge_quantity,
      ),
    })
  }
  const awards = Array.from(awardsByBadgeId.values())
  if (awards.length > 0) notifyAchievementsChanged()
  return awards
}
