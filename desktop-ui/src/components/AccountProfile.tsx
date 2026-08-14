import React, { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertCircle,
  BatteryLow,
  Award,
  Bell,
  BookOpen,
  Briefcase,
  Building2,
  Clock,
  Code2,
  Flame,
  Focus,
  Frown,
  LogOut,
  Moon,
  Pencil,
  PenTool,
  RefreshCw,
  Save,
  Smile,
  UserRound,
  WalletCards,
  X,
  Zap,
  type LucideIcon,
} from 'lucide-react'
import type {
  AccountProfileSection,
  BreadcrumbDefinition,
  BreadcrumbProfile,
  BreadcrumbSurface,
  CloudBalance,
  CloudUser,
} from '../types'
import { updateUserProfile } from '../utils/authApi'
import {
  BREADCRUMBS_CHANGED_KEY,
  equipBreadcrumb,
  fetchBreadcrumbProfile,
} from '../utils/breadcrumbApi'
import { toUserFacingError } from '../utils/userFacingError'
import { useAppMetadata } from '../utils/appMetadata'
import {
  fetchWorkProfileDay,
  hasWorkProfileDayDetails,
  toLocalDateKey,
  type InferredWorkMood,
  type WorkProfileSummary,
} from '../utils/workProfile'
import {
  loadCachedWorkProfile,
  mergeWorkProfiles,
  synchronizeWorkProfile,
  WORK_PROFILE_SYNCED_EVENT,
  type SyncWorkProfileEventDetail,
} from '../utils/workProfileCloud'
import MessagePanel from './MessagePanel'
import AccountContactBindings from './AccountContactBindings'
import { useAppStore } from '../store/useAppStore'
import './AccountProfile.css'

interface AccountProfileProps {
  apiBaseUrl: string
  adminApiBaseUrl: string
  authToken: string
  user: CloudUser
  accountLabel: string
  runModeLabel: string
  cloudBalance: CloudBalance | null
  balanceError: string | null
  initialSection?: AccountProfileSection
  highlightedAchievementKeys?: string[]
  onInitialSectionHandled?: () => void
  onUserChange: (user: CloudUser) => void
  onLogout: () => void | Promise<void>
}

const TABS: Array<{
  id: AccountProfileSection
  label: string
  icon: LucideIcon
}> = [
  { id: 'personal', label: '个人信息', icon: UserRound },
  { id: 'messages', label: '消息', icon: Bell },
  { id: 'achievements', label: '面包屑', icon: Award },
  { id: 'investment', label: '工作投入', icon: Clock },
  { id: 'mood', label: '工作心情', icon: Smile },
]

const EMPTY_ACHIEVEMENT_KEYS: string[] = []

const MOOD_PRESENTATION: Record<InferredWorkMood, {
  label: string
  summary: string
  icon: LucideIcon
}> = {
  energized: {
    label: '积极有能量',
    summary: '今天的表达更积极，推进意愿也更强。',
    icon: Zap,
  },
  focused: {
    label: '专注投入',
    summary: '今天的表达集中在确认、处理和推进事项。',
    icon: Focus,
  },
  steady: {
    label: '平稳从容',
    summary: '今天的表达整体平稳，没有明显的压力或疲惫信号。',
    icon: Smile,
  },
  tired: {
    label: '有些疲惫',
    summary: '今天的表达里出现了较明显的疲惫信号。',
    icon: BatteryLow,
  },
  overloaded: {
    label: '压力偏高',
    summary: '今天的表达里出现了较明显的赶工或压力信号。',
    icon: Frown,
  },
}

const GOOD_MOOD_PRESENTATION: {
  label: string
  summary: string
  icon: LucideIcon
} = {
  label: '心情良好',
  summary: '今天状态良好，保持轻松稳定的工作节奏。',
  icon: Smile,
}

const APP_COLORS = ['#9f4522', '#bd6533', '#d58a4e', '#e3ad77', '#efd0aa', '#d9c0a5']

const BADGE_ICONS: Record<string, LucideIcon> = {
  code: Code2,
  blueprint: PenTool,
  moon: Moon,
  focus: Focus,
  book: BookOpen,
  flame: Flame,
}

const RARITY_LABELS: Record<BreadcrumbDefinition['rarity'], string> = {
  common: '常见',
  rare: '稀有',
  epic: '史诗',
  legendary: '传说',
}

const BADGE_WELLNESS_NOTES: Partial<Record<string, string>> = {
  sleepless_warrior: '这是一枚高强度纪念卡。请优先补充睡眠和休息。',
  overnight_writer: '这是一枚通宵纪念卡。完成赶稿后，请尽快补充睡眠。',
  uninterrupted_four_hours: '长时间久坐会影响健康。记得起身、喝水和休息。',
}

const BadgeMark: React.FC<{ badge: BreadcrumbDefinition; className?: string }> = ({ badge, className = '' }) => {
  const Icon = BADGE_ICONS[badge.icon_key] ?? Briefcase
  return (
    <span
      aria-label={`已佩戴${badge.name}`}
      className={`account-profile__badge-mark account-profile__badge-mark--${badge.palette_key} ${className}`}
      title={badge.name}
    >
      <Icon size={13} strokeWidth={2.4} aria-hidden />
    </span>
  )
}

const getInitials = (label: string) => {
  const characters = Array.from(label.trim())
  if (characters.length === 0) return '记'
  if (/\p{Script=Han}/u.test(characters[0])) return characters[0]
  return characters.slice(0, 2).join('').toUpperCase()
}

