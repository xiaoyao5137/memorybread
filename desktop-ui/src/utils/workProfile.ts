export interface WorkProfileDay {
  date: string
  minutes: number
  capture_count: number
  active_period_count?: number | null
  first_capture_at?: number | null
  last_capture_at?: number | null
  apps?: WorkProfileApp[]
}

export interface WorkProfileApp {
  name: string
  minutes: number
  capture_count: number
}

export type InferredWorkMood = 'energized' | 'focused' | 'steady' | 'tired' | 'overloaded'

export interface WorkAchievementMetrics {
  longest_work_session_minutes: number
  max_overnight_work_minutes: number
  interruption_gap_minutes: number
  overnight_start_hour: number
  overnight_end_hour: number
  /** 分类时长由新版核心引擎本地聚合；旧核心或统计降级时字段缺失，消费方必须按可选处理。 */
  coding_minutes?: number
  design_minutes?: number
  focus_minutes?: number
  knowledge_minutes?: number
}

export interface WorkProfileSummary {
  range_start: number
  range_end: number
  idle_gap_cap_minutes: number
  total_minutes: number
  active_days: number
  current_streak: number
  longest_streak: number
  longest_day_minutes: number
  achievement_metrics?: WorkAchievementMetrics
  today: {
    date: string
    total_minutes: number
    capture_count: number
    active_period_count?: number
    first_capture_at: number | null
    last_capture_at: number | null
    apps: WorkProfileApp[]
    mood: {
      inferred: boolean
      mood: InferredWorkMood | null
      expression_count: number
      source_apps: string[]
    }
  }
  days: WorkProfileDay[]
}

const padDatePart = (value: number) => String(value).padStart(2, '0')

export const toLocalDateKey = (date: Date) => (
  `${date.getFullYear()}-${padDatePart(date.getMonth() + 1)}-${padDatePart(date.getDate())}`
)

export const getWorkProfileRange = (now = new Date()) => {
  const end = new Date(now)
  end.setHours(0, 0, 0, 0)
  end.setDate(end.getDate() + 1)

  const start = new Date(end)
  start.setDate(start.getDate() - 371)

  return {
    from: start.getTime(),
    to: end.getTime(),
    timezoneOffsetMinutes: -now.getTimezoneOffset(),
  }
}

export interface WorkProfileRange {
  from: number
  to: number
  timezoneOffsetMinutes: number
  includeAchievementMetrics?: boolean
  includeDayDetails?: boolean
}

const isSystemSessionApp = (app: WorkProfileApp) => (
  app.name.trim().toLocaleLowerCase() === 'loginwindow'
)

const sanitizeApps = (apps: WorkProfileApp[]) => {
  const systemApps = apps.filter(isSystemSessionApp)
  return {
    apps: apps.filter(app => !isSystemSessionApp(app)),
    removedMinutes: systemApps.reduce(
      (sum, app) => sum + Math.max(0, Math.round(Number(app.minutes) || 0)),
      0,
    ),
    removedCaptures: systemApps.reduce(
      (sum, app) => sum + Math.max(0, Math.round(Number(app.capture_count) || 0)),
      0,
    ),
  }
}

/**
 * 清理旧核心或云端缓存里曾误计入的 macOS 登录/锁屏系统会话。
 * 一旦移除了系统记录，旧首末时间也不再可信，等待本机日级明细重新补齐。
 */
export const sanitizeWorkProfile = (profile: WorkProfileSummary): WorkProfileSummary => {
  const rawTodayApps = Array.isArray(profile.today.apps) ? profile.today.apps : []
  const sanitizedTodayApps = sanitizeApps(rawTodayApps)
  const todayHadSystemSession = sanitizedTodayApps.removedCaptures > 0
    || sanitizedTodayApps.removedMinutes > 0

  const days = profile.days.map((day) => {
    const rawApps = Array.isArray(day.apps)
      ? day.apps
      : day.date === profile.today.date ? rawTodayApps : []
    const sanitized = sanitizeApps(rawApps)
    const removedSystemSession = sanitized.removedCaptures > 0 || sanitized.removedMinutes > 0
    return {
      ...day,
      minutes: Math.max(0, Math.round(Number(day.minutes) || 0) - sanitized.removedMinutes),
      capture_count: Math.max(
        0,
        Math.round(Number(day.capture_count) || 0) - sanitized.removedCaptures,
      ),
      active_period_count: removedSystemSession
        ? 0
        : day.active_period_count == null
          ? day.active_period_count
          : Math.max(0, Math.round(Number(day.active_period_count) || 0)),
      first_capture_at: removedSystemSession ? null : day.first_capture_at,
      last_capture_at: removedSystemSession ? null : day.last_capture_at,
      apps: Array.isArray(day.apps) ? sanitized.apps : day.apps,
    }
  })
  const todayDay = days.find(day => day.date === profile.today.date)
  const today = {
    ...profile.today,
    total_minutes: todayDay?.minutes ?? Math.max(
      0,
      Math.round(Number(profile.today.total_minutes) || 0) - sanitizedTodayApps.removedMinutes,
    ),
    capture_count: todayDay?.capture_count ?? Math.max(
      0,
      Math.round(Number(profile.today.capture_count) || 0) - sanitizedTodayApps.removedCaptures,
    ),
    active_period_count: todayDay?.active_period_count ?? (
      todayHadSystemSession
        ? 0
        : Math.max(0, Math.round(Number(profile.today.active_period_count) || 0))
    ),
    first_capture_at: todayHadSystemSession ? null : profile.today.first_capture_at,
    last_capture_at: todayHadSystemSession ? null : profile.today.last_capture_at,
    apps: sanitizedTodayApps.apps,
  }

  return {
    ...profile,
    total_minutes: days.reduce((sum, day) => sum + day.minutes, 0),
    active_days: days.filter(day => day.minutes > 0 || day.capture_count > 0).length,
    longest_day_minutes: days.reduce((maximum, day) => Math.max(maximum, day.minutes), 0),
    today,
    days,
  }
}

