import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Calendar, CheckCircle2, Edit2, RefreshCw } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import TutorialLink, { TUTORIAL_URLS } from './TutorialLink'
import './DiaryPanel.css'

type PeriodType = 'daily' | 'weekly' | 'monthly'

interface DiaryContent {
  title?: string
  summary?: string
  markdown?: string
  work_outputs?: string[]
  problems_solved?: string[]
  next_plan?: string[]
  timeline?: Array<{
    timeline_id?: number
    time?: string
    duration_minutes?: number | null
    summary?: string
    category?: string
  }>
  source_dates?: string[]
}

interface DiaryEntry {
  id: number
  period_type: PeriodType
  period_start: string
  period_end: string
  diary_date: string
  content: DiaryContent
  source_timeline_ids: number[]
  source_diary_ids: number[]
  generation_status: string
  is_system_generated: boolean
  created_at: string
  updated_at: string
}

interface LegacyProfileEntry {
  id: number
  snapshot_type: PeriodType
  snapshot_date: string
  content: DiaryContent
  is_system_generated: boolean
  created_at: string
  updated_at: string
}

interface ScheduledTaskSummary {
  id: number
  name: string
  user_instruction?: string
  template_id?: string | null
  enabled: boolean
}

const PERIOD_LABELS: Record<PeriodType, string> = {
  daily: '日记',
  weekly: '周记',
  monthly: '月记',
}

const RECENT_DAILY_CATCHUP_DAYS = 2
const AUTO_REFRESH_POLL_INTERVAL_MS = 3000
const AUTO_REFRESH_MAX_POLLS = 6