const formatDuration = (minutes: number) => {
  const normalized = Math.max(0, Math.round(minutes))
  if (normalized < 60) return `${normalized} 分钟`
  const hours = Math.floor(normalized / 60)
  const remainder = normalized % 60
  return remainder > 0 ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`
}

const formatClock = (timestamp: number | null) => {
  if (timestamp == null) return '--:--'
  return new Date(timestamp).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

const dateKeyIndex = (dateKey: string) => {
  const [year, month, day] = dateKey.split('-').map(Number)
  if (!year || !month || !day) return null
  return Math.floor(Date.UTC(year, month - 1, day) / 86_400_000)
}

const formatWorkdayClock = (timestamp: number | null, workdayDate: string) => {
  const clock = formatClock(timestamp)
  if (timestamp == null) return clock
  const workdayIndex = dateKeyIndex(workdayDate)
  const timestampIndex = dateKeyIndex(toLocalDateKey(new Date(timestamp)))
  if (workdayIndex == null || timestampIndex == null) return clock
  const dayOffset = timestampIndex - workdayIndex
  if (dayOffset === 1) return `次日 ${clock}`
  if (dayOffset > 1) return `${dayOffset} 日后 ${clock}`
  if (dayOffset === -1) return `前一日 ${clock}`
  if (dayOffset < -1) return `${Math.abs(dayOffset)} 日前 ${clock}`
  return clock
}

const formatCreatedAt = (value: string) => new Date(value).toLocaleString('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const formatHeatmapDate = (date: Date) => date.toLocaleDateString('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'short',
})

const getHeatLevel = (minutes: number) => {
  if (minutes <= 0) return 0
  if (minutes < 30) return 1
  if (minutes < 90) return 2
  if (minutes < 180) return 3
  return 4
}

const WorkProfileSkeleton: React.FC = () => (
  <div className="account-profile__skeleton" aria-label="正在读取工作画像">
    <div className="account-profile__skeleton-line account-profile__skeleton-line--wide" />
    <div className="account-profile__skeleton-grid">
      <div />
      <div />
    </div>
  </div>
)

const AccountProfile: React.FC<AccountProfileProps> = ({
  apiBaseUrl,
  adminApiBaseUrl,
  authToken,
  user,
  accountLabel,
  runModeLabel,
  cloudBalance,
  balanceError,
  initialSection,
  highlightedAchievementKeys: requestedHighlightedAchievementKeys = EMPTY_ACHIEVEMENT_KEYS,
  onInitialSectionHandled,
  onUserChange,
  onLogout,
}) => {
  const appMetadata = useAppMetadata()
  const [workProfile, setWorkProfile] = useState<WorkProfileSummary | null>(null)
  const [achievements, setAchievements] = useState<BreadcrumbProfile | null>(null)
  const [achievementsError, setAchievementsError] = useState<string | null>(null)
  const [achievementsLoading, setAchievementsLoading] = useState(true)
  const [achievementRetryKey, setAchievementRetryKey] = useState(0)
  const [equippingSurface, setEquippingSurface] = useState<BreadcrumbSurface | null>(null)
  const [achievementMessage, setAchievementMessage] = useState<string | null>(null)
  const [expandedAchievementId, setExpandedAchievementId] = useState<string | null>(null)
  const [workProfileError, setWorkProfileError] = useState<string | null>(null)
  const [workProfileLoading, setWorkProfileLoading] = useState(true)
  const [retryKey, setRetryKey] = useState(0)
  const [workDayDetailLoading, setWorkDayDetailLoading] = useState(false)
  const [workDayDetailError, setWorkDayDetailError] = useState<string | null>(null)
  const [workDayDetailRetryKey, setWorkDayDetailRetryKey] = useState(0)
  const [activeSection, setActiveSection] = useState<AccountProfileSection>('personal')
  const [highlightedAchievementKeys, setHighlightedAchievementKeys] = useState<string[]>([])
  const [pinnedHeatmapDate, setPinnedHeatmapDate] = useState<string | null>(null)
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false)
  const [logoutPending, setLogoutPending] = useState(false)
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [profileEditing, setProfileEditing] = useState(false)
  const [profilePending, setProfilePending] = useState(false)
  const [profileError, setProfileError] = useState<string | null>(null)
  const [profileMessage, setProfileMessage] = useState<string | null>(null)
  const [nicknameDraft, setNicknameDraft] = useState(user.nickname ?? user.username ?? '')
  const [companyNameDraft, setCompanyNameDraft] = useState(user.company_name ?? '')
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const logoutTriggerRef = useRef<HTMLButtonElement>(null)
  const logoutCancelRef = useRef<HTMLButtonElement>(null)
  const logoutDialogRef = useRef<HTMLElement>(null)
  const highlightedBadgeRef = useRef<HTMLElement>(null)
  const achievementDialogRef = useRef<HTMLElement>(null)
  const achievementDialogCloseRef = useRef<HTMLButtonElement>(null)
  const achievementDialogTriggerRef = useRef<HTMLButtonElement | null>(null)
  const expandedAchievement = achievements?.breadcrumbs.find(
    ({ breadcrumb }) => breadcrumb.id === expandedAchievementId,
  ) ?? null

  useEffect(() => {
    if (profileEditing) return
    setNicknameDraft(user.nickname ?? user.username ?? '')
    setCompanyNameDraft(user.company_name ?? '')
  }, [profileEditing, user.company_name, user.nickname, user.username])

  useEffect(() => {
    if (!initialSection) return
    const tabIndex = TABS.findIndex((tab) => tab.id === initialSection)
    setActiveSection(initialSection)
    setHighlightedAchievementKeys(requestedHighlightedAchievementKeys)
    const focusFrame = window.requestAnimationFrame(() => {
      if (tabIndex >= 0) tabRefs.current[tabIndex]?.focus()
    })
    onInitialSectionHandled?.()
    return () => window.cancelAnimationFrame(focusFrame)
  }, [initialSection, onInitialSectionHandled, requestedHighlightedAchievementKeys])

  useEffect(() => {
    let cancelled = false
    let hasProfile = false
    const applyProfile = (profile: WorkProfileSummary) => {
      if (cancelled) return
      hasProfile = true
      setWorkProfile((current) => current ? mergeWorkProfiles(current, profile) : profile)
      setWorkProfileLoading(false)
      setWorkProfileError(null)
    }
    const handleSyncedProfile = (event: Event) => {
      const detail = (event as CustomEvent<SyncWorkProfileEventDetail>).detail
      if (detail.userId === user.id) applyProfile(detail.profile)
    }

    const cached = loadCachedWorkProfile(user.id)
    if (cached) {
      applyProfile(cached)
    } else {
      setWorkProfile(null)
      setWorkProfileLoading(true)
    }
    setWorkProfileError(null)
    window.addEventListener(WORK_PROFILE_SYNCED_EVENT, handleSyncedProfile)
    if (!cached || retryKey > 0) {
      synchronizeWorkProfile({
        apiBaseUrl,
        adminApiBaseUrl,
        authToken,
        userId: user.id,
      })
        .then((profile) => {
          applyProfile(profile)
        })
        .catch((error) => {
          if (!cancelled && !hasProfile) {
            setWorkProfileError(toUserFacingError(error, '工作画像读取失败'))
            setWorkProfileLoading(false)
          }
        })
    }
    return () => {
      cancelled = true
      window.removeEventListener(WORK_PROFILE_SYNCED_EVENT, handleSyncedProfile)
    }
  }, [adminApiBaseUrl, apiBaseUrl, authToken, retryKey, user.id])

  useEffect(() => {
    const controller = new AbortController()
    setAchievementsLoading(true)
    setAchievementsError(null)
    const initialProfile = fetchBreadcrumbProfile(apiBaseUrl, controller.signal)
    initialProfile
      .then((profile) => {
        if (!controller.signal.aborted) setAchievements(profile)
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setAchievementsError(toUserFacingError(error, '面包屑读取失败'))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setAchievementsLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, achievementRetryKey])

  useEffect(() => {
    const refreshAchievements = () => setAchievementRetryKey((value) => value + 1)
    const refreshAchievementsFromStorage = (event: StorageEvent) => {
      if (event.key === BREADCRUMBS_CHANGED_KEY) refreshAchievements()
    }
    window.addEventListener(BREADCRUMBS_CHANGED_KEY, refreshAchievements)
    window.addEventListener('storage', refreshAchievementsFromStorage)
    return () => {
      window.removeEventListener(BREADCRUMBS_CHANGED_KEY, refreshAchievements)
      window.removeEventListener('storage', refreshAchievementsFromStorage)
    }
  }, [])

  useEffect(() => {
    if (
      activeSection !== 'achievements'
      || achievementsLoading
      || highlightedAchievementKeys.length === 0
    ) return undefined
    const scrollFrame = window.requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
      highlightedBadgeRef.current?.scrollIntoView?.({
        behavior: reduceMotion ? 'auto' : 'smooth',
        block: 'center',
      })
    })
    return () => window.cancelAnimationFrame(scrollFrame)
  }, [achievementsLoading, activeSection, highlightedAchievementKeys])

  useEffect(() => {
    if (!logoutConfirmOpen) return undefined
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setLogoutConfirmOpen(false)
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        logoutDialogRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
      )
      if (focusable.length < 2) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    window.requestAnimationFrame(() => logoutCancelRef.current?.focus())
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      logoutTriggerRef.current?.focus()
    }
  }, [logoutConfirmOpen])

  useEffect(() => {
    if (!expandedAchievementId) return undefined
    const focusFrame = window.requestAnimationFrame(() => achievementDialogCloseRef.current?.focus())
    const previousOverflow = document.body.style.overflow
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setExpandedAchievementId(null)
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        achievementDialogRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
      )
      if (focusable.length < 2) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', handleKeyDown)
      achievementDialogTriggerRef.current?.focus()
    }
  }, [expandedAchievementId])

  const heatmap = useMemo(() => {
    const workByDate = new Map(workProfile?.days.map((day) => [day.date, day]) ?? [])
    const current = new Date()
    current.setHours(0, 0, 0, 0)
    const mondayOffset = (current.getDay() + 6) % 7
    const currentWeekStart = new Date(current)
    currentWeekStart.setDate(currentWeekStart.getDate() - mondayOffset)
    const rangeStart = new Date(currentWeekStart)
    rangeStart.setDate(rangeStart.getDate() - 52 * 7)

    const cells = Array.from({ length: 53 * 7 }, (_, index) => {
      const date = new Date(rangeStart)
      date.setDate(date.getDate() + index)
      const dateKey = toLocalDateKey(date)
      const workDay = workByDate.get(dateKey)
      const minutes = workDay?.minutes ?? 0
      return {
        date,
        dateKey,
        dateLabel: formatHeatmapDate(date),
        minutes,
        captureCount: workDay?.capture_count ?? 0,
        level: getHeatLevel(minutes),
        week: Math.floor(index / 7),
        isFuture: date.getTime() > current.getTime(),
      }
    })

    const months: Array<{ label: string; week: number }> = []
    let previousMonth = -1
    for (let week = 0; week < 53; week += 1) {
      const date = cells[week * 7].date
      if (date.getMonth() !== previousMonth) {
        months.push({ label: `${date.getMonth() + 1}月`, week })
        previousMonth = date.getMonth()
      }
    }
    return { cells, months }
  }, [workProfile])

  const selectTab = (section: AccountProfileSection) => {
    setActiveSection(section)
  }

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % TABS.length
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + TABS.length) % TABS.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = TABS.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    const nextTab = TABS[nextIndex]
    setActiveSection(nextTab.id)
    tabRefs.current[nextIndex]?.focus()
  }

  const confirmLogout = async () => {
    setLogoutPending(true)
    setLogoutError(null)
    try {
      await onLogout()
      setLogoutConfirmOpen(false)
    } catch {
      setLogoutError('退出失败，请稍后重试。')
    } finally {
      setLogoutPending(false)
    }
  }

  const beginProfileEdit = () => {
    setNicknameDraft(user.nickname ?? user.username ?? '')
    setCompanyNameDraft(user.company_name ?? '')
    setProfileError(null)
    setProfileMessage(null)
    setProfileEditing(true)
  }

  const cancelProfileEdit = () => {
    setProfileEditing(false)
    setProfileError(null)
  }

  const saveProfile = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setProfilePending(true)
    setProfileError(null)
    setProfileMessage(null)
    try {
      const updatedUser = await updateUserProfile(
        adminApiBaseUrl,
        authToken,
        nicknameDraft,
        companyNameDraft,
      )
      onUserChange(updatedUser)
      setProfileEditing(false)
      setProfileMessage('个人资料已更新。')
    } catch (error) {
      setProfileError(toUserFacingError(error, '个人资料更新失败'))
    } finally {
      setProfilePending(false)
    }
  }

  const toggleBadge = async (
    surface: BreadcrumbSurface,
    badge: BreadcrumbDefinition,
  ) => {
    const equipped = achievements?.equipped[surface]
    const nextBadgeId = equipped?.id === badge.id ? null : badge.id
    setEquippingSurface(surface)
    setAchievementMessage(null)
    try {
      const profile = await equipBreadcrumb(apiBaseUrl, surface, nextBadgeId)
      setAchievements(profile)
      setAchievementMessage(nextBadgeId ? `已将「${badge.name}」佩戴到${surface === 'profile_avatar' ? '个人头像' : '悬浮球'}。` : `已从${surface === 'profile_avatar' ? '个人头像' : '悬浮球'}取下面包屑。`)
    } catch (error) {
      setAchievementMessage(toUserFacingError(error, '佩戴面包屑失败'))
    } finally {
      setEquippingSurface(null)
    }
  }

  const renderWorkError = () => (
    <div className="account-profile__data-error" role="alert">
      <AlertCircle size={19} aria-hidden />
      <div><strong>暂时无法读取工作记录</strong><span>{workProfileError}</span></div>
      <button onClick={() => setRetryKey((value) => value + 1)} type="button">重试</button>
    </div>
  )

  const selectedDate = pinnedHeatmapDate ?? workProfile?.today.date ?? null
  const selectedDay = workProfile?.days.find((day) => day.date === selectedDate)
  const selectedDateIsToday = Boolean(workProfile && selectedDate === workProfile.today.date)
  const selectedDayNeedsDetails = Boolean(
    pinnedHeatmapDate
    && selectedDay
    && selectedDay.capture_count > 0
    && !selectedDateIsToday
    && !hasWorkProfileDayDetails(selectedDay),
  )

  useEffect(() => {
    if (!pinnedHeatmapDate || !selectedDayNeedsDetails) {
      setWorkDayDetailLoading(false)
      setWorkDayDetailError(null)
      return undefined
    }

    const controller = new AbortController()
    setWorkDayDetailLoading(true)
    setWorkDayDetailError(null)
    fetchWorkProfileDay(apiBaseUrl, pinnedHeatmapDate, controller.signal)
      .then((detail) => {
        if (controller.signal.aborted) return
        setWorkProfile((current) => current && ({
          ...current,
          days: current.days.map((day) => (
            day.date === detail.date ? { ...day, ...detail } : day
          )),
        }))
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setWorkDayDetailError(toUserFacingError(error, '当天工作明细读取失败'))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setWorkDayDetailLoading(false)
      })

    return () => controller.abort()
  }, [
    apiBaseUrl,
    pinnedHeatmapDate,
    selectedDayNeedsDetails,
    workDayDetailRetryKey,
  ])

  const selectedWorkDay = workProfile && selectedDate
    ? {
        date: selectedDate,
        totalMinutes: selectedDateIsToday
          ? workProfile.today.total_minutes
          : selectedDay?.minutes ?? 0,
        captureCount: selectedDateIsToday
          ? workProfile.today.capture_count
          : selectedDay?.capture_count ?? 0,
        activePeriodCount: selectedDateIsToday
          ? workProfile.today.active_period_count ?? 0
          : selectedDay?.active_period_count ?? 0,
        firstCaptureAt: selectedDateIsToday
          ? workProfile.today.first_capture_at
          : selectedDay?.first_capture_at ?? null,
        lastCaptureAt: selectedDateIsToday
          ? workProfile.today.last_capture_at
          : selectedDay?.last_capture_at ?? null,
        apps: selectedDateIsToday
          ? workProfile.today.apps
          : selectedDay?.apps ?? [],
      }
    : null
  const selectedHeatmapCell = heatmap.cells.find((cell) => cell.dateKey === selectedDate)
  const selectedDateLabel = selectedHeatmapCell?.dateLabel ?? selectedDate ?? ''
  const totalAppMinutes = Math.max(
    1,
    selectedWorkDay?.apps.reduce((sum, app) => sum + app.minutes, 0) ?? 0,
  )
  const selectedWorkDayHasTimeRange = (
    selectedWorkDay?.firstCaptureAt != null
    && selectedWorkDay.lastCaptureAt != null
  )
  const selectedDayDetailPending = selectedDayNeedsDetails && !workDayDetailError
  const selectedDayDetailBusy = selectedDayDetailPending || workDayDetailLoading
  const openSelectedDayCaptures = () => {
    if (!selectedWorkDay || selectedWorkDay.captureCount <= 0) return
    useAppStore.setState({
      windowMode: 'knowledge',
      repositoryTab: 'capture',
      selectedMemoryId: null,
      selectedCaptureId: null,
      repositoryMemoryFocusId: null,
      repositoryCaptureQuery: '',
      repositoryCaptureFrom: selectedWorkDay.date,
      repositoryCaptureTo: selectedWorkDay.date,
      repositoryCaptureSourceCaptureId: null,
      bakeCaptureOffset: 0,
      bakeNavigationStack: [],
      captureBackTarget: null,
    })
  }
  const inferredMood = workProfile?.today.mood.mood
    ? MOOD_PRESENTATION[workProfile.today.mood.mood]
    : null
  const displayedMood = inferredMood ?? GOOD_MOOD_PRESENTATION
  const DisplayedMoodIcon = displayedMood.icon
  const accountName = user.username ?? user.email ?? user.phone ?? '本地账户'

  return (
    <main className="account-profile" data-testid="auth-panel">
      <div className="account-profile__shell">
        <header className="account-profile__hero">
          <span className="account-profile__avatar" aria-hidden="true">
            {getInitials(accountLabel)}
            {achievements?.equipped.profile_avatar && (
              <BadgeMark badge={achievements.equipped.profile_avatar} />
            )}
          </span>
          <div className="account-profile__identity">
            <h1>{accountLabel}</h1>
            <p>
              <span className="account-profile__account-name">{accountName}</span>
              <span aria-hidden="true">/</span>
              {runModeLabel}
            </p>
          </div>
        </header>

        <nav className="account-profile__tabs" role="tablist" aria-label="个人信息页面导航">
          {TABS.map((tab, index) => {
            const Icon = tab.icon
            const selected = activeSection === tab.id
            return (
              <button
                aria-controls={`account-profile-panel-${tab.id}`}
                aria-selected={selected}
                className={selected ? 'account-profile__tab account-profile__tab--active' : 'account-profile__tab'}
                id={`account-profile-tab-${tab.id}`}
                key={tab.id}
                onClick={() => selectTab(tab.id)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
                ref={(node) => { tabRefs.current[index] = node }}
                role="tab"
                tabIndex={selected ? 0 : -1}
                type="button"
              >
                <Icon size={17} aria-hidden />
                <span>{tab.label}</span>
              </button>
            )
          })}
        </nav>

        <div className="account-profile__content">
          <MessagePanel
            adminApiBaseUrl={adminApiBaseUrl}
            authToken={authToken}
            currentUser={user}
            embedded
            hidden={activeSection !== 'messages'}
            id="account-profile-panel-messages"
            key={`${user.id}:${adminApiBaseUrl}`}
            labelledBy="account-profile-tab-messages"
          />

          {activeSection === 'personal' && (
            <section
              aria-labelledby="account-profile-tab-personal"
              className="account-profile__panel"
              id="account-profile-panel-personal"
              role="tabpanel"
            >
              <div className="account-profile__panel-heading account-profile__panel-heading--profile">
                <div>
                  <h2>个人信息</h2>
                  <p>只会保存个人基础信息和软件心跳，任何工作采集信息不会上传云端</p>
                </div>
                {!profileEditing && (
                  <button aria-label="编辑个人资料" onClick={beginProfileEdit} type="button">
                    <Pencil size={17} aria-hidden />
                  </button>
                )}
              </div>
              {profileEditing && (
                <form className="account-profile__edit-form" onSubmit={saveProfile}>
                  <label>
                    <span>昵称</span>
                    <div>
                      <UserRound size={16} aria-hidden />
                      <input
                        autoComplete="nickname"
                        maxLength={30}
                        minLength={2}
                        onChange={(event) => setNicknameDraft(event.target.value)}
                        required
                        value={nicknameDraft}
                      />
                    </div>
                  </label>
                  <label>
                    <span>公司名称（可选）</span>
                    <div>
                      <Building2 size={16} aria-hidden />
                      <input
                        autoComplete="organization"
                        maxLength={100}
                        onChange={(event) => setCompanyNameDraft(event.target.value)}
                        placeholder="未填写"
                        value={companyNameDraft}
                      />
                    </div>
                  </label>
                  {profileError && <div className="account-profile__edit-error" role="alert">{profileError}</div>}
                  <div className="account-profile__edit-actions">
                    <button disabled={profilePending} onClick={cancelProfileEdit} type="button">
                      <X size={15} aria-hidden /> 取消
                    </button>
                    <button disabled={profilePending} type="submit">
                      <Save size={15} aria-hidden /> {profilePending ? '保存中' : '保存修改'}
                    </button>
                  </div>
                </form>
              )}
              {profileMessage && <div className="account-profile__edit-success" role="status">{profileMessage}</div>}
              <dl className="account-profile__detail-grid">
                <div><dt>账户名</dt><dd>{accountName}</dd></div>
                <div><dt>昵称</dt><dd>{user.nickname ?? user.username ?? '未设置'}</dd></div>
                <div><dt>公司名称</dt><dd>{user.company_name ?? '未填写'}</dd></div>
                <div><dt>运行模式</dt><dd>{runModeLabel}</dd></div>
                <div><dt>软件版本</dt><dd>v{appMetadata.version}</dd></div>
                <div><dt>账户状态</dt><dd>{user.status === 'active' ? '正常' : user.status}</dd></div>
                <div><dt>登录方式</dt><dd>{user.email ? '邮箱账号' : '手机号账号'}</dd></div>
                <div><dt>区域</dt><dd>{user.locale}</dd></div>
                <div><dt>时区</dt><dd>{user.timezone}</dd></div>
                <div><dt>创建时间</dt><dd>{formatCreatedAt(user.created_at)}</dd></div>
                <div className="account-profile__credit">
                  <dt><WalletCards size={16} aria-hidden /> 可用 Credit</dt>
                  <dd>{cloudBalance?.available ?? '-'}</dd>
                  {balanceError && <small role="alert">{balanceError}</small>}
                </div>
              </dl>

              <AccountContactBindings
                adminApiBaseUrl={adminApiBaseUrl}
                authToken={authToken}
                onUserChange={onUserChange}
                user={user}
              />

              <div className="account-profile__logout-area">
                <button
                  onClick={() => {
                    setLogoutError(null)
                    setLogoutConfirmOpen(true)
                  }}
                  ref={logoutTriggerRef}
                  type="button"
                >
                  <LogOut size={16} aria-hidden /> 退出登录
                </button>
              </div>
            </section>
          )}

          {activeSection === 'achievements' && (
            <section
              aria-labelledby="account-profile-tab-achievements"
              className="account-profile__panel account-profile__panel--achievements"
              id="account-profile-panel-achievements"
              role="tabpanel"
            >
              <div className="account-profile__panel-heading account-profile__panel-heading--achievements">
                <div>
                  <h2>面包屑</h2>
                  <p>面包屑会在本机持续累积，点击可查看并选择佩戴位置。</p>
                </div>
                {achievements && achievements.breadcrumbs.length > 0 && (
                  <div className="account-profile__achievement-total" aria-label="面包屑统计">
                    <strong>{achievements.breadcrumbs.reduce((sum, item) => sum + item.quantity, 0)}</strong>
                    <span>{achievements.breadcrumbs.length} 种面包屑</span>
                  </div>
                )}
              </div>

              {achievementsLoading && <WorkProfileSkeleton />}
              {!achievementsLoading && achievementsError && (
                <div className="account-profile__data-error" role="alert">
                  <AlertCircle size={19} aria-hidden />
                  <div><strong>暂时无法读取面包屑</strong><span>{achievementsError}</span></div>
                  <button onClick={() => setAchievementRetryKey((value) => value + 1)} type="button">重试</button>
                </div>
              )}
              {!achievementsLoading && !achievementsError && achievements?.breadcrumbs.length === 0 && (
                <div className="account-profile__achievement-empty">
                  <span aria-hidden="true"><Award size={28} /></span>
                  <strong>第一枚面包屑还在烘焙</strong>
                  <p>达到规则后，面包屑会在这台设备上自动累积，不会发放 Credit。</p>
                </div>
              )}
              {!achievementsLoading && !achievementsError && achievements && achievements.breadcrumbs.length > 0 && (
                <div className="account-profile__achievement-grid">
                  {achievements.breadcrumbs.map((item) => {
                    const breadcrumb = item.breadcrumb
                    const Icon = BADGE_ICONS[breadcrumb.icon_key] ?? Briefcase
                    const isNew = highlightedAchievementKeys.includes(breadcrumb.breadcrumb_key)
                    return (
                      <article
                        aria-label={isNew ? `${breadcrumb.name}，刚刚获得` : undefined}
                        className={`account-profile__achievement-card account-profile__achievement-card--${breadcrumb.palette_key}${isNew ? ' account-profile__achievement-card--new' : ''}`}
                        key={breadcrumb.id}
                        ref={isNew ? highlightedBadgeRef : undefined}
                      >
                        {isNew && <span className="account-profile__achievement-new-label">刚刚获得</span>}
                        <button
                          aria-haspopup="dialog"
                          aria-label={`查看「${breadcrumb.name}」面包屑详情`}
                          className="account-profile__achievement-card-trigger"
                          onClick={(event) => {
                            achievementDialogTriggerRef.current = event.currentTarget
                            setAchievementMessage(null)
                            setExpandedAchievementId(breadcrumb.id)
                          }}
                          type="button"
                        >
                          <div className="account-profile__achievement-card-top">
                            <span className="account-profile__achievement-icon" aria-hidden="true"><Icon size={22} strokeWidth={2.2} /></span>
                            <span className="account-profile__achievement-rarity">{RARITY_LABELS[breadcrumb.rarity]}</span>
                            <span className="account-profile__achievement-quantity">×{item.quantity}</span>
                          </div>
                          <div className="account-profile__achievement-copy">
                            <strong>{breadcrumb.name}</strong>
                            <span>{breadcrumb.tagline}</span>
                          </div>
                          <span className="account-profile__achievement-open-hint" aria-hidden="true">查看详情</span>
                        </button>
                      </article>
                    )
                  })}
                </div>
              )}
              {achievementMessage && !expandedAchievementId && (
                <div className="account-profile__achievement-message" role="status">{achievementMessage}</div>
              )}
            </section>
          )}

          {activeSection === 'investment' && (
            <section
              aria-labelledby="account-profile-tab-investment"
              className="account-profile__panel"
              id="account-profile-panel-investment"
              role="tabpanel"
            >
              <div className="account-profile__panel-heading account-profile__panel-heading--action">
                <h2>工作投入</h2>
                {!workProfileLoading && (
                  <button aria-label="刷新工作投入" onClick={() => setRetryKey((value) => value + 1)} type="button">
                    <RefreshCw size={17} aria-hidden />
                  </button>
                )}
              </div>

              {workProfileLoading && <WorkProfileSkeleton />}
              {!workProfileLoading && workProfileError && renderWorkError()}
              {!workProfileLoading && !workProfileError && workProfile && (
                <>
                  <div className="account-profile__investment-grid" aria-live="polite">
                    <div className="account-profile__today-total">
                      <span>{selectedDateIsToday ? '今日工作时长' : `${selectedDateLabel} · 工作时长`}</span>
                      <strong>{formatDuration(selectedWorkDay?.totalMinutes ?? 0)}</strong>
                      <p className="account-profile__active-periods">
                        <Clock size={16} aria-hidden />
                        {selectedDayDetailBusy
                          ? '正在读取当天工作时段…'
                          : workDayDetailError
                            ? '当天工作时段读取失败'
                            : selectedWorkDayHasTimeRange
                          ? selectedWorkDay.activePeriodCount > 0
                            ? `${selectedWorkDay.activePeriodCount} 段活跃 · 首次 ${formatWorkdayClock(selectedWorkDay.firstCaptureAt, selectedWorkDay.date)} · 最后 ${formatWorkdayClock(selectedWorkDay.lastCaptureAt, selectedWorkDay.date)}`
                            : `记录分布于 ${formatWorkdayClock(selectedWorkDay.firstCaptureAt, selectedWorkDay.date)} 至 ${formatWorkdayClock(selectedWorkDay.lastCaptureAt, selectedWorkDay.date)}（含间歇）`
                          : (selectedWorkDay?.captureCount ?? 0) > 0
                            ? '当天没有可用的工作时段明细'
                            : '暂无工作时段'}
                      </p>
                      {(selectedWorkDay?.captureCount ?? 0) > 0 ? (
                        <button
                          aria-label={`查看${selectedDateLabel}的${selectedWorkDay?.captureCount}条工作记录`}
                          className="account-profile__capture-link"
                          onClick={openSelectedDayCaptures}
                          type="button"
                        >
                          <BookOpen size={14} aria-hidden />
                          查看 {selectedWorkDay?.captureCount} 条工作记录
                        </button>
                      ) : (
                        <small>
                          {selectedDateIsToday
                            ? '今天还没有可统计的工作记录'
                            : '这一天还没有可统计的工作记录'}
                        </small>
                      )}
                    </div>
                    <div className="account-profile__distribution">
                      <div className="account-profile__distribution-head">
                        <h3>应用分布</h3>
                        <span>
                          {selectedDayDetailBusy
                            ? '正在读取'
                            : workDayDetailError
                              ? '读取失败'
                              : (selectedWorkDay?.apps.length ?? 0) > 0
                            ? `${selectedWorkDay?.apps.length} 个主要应用`
                            : selectedDateIsToday ? '等待采集' : '暂无明细'}
                        </span>
                      </div>
                      {selectedDayDetailBusy || workDayDetailError ? (
                        <div className="account-profile__empty">
                          <Activity size={25} aria-hidden />
                          <span>
                            {selectedDayDetailBusy
                              ? '正在读取这一天的应用分布…'
                              : '当天工作明细读取失败，请稍后重试。'}
                          </span>
                          {workDayDetailError && (
                            <button
                              onClick={() => setWorkDayDetailRetryKey((value) => value + 1)}
                              type="button"
                            >
                              重新读取
                            </button>
                          )}
                        </div>
                      ) : (selectedWorkDay?.apps.length ?? 0) > 0 ? (
                        <>
                          <div className="account-profile__distribution-bar" aria-label={`${selectedDateLabel}应用时长分布`}>
                            {selectedWorkDay?.apps.map((app, index) => (
                              <span
                                key={app.name}
                                style={{
                                  background: APP_COLORS[index % APP_COLORS.length],
                                  width: `${Math.max(3, (app.minutes / totalAppMinutes) * 100)}%`,
                                }}
                                title={`${app.name} ${formatDuration(app.minutes)}`}
                              />
                            ))}
                          </div>
                          <ul>
                            {selectedWorkDay?.apps.map((app, index) => (
                              <li key={app.name}>
                                <i style={{ background: APP_COLORS[index % APP_COLORS.length] }} aria-hidden="true" />
                                <span>{app.name}</span>
                                <strong>{formatDuration(app.minutes)}</strong>
                              </li>
                            ))}
                          </ul>
                        </>
                      ) : (
                        <div className="account-profile__empty">
                          <Activity size={25} aria-hidden />
                          <span>
                            {selectedDateIsToday
                              ? '开始工作后，这里会显示应用时长分布。'
                              : '这一天暂无可展示的应用分布。'}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>

                  <section className="account-profile__heatmap-section" aria-labelledby="work-investment-heatmap-title">
                    <div className="account-profile__heatmap-heading">
                      <h3 id="work-investment-heatmap-title">工作热力图</h3>
                      <div className="account-profile__heatmap-summary">
                        <strong>{workProfile.active_days}</strong>
                        <span>个活跃日</span>
                      </div>
                    </div>
                    <div className="account-profile__heatmap-frame">
                      <div className="account-profile__heatmap" aria-label="过去 53 周工作热力图">
                        <div className="account-profile__heatmap-months" aria-hidden="true">
                          {heatmap.months.map((month) => (
                            <span key={`${month.label}-${month.week}`} style={{ gridColumn: month.week + 1 }}>{month.label}</span>
                          ))}
                        </div>
                        <div className="account-profile__heatmap-body">
                          <div className="account-profile__heatmap-weekdays" aria-hidden="true">
                            <span>一</span><span /><span>三</span><span /><span>五</span><span /><span>日</span>
                          </div>
                          <div className="account-profile__heatmap-grid">
                            {heatmap.cells.map((cell) => cell.isFuture ? (
                              <span
                                aria-hidden="true"
                                className="account-profile__heatmap-cell account-profile__heatmap-cell--future"
                                key={cell.dateKey}
                              />
                            ) : (
                              <button
                                aria-label={`查看${cell.dateLabel}工作数据，工作时长 ${formatDuration(cell.minutes)}，${cell.captureCount} 条工作记录`}
                                aria-pressed={pinnedHeatmapDate === cell.dateKey}
                                className={`account-profile__heatmap-cell account-profile__heatmap-cell--${cell.level}${cell.week < 4 ? ' account-profile__heatmap-cell--tooltip-start' : ''}${cell.week > 48 ? ' account-profile__heatmap-cell--tooltip-end' : ''}${pinnedHeatmapDate === cell.dateKey ? ' account-profile__heatmap-cell--pinned' : ''}`}
                                key={cell.dateKey}
                                onClick={() => setPinnedHeatmapDate((current) => current === cell.dateKey ? null : cell.dateKey)}
                                tabIndex={cell.minutes > 0 ? 0 : -1}
                                type="button"
                              >
                                <span className="account-profile__heatmap-tooltip" role="tooltip">
                                  <strong>{cell.dateLabel}</strong>
                                  <span>工作时长 {formatDuration(cell.minutes)}</span>
                                  <span>{cell.captureCount} 条工作记录</span>
                                </span>
                              </button>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>
                    <div className="account-profile__heatmap-legend">
                      <span>少</span>
                      {[0, 1, 2, 3, 4].map((level) => <i className={`account-profile__heatmap-cell--${level}`} key={level} aria-hidden="true" />)}
                      <span>多</span>
                    </div>
                  </section>
                </>
              )}
            </section>
          )}

          {activeSection === 'mood' && (
            <section
              aria-labelledby="account-profile-tab-mood"
              className="account-profile__panel"
              id="account-profile-panel-mood"
              role="tabpanel"
            >
              <div className="account-profile__panel-heading">
                <h2>工作心情</h2>
              </div>

              {workProfileLoading && <WorkProfileSkeleton />}
              {!workProfileLoading && workProfileError && renderWorkError()}
              {!workProfileLoading && !workProfileError && workProfile && (
                <div className="account-profile__mood-layout">
                  <div className="account-profile__inferred-mood">
                    <span className="account-profile__mood-icon" aria-hidden="true"><DisplayedMoodIcon size={30} /></span>
                    <div className="account-profile__mood-copy">
                      <span>今日心情</span>
                      <strong>{displayedMood.label}</strong>
                      <p>{displayedMood.summary}</p>
                    </div>
                    {workProfile.today.mood.inferred && inferredMood && (
                      <div className="account-profile__mood-evidence">
                        <strong>{workProfile.today.mood.expression_count}</strong>
                        <span>条工作 IM 表达</span>
                        {workProfile.today.mood.source_apps.length > 0 && (
                          <small>{workProfile.today.mood.source_apps.join('、')}</small>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </section>
          )}
        </div>
      </div>

      {expandedAchievement && achievements && (() => {
        const badge = expandedAchievement.breadcrumb
        const Icon = BADGE_ICONS[badge.icon_key] ?? Briefcase
        const onProfile = achievements.equipped.profile_avatar?.id === badge.id
        const onFloating = achievements.equipped.floating_avatar?.id === badge.id
        return (
          <div
            className="account-profile__achievement-dialog-backdrop"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setExpandedAchievementId(null)
            }}
          >
            <section
              aria-describedby={`achievement-detail-description-${badge.id}`}
              aria-labelledby={`achievement-detail-title-${badge.id}`}
              aria-modal="true"
              className={`account-profile__achievement-dialog account-profile__achievement-dialog--${badge.palette_key}`}
              ref={achievementDialogRef}
              role="dialog"
            >
              <button
                aria-label={`关闭「${badge.name}」面包屑详情`}
                className="account-profile__achievement-dialog-close"
                onClick={() => setExpandedAchievementId(null)}
                ref={achievementDialogCloseRef}
                type="button"
              >
                <X size={18} aria-hidden />
              </button>
              <div className="account-profile__achievement-dialog-top">
                <span className="account-profile__achievement-dialog-icon" aria-hidden="true">
                  <Icon size={34} strokeWidth={2.1} />
                </span>
                <span className="account-profile__achievement-rarity">{RARITY_LABELS[badge.rarity]}</span>
                <span className="account-profile__achievement-quantity">×{expandedAchievement.quantity}</span>
              </div>
              <div className="account-profile__achievement-dialog-copy">
                <h2 id={`achievement-detail-title-${badge.id}`}>{badge.name}</h2>
                <span>{badge.tagline}</span>
                <p id={`achievement-detail-description-${badge.id}`}>{badge.description}</p>
              </div>
              <div className="account-profile__achievement-meta">
                <span>本机累计 <strong>{expandedAchievement.quantity}</strong> 枚</span>
                <span>最近获得 {new Date(expandedAchievement.last_earned_at).toLocaleDateString('zh-CN')}</span>
              </div>
              {BADGE_WELLNESS_NOTES[badge.breadcrumb_key] && (
                <div className="account-profile__achievement-rest-note">{BADGE_WELLNESS_NOTES[badge.breadcrumb_key]}</div>
              )}
              <div className="account-profile__achievement-actions">
                <button
                  aria-pressed={onProfile}
                  className={onProfile ? 'is-equipped' : ''}
                  disabled={equippingSurface !== null}
                  onClick={() => void toggleBadge('profile_avatar', badge)}
                  type="button"
                >
                  <UserRound size={15} aria-hidden />
                  {onProfile ? '从头像取下' : '佩戴到头像'}
                </button>
                <button
                  aria-pressed={onFloating}
                  className={onFloating ? 'is-equipped' : ''}
                  disabled={equippingSurface !== null}
                  onClick={() => void toggleBadge('floating_avatar', badge)}
                  type="button"
                >
                  <Briefcase size={15} aria-hidden />
                  {onFloating ? '从悬浮球取下' : '佩戴到悬浮球'}
                </button>
              </div>
              {achievementMessage && (
                <div className="account-profile__achievement-message" role="status">{achievementMessage}</div>
              )}
            </section>
          </div>
        )
      })()}

      {logoutConfirmOpen && (
        <div
          className="account-profile__dialog-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !logoutPending) setLogoutConfirmOpen(false)
          }}
        >
          <section
            aria-describedby="logout-confirm-description"
            aria-labelledby="logout-confirm-title"
            aria-modal="true"
            className="account-profile__dialog"
            ref={logoutDialogRef}
            role="alertdialog"
          >
            <span className="account-profile__dialog-icon" aria-hidden="true"><LogOut size={22} /></span>
            <h2 id="logout-confirm-title">确认退出登录？</h2>
            <p id="logout-confirm-description">确定要退出当前账号吗？</p>
            {logoutError && <div className="account-profile__dialog-error" role="alert">{logoutError}</div>}
            <div className="account-profile__dialog-actions">
              <button disabled={logoutPending} onClick={() => setLogoutConfirmOpen(false)} ref={logoutCancelRef} type="button">取消</button>
              <button disabled={logoutPending} onClick={confirmLogout} type="button">
                {logoutPending ? '正在退出' : '确认退出'}
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  )
}

export default AccountProfile