const normalizeOptionalMinutes = (value: unknown) => (
  typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined
)

const normalizeWorkProfile = (payload: Partial<WorkProfileSummary>): WorkProfileSummary => {
  if (
    !payload.today
    || !Array.isArray(payload.days)
    || !Array.isArray(payload.today.apps)
  ) {
    throw new Error('工作画像数据格式不完整')
  }

  // 桌面界面可能先于常驻核心进程完成热更新。旧核心尚未返回 mood 时，
  // 只将心情降级为空态，仍然保留有效的工作时长、分布和热力图数据。
  const receivedMood = payload.today.mood
  const mood = receivedMood && Array.isArray(receivedMood.source_apps)
    ? {
        inferred: receivedMood.inferred === true,
        mood: receivedMood.mood && [
          'energized',
          'focused',
          'steady',
          'tired',
          'overloaded',
        ].includes(receivedMood.mood)
          ? receivedMood.mood
          : null,
        expression_count: Number.isFinite(receivedMood.expression_count)
          ? receivedMood.expression_count
          : 0,
        source_apps: receivedMood.source_apps.filter(
          (app): app is string => typeof app === 'string',
        ),
      }
    : {
        inferred: false,
        mood: null,
        expression_count: 0,
        source_apps: [],
      }

  const receivedMetrics = payload.achievement_metrics
  const achievementMetrics = receivedMetrics
    && Number.isFinite(receivedMetrics.longest_work_session_minutes)
    && Number.isFinite(receivedMetrics.max_overnight_work_minutes)
    ? {
        ...receivedMetrics,
        // 分类时长字段可能被旧核心省略或被核心降级为零值；只保留有限数值，
        // 其它情况一律置为 undefined，任务评估会自动跳过对应标签。
        coding_minutes: normalizeOptionalMinutes(receivedMetrics.coding_minutes),
        design_minutes: normalizeOptionalMinutes(receivedMetrics.design_minutes),
        focus_minutes: normalizeOptionalMinutes(receivedMetrics.focus_minutes),
        knowledge_minutes: normalizeOptionalMinutes(receivedMetrics.knowledge_minutes),
      }
    : undefined

  return sanitizeWorkProfile({
    ...payload,
    achievement_metrics: achievementMetrics,
    today: {
      ...payload.today,
      active_period_count: Math.max(
        0,
        Math.round(Number(payload.today.active_period_count) || 0),
      ),
      mood,
    },
  } as WorkProfileSummary)
}

export const fetchWorkProfileRange = async (
  apiBaseUrl: string,
  range: WorkProfileRange,
  signal?: AbortSignal,
): Promise<WorkProfileSummary> => {
  const url = new URL(`${apiBaseUrl.replace(/\/$/, '')}/api/work-profile`)
  url.searchParams.set('from', String(range.from))
  url.searchParams.set('to', String(range.to))
  url.searchParams.set('timezone_offset_minutes', String(range.timezoneOffsetMinutes))
  if (range.includeAchievementMetrics) {
    url.searchParams.set('include_achievement_metrics', 'true')
  }
  if (range.includeDayDetails) {
    url.searchParams.set('include_day_details', 'true')
  }

  const response = await fetch(url.toString(), { signal })
  if (!response.ok) {
    throw new Error(`工作画像读取失败 (${response.status})`)
  }
  return normalizeWorkProfile(await response.json() as Partial<WorkProfileSummary>)
}

export const fetchWorkProfile = async (
  apiBaseUrl: string,
  signal?: AbortSignal,
): Promise<WorkProfileSummary> => {
  return fetchWorkProfileRange(apiBaseUrl, getWorkProfileRange(), signal)
}

export const hasWorkProfileDayDetails = (day: WorkProfileDay) => (
  day.capture_count <= 0
  || (
    day.active_period_count != null
    && day.first_capture_at != null
    && day.last_capture_at != null
    && Array.isArray(day.apps)
    && day.apps.length > 0
  )
)

export const fetchWorkProfileDay = async (
  apiBaseUrl: string,
  dateKey: string,
  signal?: AbortSignal,
): Promise<WorkProfileDay> => {
  const [year, month, day] = dateKey.split('-').map(Number)
  if (!year || !month || !day) throw new Error('工作日期格式不正确')

  const start = new Date(year, month - 1, day)
  const end = new Date(year, month - 1, day + 1)
  const profile = await fetchWorkProfileRange(apiBaseUrl, {
    from: start.getTime(),
    to: end.getTime(),
    timezoneOffsetMinutes: -start.getTimezoneOffset(),
    includeDayDetails: true,
  }, signal)
  const workDay = profile.days.find(item => item.date === dateKey)
  if (!workDay || !hasWorkProfileDayDetails(workDay)) {
    throw new Error('当天工作明细暂不可用')
  }
  return workDay
}