const DiaryPanel: React.FC = () => {
  const apiBaseUrl = useAppStore(state => state.apiBaseUrl)
  const [diaries, setDiaries] = useState<DiaryEntry[]>([])
  const [selectedType, setSelectedType] = useState<PeriodType>('daily')
  const [selectedDate, setSelectedDate] = useState('')
  const [selectedDiary, setSelectedDiary] = useState<DiaryEntry | null>(null)
  const [isEditing, setIsEditing] = useState(false)
  const [editMarkdown, setEditMarkdown] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [autoRefreshMessage, setAutoRefreshMessage] = useState<string | null>(null)
  const autoRefreshKeyRef = useRef<string | null>(null)

  useEffect(() => {
    void fetchDiaries()
  }, [selectedType, selectedDate, apiBaseUrl])

  const applyDiaryEntries = (data: DiaryEntry[]) => {
    setDiaries(data)
    setSelectedDiary(current => {
      if (data.length === 0) return null
      if (current && data.some(item => item.id === current.id)) return current
      return data[0]
    })
  }

  const fetchDiaries = async (options: { skipAutoRefresh?: boolean } = {}) => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchDiaryEntries(apiBaseUrl, selectedType, selectedDate || undefined)
      applyDiaryEntries(data)
      if (!options.skipAutoRefresh && selectedType === 'daily' && !selectedDate) {
        void maybeRefreshRecentDailyDiaries(data)
      }
    } catch (err) {
      console.error('获取日记失败:', err)
      setError('日记加载失败')
    } finally {
      setLoading(false)
    }
  }

  const maybeRefreshRecentDailyDiaries = async (data: DiaryEntry[]) => {
    const missingDates = findMissingRecentDailyDates(data)
    if (missingDates.length === 0) {
      setAutoRefreshMessage(null)
      return
    }

    const refreshKey = `${apiBaseUrl}:${missingDates.join('|')}`
    if (autoRefreshKeyRef.current === refreshKey) return
    autoRefreshKeyRef.current = refreshKey

    try {
      const triggered = await triggerDailyJournalTask(apiBaseUrl)
      if (!triggered) {
        setAutoRefreshMessage(null)
        return
      }

      setAutoRefreshMessage('正在后台补齐最近日记...')
      for (let attempt = 0; attempt < AUTO_REFRESH_MAX_POLLS; attempt += 1) {
        await delay(AUTO_REFRESH_POLL_INTERVAL_MS)
        const nextData = await fetchDiaryEntries(apiBaseUrl, 'daily')
        applyDiaryEntries(nextData)
        if (findMissingRecentDailyDates(nextData).length === 0) {
          setAutoRefreshMessage('最近日记已更新')
          window.setTimeout(() => setAutoRefreshMessage(null), 2500)
          return
        }
      }

      setAutoRefreshMessage('后台日记任务已触发，生成完成后会显示在列表中')
    } catch (err) {
      console.warn('自动触发日记更新失败:', err)
      setAutoRefreshMessage(null)
      autoRefreshKeyRef.current = null
    }
  }

  const handleEdit = () => {
    if (!selectedDiary) return
    setEditMarkdown(selectedDiary.content.markdown || '')
    setIsEditing(true)
  }

  const handleSave = async () => {
    if (!selectedDiary) return
    const nextContent = {
      ...selectedDiary.content,
      markdown: editMarkdown,
      summary: firstMeaningfulLine(editMarkdown) || selectedDiary.content.summary,
    }

    const res = await fetch(`${apiBaseUrl}/api/diaries/${selectedDiary.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: nextContent }),
    })
    if (!res.ok) {
      setError('保存失败')
      return
    }
    setIsEditing(false)
    await fetchDiaries()
  }

  const periodText = useMemo(() => {
    if (!selectedDiary) return ''
    if (selectedDiary.period_start === selectedDiary.period_end) return selectedDiary.diary_date
    return `${selectedDiary.period_start} 至 ${selectedDiary.period_end}`
  }, [selectedDiary])

  return (
    <div className="diary-panel">
      <header className="diary-card diary-header">
        <div className="tutorial-title-row">
          <h1 className="diary-title">工作日记</h1>
          <TutorialLink url={TUTORIAL_URLS.diary} />
        </div>
      </header>

      <section className="diary-card diary-toolbar" aria-label="日记筛选">
        <div className="diary-toolbar__filters">
          <div className="diary-segmented-control" role="tablist" aria-label="日记周期">
            {(['daily', 'weekly', 'monthly'] as PeriodType[]).map((type) => (
              <button
                aria-selected={selectedType === type}
                className={`diary-tab${selectedType === type ? ' diary-tab--active' : ''}`}
                key={type}
                onClick={() => {
                  setSelectedType(type)
                  setIsEditing(false)
                }}
                role="tab"
                type="button"
              >
                {PERIOD_LABELS[type]}
              </button>
            ))}
          </div>
          <label className="diary-date-field">
            <span>日期</span>
            <input
              aria-label="选择日记日期"
              className="diary-date-input"
              type="date"
              value={selectedDate}
              onChange={(event) => {
                setSelectedDate(event.target.value)
                setIsEditing(false)
              }}
            />
          </label>
          {selectedDate && (
            <button
              className="diary-button"
              onClick={() => {
                setSelectedDate('')
                setIsEditing(false)
              }}
              type="button"
            >
              清除日期
            </button>
          )}
        </div>
        <div className="diary-toolbar__actions">
          {autoRefreshMessage && (
            <span className="diary-refresh-status" role="status">
              {autoRefreshMessage}
            </span>
          )}
          <button
            className="diary-button diary-button--icon"
            disabled={loading}
            onClick={() => fetchDiaries({ skipAutoRefresh: true })}
            type="button"
          >
            <RefreshCw className={loading ? 'diary-spin' : ''} size={15} />
            刷新
          </button>
        </div>
      </section>

      {loading ? (
        <StatusBlock kind="loading" text="加载中..." />
      ) : error ? (
        <StatusBlock kind="error" text={error} />
      ) : diaries.length === 0 ? (
        <StatusBlock text={selectedDate ? '该日期暂无日记' : '暂无日记'} />
      ) : (
        <div className="diary-layout">
          <aside className="diary-card diary-list-panel">
            <h2 className="diary-section-heading">
              <Calendar size={16} />
              {PERIOD_LABELS[selectedType]}列表
            </h2>
            <div className="diary-entry-list">
              {diaries.map((diary) => (
                <button
                  aria-current={selectedDiary?.id === diary.id ? 'true' : undefined}
                  className={`diary-entry${selectedDiary?.id === diary.id ? ' diary-entry--active' : ''}`}
                  key={diary.id}
                  onClick={() => {
                    setSelectedDiary(diary)
                    setIsEditing(false)
                  }}
                  type="button"
                >
                  <span className="diary-entry__date">
                    {diary.diary_date}
                  </span>
                  <span className="diary-entry__meta">
                    <CheckCircle2 size={13} />
                    {diary.is_system_generated ? '系统生成' : '用户编辑'}
                  </span>
                </button>
              ))}
            </div>
          </aside>

          {selectedDiary && (
            <section className="diary-card diary-detail">
              <div className="diary-detail__header">
                <div className="diary-detail__title-group">
                  <h2 className="diary-detail__title">
                    {selectedDiary.content.title || `${selectedDiary.diary_date} ${PERIOD_LABELS[selectedType]}`}
                  </h2>
                  <div className="diary-detail__period">{periodText}</div>
                </div>
                {!isEditing && (
                  <button
                    className="diary-button diary-button--icon"
                    onClick={handleEdit}
                    type="button"
                  >
                    <Edit2 size={15} />
                    编辑
                  </button>
                )}
              </div>

              {isEditing ? (
                <div className="diary-editor">
                  <textarea
                    aria-label="编辑日记内容"
                    className="diary-textarea"
                    value={editMarkdown}
                    onChange={(event) => setEditMarkdown(event.target.value)}
                  />
                  <div className="diary-editor__actions">
                    <button className="diary-button" onClick={() => setIsEditing(false)} type="button">取消</button>
                    <button className="diary-button diary-button--primary" onClick={handleSave} type="button">保存</button>
                  </div>
                </div>
              ) : (
                <DiaryContentView diary={selectedDiary} />
              )}
            </section>
          )}
        </div>
      )}
    </div>
  )
}

const DiaryContentView: React.FC<{ diary: DiaryEntry }> = ({ diary }) => {
  const content = diary.content || {}
  const hasStructured = Boolean(
    content.work_outputs?.length || content.problems_solved?.length
      || (diary.period_type !== 'daily' && content.next_plan?.length)
      || content.timeline?.length
  )

  if (!hasStructured && content.markdown) {
    return <MarkdownBlock markdown={content.markdown} />
  }

  return (
    <div className="diary-content">
      <SectionList title="工作产出" items={content.work_outputs || []} />
      <SectionList title="问题与解决" items={content.problems_solved || []} />
      {diary.period_type !== 'daily' && (
        <SectionList title="后续计划" items={content.next_plan || []} />
      )}
      {content.timeline && content.timeline.length > 0 && (
        <section className="diary-content-section">
          <h3 className="diary-content-section__title">来源线索</h3>
          <div className="diary-timeline">
            {content.timeline.map((item, index) => (
              <article className="diary-timeline__item" key={`${item.timeline_id || index}-${item.time || ''}`}>
                <div className="diary-timeline__meta">
                  {item.time || '未知时间'}{item.duration_minutes ? ` · ${item.duration_minutes} 分钟` : ''}{item.category ? ` · ${item.category}` : ''}
                </div>
                <div className="diary-timeline__summary">{item.summary}</div>
              </article>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

const SectionList: React.FC<{ title: string; items: string[] }> = ({ title, items }) => (
  <section className="diary-content-section">
    <h3 className="diary-content-section__title">{title}</h3>
    {items.length === 0 ? (
      <div className="diary-content-section__empty">暂无记录</div>
    ) : (
      <ul className="diary-content-list">
        {items.map((item, index) => (
          <li key={`${title}-${index}`}>{item}</li>
        ))}
      </ul>
    )}
  </section>
)

const MarkdownBlock: React.FC<{ markdown: string }> = ({ markdown }) => (
  <pre className="diary-markdown">
    {markdown}
  </pre>
)

const StatusBlock: React.FC<{ text: string; kind?: 'empty' | 'loading' | 'error' }> = ({
  text,
  kind = 'empty',
}) => (
  <div className={`diary-card diary-status diary-status--${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
    {kind === 'loading' && (
      <div className="diary-status__skeleton" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    )}
    <span className="diary-status__text">{text}</span>
  </div>
)

function firstMeaningfulLine(markdown: string): string {
  for (const rawLine of markdown.split('\n')) {
    const line = rawLine.replace(/^#+\s*/, '').replace(/^[-*]\s*/, '').trim()
    if (line) return line.slice(0, 160)
  }
  return ''
}

async function fetchDiaryEntries(apiBaseUrl: string, periodType: PeriodType, diaryDate?: string): Promise<DiaryEntry[]> {
  const params = new URLSearchParams({
    period_type: periodType,
    limit: diaryDate ? '1' : '20',
  })
  if (diaryDate) params.set('diary_date', diaryDate)
  const diaryUrl = `${apiBaseUrl}/api/diaries?${params.toString()}`
  const diaryResp = await fetch(diaryUrl)
  if (diaryResp.ok) {
    return await diaryResp.json() as DiaryEntry[]
  }

  const diaryErrorBody = await readErrorBody(diaryResp)
  if (!isCompatiblyMissingDiaryBackend(diaryResp.status, diaryErrorBody)) {
    throw new Error(`diaries fetch failed: ${diaryResp.status}`)
  }

  const legacyResp = await fetch(`${apiBaseUrl}/api/profiles?type=${periodType}&limit=${diaryDate ? 500 : 20}`)
  if (legacyResp.ok) {
    const legacy = await legacyResp.json() as LegacyProfileEntry[]
    return legacy
      .filter(profile => !diaryDate || profile.snapshot_date === diaryDate)
      .map(profileToDiary)
  }

  const legacyErrorBody = await readErrorBody(legacyResp)
  if (isCompatiblyMissingDiaryBackend(legacyResp.status, legacyErrorBody)) {
    return []
  }

  throw new Error(`legacy profiles fetch failed: ${legacyResp.status}`)
}

async function readErrorBody(resp: Response): Promise<string> {
  try {
    return await resp.text()
  } catch {
    return ''
  }
}

function isCompatiblyMissingDiaryBackend(status: number, body: string): boolean {
  if (status === 404) return true
  return status === 500 && /no such table:\s*(diaries|user_profiles)/i.test(body)
}

function profileToDiary(profile: LegacyProfileEntry): DiaryEntry {
  return {
    id: profile.id,
    period_type: profile.snapshot_type,
    period_start: profile.snapshot_date,
    period_end: profile.snapshot_date,
    diary_date: profile.snapshot_date,
    content: profile.content || {},
    source_timeline_ids: [],
    source_diary_ids: [],
    generation_status: 'ready',
    is_system_generated: profile.is_system_generated,
    created_at: profile.created_at,
    updated_at: profile.updated_at,
  }
}

function findMissingRecentDailyDates(entries: DiaryEntry[]): string[] {
  const availableDates = new Set(
    entries
      .filter(entry => entry.period_type === 'daily')
      .map(entry => entry.diary_date),
  )
  const recentDates = recentCompletedLocalDates(RECENT_DAILY_CATCHUP_DAYS)
  const latestCompletedDate = recentDates[recentDates.length - 1]
  if (availableDates.has(latestCompletedDate)) return []
  return recentDates.filter(date => !availableDates.has(date))
}

function recentCompletedLocalDates(days: number, now = new Date()): string[] {
  const dates: string[] = []
  for (let offset = days; offset >= 1; offset -= 1) {
    const day = new Date(now)
    day.setHours(0, 0, 0, 0)
    day.setDate(day.getDate() - offset)
    dates.push(formatLocalDate(day))
  }
  return dates
}

function formatLocalDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

async function triggerDailyJournalTask(apiBaseUrl: string): Promise<boolean> {
  const tasksResp = await fetch(`${apiBaseUrl}/api/tasks`)
  if (!tasksResp.ok) return false

  const data = await tasksResp.json() as { tasks?: ScheduledTaskSummary[] }
  const task = (data.tasks || []).find(task => task.enabled && isDailyJournalTask(task))
  if (!task) return false

  const triggerResp = await fetch(`${apiBaseUrl}/api/tasks/${task.id}/trigger`, { method: 'POST' })
  return triggerResp.ok
}

function isDailyJournalTask(task: ScheduledTaskSummary): boolean {
  if (task.template_id === 'daily_journal') return true
  const text = `${task.name || ''} ${task.user_instruction || ''}`.toLowerCase()
  return text.includes('工作日记') || text.includes('daily journal')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

export default DiaryPanel
