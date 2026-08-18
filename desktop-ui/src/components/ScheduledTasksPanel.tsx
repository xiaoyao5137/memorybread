import React, { useEffect, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { emit } from '@tauri-apps/api/event'
import type { NotificationChannel, ScheduledTask, TaskExecution, TaskTemplate } from '../types'
import { useAppStore } from '../store/useAppStore'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { BUILTIN_TEMPLATES, CATEGORY_COLORS, groupTemplatesByCategory } from '../data/taskTemplates'
import TutorialLink, { TUTORIAL_URLS } from './TutorialLink'
import { MentionHighlightTextarea } from './MentionHighlightField'
import {
  CREATION_SKILL_AGENT_OPTIONS,
  CREATION_SKILL_TOOL_OPTIONS,
  listLocalCreationSkills,
} from '../utils/creationSkills'
import {
  FLOATING_ASSIST_ENABLED_KEY,
  readFloatingAssistAutoTaskConfig,
  writeFloatingAssistAutoTaskConfig,
  type FloatingAssistAutoTaskAppTarget,
  type FloatingAssistAutoTaskConfig,
} from '../utils/floatingAssistAutoTask'

const API = 'http://localhost:7070'

type TaskForm = {
  name: string
  user_instruction: string
  executor_kind: ScheduledTask['executor_kind']
  notification_channel_ids: number[]
}

type ChannelForm = {
  name: string
  channel_type: NotificationChannel['channel_type']
  webhook_url: string
}

const emptyTaskForm = (): TaskForm => ({
  name: '',
  user_instruction: '',
  executor_kind: 'consult',
  notification_channel_ids: [],
})

function formatTs(ms: number | null): string {
  if (!ms) return '—'
  return new Date(ms).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

// ── 执行频率的友好交互：cron 表达式与可视化配置互转 ─────────────────────────
type ScheduleKind = 'daily' | 'weekly' | 'weekdays' | 'monthly' | 'custom'

type ScheduleState = {
  kind: ScheduleKind
  time: string              // HH:MM
  weekdays: number[]        // Vixie 约定：0=周日，1=周一 … 6=周六
  dayOfMonth: number
  customExpression: string
}

const SCHEDULE_KIND_LABELS: Record<ScheduleKind, string> = {
  daily: '每天',
  weekly: '每周',
  weekdays: '工作日',
  monthly: '每月',
  custom: '高级',
}

const WEEKDAY_NAMES = ['日', '一', '二', '三', '四', '五', '六']
const WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0]

const defaultSchedule = (): ScheduleState => ({
  kind: 'daily',
  time: '20:00',
  weekdays: [1],
  dayOfMonth: 1,
  customExpression: '0 20 * * *',
})

// 统一成五段 Vixie 格式（分 时 日 月 周）；六段格式把 cron crate 的星期平移回来
function cronToFiveFields(expr: string): string[] | null {
  const fields = expr.trim().split(/\s+/)
  if (fields.length === 5) return fields
  if (fields.length === 6 && fields[0] === '0') {
    const shiftDay = (token: string): string => {
      const match = token.match(/^(\d+)(?:-(\d+))?$/)
      if (!match) return token
      const toVixie = (day: string) => String((Number(day) - 1) % 7)
      return match[2] ? `${toVixie(match[1])}-${toVixie(match[2])}` : toVixie(match[1])
    }
    return [...fields.slice(1, 5), fields[5].split(',').map(shiftDay).join(',')]
  }
  return null
}

function expandWeekdays(dow: string): number[] | null {
  const days = new Set<number>()
  for (const item of dow.split(',')) {
    const match = item.match(/^(\d+)(?:-(\d+))?$/)
    if (!match) return null
    const toVixie = (value: number) => (value === 7 ? 0 : value)
    const start = toVixie(Number(match[1]))
    const end = toVixie(match[2] !== undefined ? Number(match[2]) : Number(match[1]))
    if (start > end) return null
    for (let day = start; day <= end; day += 1) days.add(day)
  }
  return days.size > 0 ? Array.from(days) : null
}

function parseCronToSchedule(expr: string): ScheduleState {
  const fallback: ScheduleState = { ...defaultSchedule(), kind: 'custom', customExpression: expr }
  const five = cronToFiveFields(expr)
  if (!five) return fallback
  const [minute, hour, dom, month, dow] = five
  if (!/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour) || month !== '*') return fallback
  const base = {
    time: `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`,
    weekdays: [1],
    dayOfMonth: 1,
    customExpression: expr,
  }
  if (dom === '*' && dow === '*') return { ...base, kind: 'daily' }
  if (dom === '*') {
    if (dow === '1-5') return { ...base, kind: 'weekdays' }
    const days = expandWeekdays(dow)
    if (days) return { ...base, kind: 'weekly', weekdays: days }
    return fallback
  }
  if (/^\d{1,2}$/.test(dom) && dow === '*') {
    return { ...base, kind: 'monthly', dayOfMonth: Number(dom) }
  }
  return fallback
}

function buildCronFromSchedule(state: ScheduleState): string | null {
  if (state.kind === 'custom') return state.customExpression.trim() || null
  const match = state.time.match(/^(\d{1,2}):(\d{2})$/)
  if (!match) return null
  const head = `${Number(match[2])} ${Number(match[1])}`
  switch (state.kind) {
    case 'daily':
      return `${head} * * *`
    case 'weekdays':
      return `${head} * * 1-5`
    case 'weekly': {
      if (state.weekdays.length === 0) return null
      const days = [...state.weekdays].sort((a, b) => (a === 0 ? 7 : a) - (b === 0 ? 7 : b))
      return `${head} * * ${days.join(',')}`
    }
    case 'monthly':
      return `${head} ${state.dayOfMonth} * *`
    default:
      return null
  }
}

function describeCron(expr: string): string {
  const five = cronToFiveFields(expr)
  if (!five) return expr
  const [minute, hour, dom, month, dow] = five
  if (!/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour) || month !== '*') return expr
  const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
  if (dom === '*' && dow === '*') return `每天 ${time}`
  if (dom === '*') {
    if (dow === '1-5') return `工作日 ${time}`
    const days = expandWeekdays(dow)
    if (days) {
      const sorted = [...days].sort((a, b) => (a === 0 ? 7 : a) - (b === 0 ? 7 : b))
      return `每周${sorted.map(day => WEEKDAY_NAMES[day]).join('、')} ${time}`
    }
    return expr
  }
  if (/^\d{1,2}$/.test(dom) && dow === '*') return `每月${Number(dom)}日 ${time}`
  return expr
}

function channelTypeLabel(type: NotificationChannel['channel_type']): string {
  return {
    feishu: '飞书',
    dingtalk: '钉钉',
    wecom: '企业微信',
    webhook: '通用 Webhook',
  }[type]
}

function webhookHost(value: string): string {
  try {
    return `${new URL(value).host}/••••`
  } catch {
    return '已配置'
  }
}

// ── 子组件：任务卡片 ─────────────────────────────────────────────────────────
const TaskCard: React.FC<{
  task: ScheduledTask
  onToggle: (id: number, enabled: boolean) => void
  onTrigger: (id: number) => void
  onEdit: (task: ScheduledTask) => void
  onDelete: (id: number) => void
  onViewResult: (task: ScheduledTask) => void
}> = ({ task, onToggle, onTrigger, onEdit, onDelete, onViewResult }) => {
  const statusColor = task.last_run_status === 'success' ? '#34C759'
    : task.last_run_status === 'failed' ? '#FF3B30' : '#AEAEB2'

  return (
    <div style={{
      background: 'var(--mb-bg-card)', borderRadius: 12, padding: '14px 16px',
      border: '1px solid var(--mb-border)', marginBottom: 10,
      opacity: task.enabled ? 1 : 0.5,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        {/* 启用开关 */}
        <button
          onClick={() => onToggle(task.id, !task.enabled)}
          style={{
            width: 36, height: 20, borderRadius: 10, border: 'none', cursor: 'pointer',
            background: task.enabled ? '#007AFF' : '#E5E5EA', flexShrink: 0, marginTop: 2,
            position: 'relative', transition: 'background 0.2s',
          }}
          title={task.enabled ? '点击禁用' : '点击启用'}
        >
          <span style={{
            position: 'absolute', top: 2, left: task.enabled ? 18 : 2,
            width: 16, height: 16, borderRadius: '50%', background: 'white',
            transition: 'left 0.2s', display: 'block',
          }} />
        </button>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--mb-text-primary)' }}>{task.name}</span>
            <span style={{
              fontSize: 11, padding: '1px 6px', borderRadius: 4,
              background: 'rgba(0,122,255,0.08)', color: '#007AFF',
            }}>{describeCron(task.cron_expression)}</span>
            {task.is_builtin && (
              <span style={{
                fontSize: 11, padding: '1px 6px', borderRadius: 4,
                background: 'rgba(181,122,43,0.1)', color: 'var(--mb-brand-text)',
              }}>内置日记</span>
            )}
            <span style={{
              fontSize: 11, padding: '1px 6px', borderRadius: 4,
              background: task.executor_kind === 'creation' ? 'rgba(88,86,214,0.1)' : 'rgba(142,142,147,0.12)',
              color: task.executor_kind === 'creation' ? '#5856D6' : '#6E6E73',
            }}>{task.executor_kind === 'creation' ? '创作智能体' : '咨询智能体'}</span>
            {task.last_run_status && (
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: statusColor, flexShrink: 0 }} />
            )}
          </div>
          <p style={{ fontSize: 12, color: 'var(--mb-text-secondary)', margin: 0, lineHeight: 1.4,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {task.user_instruction}
          </p>
          <div style={{ display: 'flex', gap: 12, marginTop: 6, fontSize: 11, color: 'var(--mb-text-faint)' }}>
            <span>执行 {task.run_count} 次</span>
            {task.last_run_at && <span>上次 {formatTs(task.last_run_at)}</span>}
            {task.next_run_at && <span>下次 {formatTs(task.next_run_at)}</span>}
          </div>
        </div>

        {/* 操作按钮 */}
        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
          <button onClick={() => onViewResult(task)} style={btnStyle('#007AFF')} title="查看历史">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
            </svg>
          </button>
          <button onClick={() => onEdit(task)} style={btnStyle('#8A5A1F')} title="编辑">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>
            </svg>
          </button>
          <button onClick={() => onTrigger(task.id)} style={btnStyle('#34C759')} title="立即执行">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
          </button>
          {task.can_delete && (
            <button onClick={() => onDelete(task.id)} style={btnStyle('#FF3B30')} title="删除">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>
              </svg>
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── 主组件 ───────────────────────────────────────────────────────────────────
const ScheduledTasksPanel: React.FC = () => {
  const { apiBaseUrl, setWindowMode, setCreationHistoryOpenTarget } = useAppStore()
  const base = apiBaseUrl || API

  const [tasks, setTasks] = useState<ScheduledTask[]>([])
  const [channels, setChannels] = useState<NotificationChannel[]>([])
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState<'list' | 'create' | 'edit' | 'templates' | 'result' | 'channels'>('list')
  const [selectedTask, setSelectedTask] = useState<ScheduledTask | null>(null)
  const [executions, setExecutions] = useState<TaskExecution[]>([])
  const [toast, setToast] = useState<string | null>(null)
  const [autoTaskExpanded, setAutoTaskExpanded] = useState(false)
  const [autoTaskConfig, setAutoTaskConfig] = useState<FloatingAssistAutoTaskConfig>(readFloatingAssistAutoTaskConfig)
  const [autoTaskDraft, setAutoTaskDraft] = useState(() => readFloatingAssistAutoTaskConfig())
  const [autoTaskAppDraft, setAutoTaskAppDraft] = useState<FloatingAssistAutoTaskAppTarget>({ bundleId: '', appName: '' })
  const [triggerWordDraft, setTriggerWordDraft] = useState('')
  const triggerWordImeGuard = useImeCompositionGuard<HTMLInputElement>()

  // 执行指令的 @ 提及：候选 = 工具 + Agent + 已安装创作技能，交互与创作技能编辑器一致。
  const [installedSkillTitles, setInstalledSkillTitles] = useState<string[]>([])
  const [instructionMentionQuery, setInstructionMentionQuery] = useState<string | null>(null)
  const [instructionMentionActiveIndex, setInstructionMentionActiveIndex] = useState(0)
  const instructionTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  const instructionImeGuard = useImeCompositionGuard<HTMLTextAreaElement>()

  const [form, setForm] = useState<TaskForm>(emptyTaskForm)
  const [schedule, setSchedule] = useState<ScheduleState>(defaultSchedule)
  const [channelForm, setChannelForm] = useState<ChannelForm>({
    name: '',
    channel_type: 'feishu',
    webhook_url: '',
  })

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 3000)
  }

  const persistAutoTaskConfig = async (next: FloatingAssistAutoTaskConfig) => {
    const saved = writeFloatingAssistAutoTaskConfig(next)
    setAutoTaskConfig(saved)
    setAutoTaskDraft(saved)
    try {
      if (saved.enabled) {
        localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, 'true')
        await invoke('set_floating_assist_menu_state', { enabled: true })
        await invoke('set_floating_assist_visible', { enabled: true })
      }
      await invoke('set_floating_assist_auto_task_menu_state', {
        checked: saved.enabled,
        enabled: localStorage.getItem(FLOATING_ASSIST_ENABLED_KEY) === 'true',
      })
      await emit('floating-assist-auto-task-changed', saved)
    } catch {
      // 浏览器预览或 Tauri runtime 不可用时，本地配置仍然生效。
    }
  }

  const handleAutoTaskToggle = async () => {
    await persistAutoTaskConfig({
      ...autoTaskConfig,
      enabled: !autoTaskConfig.enabled,
    })
    showToast(!autoTaskConfig.enabled ? '自动识别任务已开启' : '自动识别任务已关闭')
  }

  const handleAutoTaskSave = async () => {
    await persistAutoTaskConfig({
      ...autoTaskConfig,
      appTargets: autoTaskDraft.appTargets,
      triggerWords: autoTaskDraft.triggerWords,
    })
    showToast('自动识别任务配置已保存')
  }

  const handleAddAutoTaskApp = () => {
    const bundleId = autoTaskAppDraft.bundleId.trim()
    const appName = autoTaskAppDraft.appName.trim()
    if (!bundleId && !appName) {
      showToast('请填写 Bundle ID 或应用名称')
      return
    }
    setAutoTaskDraft(value => ({
      ...value,
      appTargets: [...value.appTargets, { bundleId, appName }],
    }))
    setAutoTaskAppDraft({ bundleId: '', appName: '' })
  }

  const handleRemoveAutoTaskApp = (index: number) => {
    setAutoTaskDraft(value => ({
      ...value,
      appTargets: value.appTargets.filter((_, itemIndex) => itemIndex !== index),
    }))
  }

  const handleAddTriggerWord = () => {
    const word = triggerWordDraft.trim()
    if (!word) return
    setAutoTaskDraft(value => value.triggerWords.some(item => item.toLocaleLowerCase() === word.toLocaleLowerCase())
      ? value
      : { ...value, triggerWords: [...value.triggerWords, word] })
    setTriggerWordDraft('')
  }

  const handleRemoveTriggerWord = (word: string) => {
    setAutoTaskDraft(value => ({
      ...value,
      triggerWords: value.triggerWords.filter(item => item !== word),
    }))
  }

  const loadTasks = async () => {
    setLoading(true)
    try {
      const res = await fetch(`${base}/api/tasks`)
      if (!res.ok) throw new Error('加载任务失败')
      const data = await res.json()
      setTasks(data.tasks || [])
    } catch {
      showToast('加载失败')
    } finally {
      setLoading(false)
    }
  }

  const loadChannels = async () => {
    try {
      const res = await fetch(`${base}/api/notification-channels`)
      if (!res.ok) throw new Error('加载消息渠道失败')
      const data = await res.json()
      setChannels(data.channels || [])
    } catch {
      showToast('消息渠道加载失败')
    }
  }

  useEffect(() => {
    void loadTasks()
    void loadChannels()
  }, [base])

  useEffect(() => {
    let cancelled = false
    listLocalCreationSkills(base, { installed: true })
      .then(skills => {
        if (cancelled) return
        setInstalledSkillTitles(
          skills.map(skill => skill.title.trim()).filter(Boolean),
        )
      })
      .catch(() => {
        // 创作技能服务不可用时，@ 候选仅保留内置工具/Agent。
      })
    return () => { cancelled = true }
  }, [base])

  const instructionMentionOptions = [
    ...CREATION_SKILL_TOOL_OPTIONS.map(option => ({
      id: `tool:${option.id}`,
      label: option.label,
      description: '工具',
    })),
    ...CREATION_SKILL_AGENT_OPTIONS.map(option => ({
      id: `agent:${option.id}`,
      label: option.label,
      description: 'Agent',
    })),
    ...installedSkillTitles.map(title => ({
      id: `skill:${title}`,
      label: title,
      description: '创作技能',
    })),
  ]

  const filteredInstructionMentionOptions = instructionMentionQuery === null
    ? []
    : instructionMentionOptions.filter(option => !instructionMentionQuery
      || option.label.toLowerCase().includes(instructionMentionQuery.toLowerCase()))

  const refreshInstructionMention = (value: string, element: HTMLTextAreaElement) => {
    const caret = element.selectionStart ?? value.length
    // 与创作技能编辑器相同：光标前存在未完成的 @ 查询才弹出选择器。
    const active = value.slice(0, caret).match(/@([^\s@]{0,40})$/)
    if (active) {
      setInstructionMentionQuery(active[1])
      setInstructionMentionActiveIndex(0)
    } else {
      setInstructionMentionQuery(null)
    }
  }

  const insertInstructionMention = (option: { label: string }) => {
    if (instructionMentionQuery === null) return
    const element = instructionTextareaRef.current
    const value = form.user_instruction
    const caret = element?.selectionStart ?? value.length
    // 连 @ 符号一起替换成完整提及 + 尾随空格，避免与后续文字粘连。
    const start = Math.max(0, caret - instructionMentionQuery.length - 1)
    const inserted = `@${option.label} `
    const next = value.slice(0, start) + inserted + value.slice(caret)
    setForm(f => ({ ...f, user_instruction: next }))
    setInstructionMentionQuery(null)
    setInstructionMentionActiveIndex(0)
    window.requestAnimationFrame(() => {
      const target = instructionTextareaRef.current
      if (!target) return
      target.focus()
      const position = start + inserted.length
      target.setSelectionRange(position, position)
    })
  }

  const handleInstructionKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (instructionMentionQuery === null
      || filteredInstructionMentionOptions.length === 0
      || instructionImeGuard.isImeEvent(event)) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setInstructionMentionActiveIndex(index => (index + 1) % filteredInstructionMentionOptions.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setInstructionMentionActiveIndex(index =>
        (index - 1 + filteredInstructionMentionOptions.length) % filteredInstructionMentionOptions.length)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      insertInstructionMention(
        filteredInstructionMentionOptions[
          Math.min(instructionMentionActiveIndex, filteredInstructionMentionOptions.length - 1)
        ],
      )
    } else if (event.key === 'Escape') {
      event.preventDefault()
      setInstructionMentionQuery(null)
    }
  }

  const handleOpenCreationHistory = (historyId: number) => {
    setCreationHistoryOpenTarget(historyId)
    setWindowMode('creation')
  }

  const responseError = async (res: Response, fallback: string) => {
    try {
      const error = await res.json()
      return error.message || error.error || fallback
    } catch {
      return fallback
    }
  }

  const handleToggle = async (id: number, enabled: boolean) => {
    const res = await fetch(`${base}/api/tasks/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
    if (!res.ok) {
      showToast(await responseError(res, '更新任务失败'))
      return
    }
    void loadTasks()
  }

  const handleTrigger = async (id: number) => {
    const res = await fetch(`${base}/api/tasks/${id}/trigger`, { method: 'POST' })
    if (!res.ok) {
      showToast(await responseError(res, '触发任务失败'))
      return
    }
    showToast('任务已触发，正在执行...')
    setTimeout(loadTasks, 2000)
  }

  const handleDelete = async (id: number) => {
    if (!confirm('确认删除此任务？')) return
    const res = await fetch(`${base}/api/tasks/${id}`, { method: 'DELETE' })
    if (!res.ok) {
      showToast(await responseError(res, '删除任务失败'))
      return
    }
    showToast('任务已删除')
    void loadTasks()
  }

  const handleViewResult = async (task: ScheduledTask) => {
    setSelectedTask(task)
    const res = await fetch(`${base}/api/tasks/${task.id}/executions?limit=5`)
    if (!res.ok) {
      showToast(await responseError(res, '加载执行历史失败'))
      return
    }
    const data = await res.json()
    setExecutions(data.executions || [])
    setView('result')
  }

  const handleEdit = (task: ScheduledTask) => {
    setSelectedTask(task)
    setForm({
      name: task.name,
      user_instruction: task.user_instruction,
      executor_kind: task.executor_kind === 'creation' ? 'creation' : 'consult',
      notification_channel_ids: task.notification_channel_ids || [],
    })
    setSchedule(parseCronToSchedule(task.cron_expression))
    setView('edit')
  }

  const validateSchedule = (): string | null => {
    if (!form.name || !form.user_instruction) return '请填写所有字段'
    if (schedule.kind === 'weekly' && schedule.weekdays.length === 0) return '请至少选择一天'
    if (!buildCronFromSchedule(schedule)) return '请完善执行频率设置'
    return null
  }

  const handleCreate = async () => {
    const error = validateSchedule()
    if (error) {
      showToast(error)
      return
    }
    try {
      const res = await fetch(`${base}/api/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, cron_expression: buildCronFromSchedule(schedule) }),
      })
      if (!res.ok) {
        showToast(await responseError(res, '创建失败'))
        return
      }
      showToast('任务创建成功')
      setForm(emptyTaskForm())
      setSchedule(defaultSchedule())
      setView('list')
      void loadTasks()
    } catch {
      showToast('创建失败')
    }
  }

  const handleUpdate = async () => {
    if (!selectedTask) return
    const error = validateSchedule()
    if (error) {
      showToast(error)
      return
    }
    try {
      const res = await fetch(`${base}/api/tasks/${selectedTask.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, cron_expression: buildCronFromSchedule(schedule) }),
      })
      if (!res.ok) {
        showToast(await responseError(res, '保存失败'))
        return
      }
      showToast('任务已保存')
      setSelectedTask(null)
      setForm(emptyTaskForm())
      setSchedule(defaultSchedule())
      setView('list')
      void loadTasks()
    } catch {
      showToast('保存失败')
    }
  }

  const handleUseTemplate = (tpl: TaskTemplate) => {
    setForm({
      name: tpl.name,
      user_instruction: tpl.user_instruction,
      executor_kind: 'consult',
      notification_channel_ids: [],
    })
    setSchedule(tpl.cron.trim() ? parseCronToSchedule(tpl.cron) : defaultSchedule())
    setView('create')
  }

  const toggleFormChannel = (channelId: number) => {
    setForm(value => ({
      ...value,
      notification_channel_ids: value.notification_channel_ids.includes(channelId)
        ? value.notification_channel_ids.filter(id => id !== channelId)
        : [...value.notification_channel_ids, channelId],
    }))
  }

  const handleCreateChannel = async () => {
    if (!channelForm.name.trim() || !channelForm.webhook_url.trim()) {
      showToast('请填写渠道名称和 Webhook 地址')
      return
    }
    try {
      const res = await fetch(`${base}/api/notification-channels`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...channelForm, enabled: true }),
      })
      if (!res.ok) {
        showToast(await responseError(res, '创建渠道失败'))
        return
      }
      setChannelForm({ name: '', channel_type: 'feishu', webhook_url: '' })
      showToast('消息渠道已添加')
      void loadChannels()
    } catch {
      showToast('创建渠道失败')
    }
  }

  const handleToggleChannel = async (channel: NotificationChannel) => {
    const res = await fetch(`${base}/api/notification-channels/${channel.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !channel.enabled }),
    })
    if (!res.ok) {
      showToast(await responseError(res, '更新渠道失败'))
      return
    }
    void loadChannels()
  }

  const handleDeleteChannel = async (channel: NotificationChannel) => {
    if (!confirm(`确认删除消息渠道「${channel.name}」？`)) return
    const res = await fetch(`${base}/api/notification-channels/${channel.id}`, {
      method: 'DELETE',
    })
    if (!res.ok) {
      showToast(await responseError(res, '删除渠道失败'))
      return
    }
    setForm(value => ({
      ...value,
      notification_channel_ids: value.notification_channel_ids.filter(id => id !== channel.id),
    }))
    showToast('消息渠道已删除')
    void Promise.all([loadChannels(), loadTasks()])
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────────
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: 'var(--mb-bg-page)' }}>
      {/* Header */}
      <div style={{ padding: '16px 16px 0', background: 'var(--mb-bg-page)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <span className="tutorial-title-row" style={{ fontSize: 16, fontWeight: 600, color: 'var(--mb-text-primary)' }}>
            定时任务<TutorialLink url={TUTORIAL_URLS.tasks} />
          </span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => setView('channels')} style={{
              fontSize: 12, padding: '5px 10px', borderRadius: 8, border: '1px solid var(--mb-border-strong)',
              background: 'var(--mb-bg-input)', color: 'var(--mb-brand-text)', cursor: 'pointer',
            }}>消息渠道</button>
            <button onClick={() => setView('templates')} style={{
              fontSize: 12, padding: '5px 10px', borderRadius: 8, border: '1px solid var(--mb-border-strong)',
              background: 'var(--mb-bg-input)', color: '#007AFF', cursor: 'pointer',
            }}>模板库</button>
            <button onClick={() => {
              setSelectedTask(null)
              setForm(emptyTaskForm())
              setSchedule(defaultSchedule())
              setView('create')
            }} style={{
              fontSize: 12, padding: '5px 10px', borderRadius: 8, border: 'none',
              background: '#007AFF', color: 'white', cursor: 'pointer',
            }}>+ 新建</button>
          </div>
        </div>

        {/* Tab bar */}
        {view !== 'list' && (
          <button onClick={() => {
            setSelectedTask(null)
            setView('list')
          }} style={{
            fontSize: 12, color: '#007AFF', background: 'none', border: 'none',
            cursor: 'pointer', padding: 0, marginBottom: 8,
          }}>← 返回列表</button>
        )}
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: 'auto', padding: '8px 16px 16px' }}>

        {/* 任务列表 */}
        {view === 'list' && (
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <div style={{
              background: 'var(--mb-bg-card)', borderRadius: 12, padding: 16,
              border: '1px solid var(--mb-border)', marginBottom: 12, order: 3,
            }}>
              <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                marginBottom: autoTaskExpanded ? 14 : 0,
              }}>
                <button
                  onClick={handleAutoTaskToggle}
                  style={{
                    width: 38, height: 22, borderRadius: 11, border: 'none', cursor: 'pointer',
                    background: autoTaskConfig.enabled ? '#34C759' : '#E5E5EA', flexShrink: 0,
                    position: 'relative', transition: 'background 0.2s',
                  }}
                  title={autoTaskConfig.enabled ? '关闭自动识别任务' : '开启自动识别任务'}
                >
                  <span style={{
                    position: 'absolute', top: 2, left: autoTaskConfig.enabled ? 18 : 2,
                    width: 18, height: 18, borderRadius: '50%', background: 'white',
                    transition: 'left 0.2s', display: 'block',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.18)',
                  }} />
                </button>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--mb-text-primary)' }}>自动识别任务</span>
                    <span style={{
                      fontSize: 11, padding: '1px 6px', borderRadius: 4,
                      background: autoTaskConfig.enabled ? 'rgba(52,199,89,0.1)' : 'rgba(142,142,147,0.12)',
                      color: autoTaskConfig.enabled ? '#248A3D' : '#6E6E73',
                    }}>{autoTaskConfig.enabled ? '运行中' : '已关闭'}</span>
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--mb-text-tertiary)' }}>按应用和触发词发现可执行任务</div>
                </div>
                <button
                  type="button"
                  aria-expanded={autoTaskExpanded}
                  onClick={() => setAutoTaskExpanded(value => !value)}
                  style={{
                    border: 'none', background: 'transparent', color: 'var(--mb-brand-text)',
                    cursor: 'pointer', fontSize: 12, padding: '4px 0 4px 8px',
                  }}
                >
                  {autoTaskExpanded ? '收起' : '展开设置'} {autoTaskExpanded ? '⌃' : '⌄'}
                </button>
              </div>

              {autoTaskExpanded && (
                <>
              <div style={{ marginBottom: 14 }}>
                <label style={labelStyle}>识别软件</label>
                <div style={{ display: 'grid', gap: 8 }}>
                  {autoTaskDraft.appTargets.map((item, index) => (
                    <div key={`${item.bundleId}-${item.appName}-${index}`} style={{
                      display: 'grid', gridTemplateColumns: 'minmax(0, 1.1fr) minmax(0, 0.9fr) auto',
                      gap: 8, alignItems: 'center',
                    }}>
                      <input
                        value={item.bundleId}
                        onChange={event => setAutoTaskDraft(value => ({
                          ...value,
                          appTargets: value.appTargets.map((target, itemIndex) => itemIndex === index
                            ? { ...target, bundleId: event.target.value }
                            : target),
                        }))}
                        placeholder="Bundle ID (如 com.tencent.xinWeChat)"
                        style={inputStyle}
                      />
                      <input
                        value={item.appName}
                        onChange={event => setAutoTaskDraft(value => ({
                          ...value,
                          appTargets: value.appTargets.map((target, itemIndex) => itemIndex === index
                            ? { ...target, appName: event.target.value }
                            : target),
                        }))}
                        placeholder="应用名称 (如 微信)"
                        style={inputStyle}
                      />
                      <button type="button" onClick={() => handleRemoveAutoTaskApp(index)} style={smallDangerButtonStyle}>
                        删除
                      </button>
                    </div>
                  ))}
                  <div style={{
                    display: 'grid', gridTemplateColumns: 'minmax(0, 1.1fr) minmax(0, 0.9fr) auto',
                    gap: 8, alignItems: 'center',
                  }}>
                    <input
                      value={autoTaskAppDraft.bundleId}
                      onChange={event => setAutoTaskAppDraft(value => ({ ...value, bundleId: event.target.value }))}
                      placeholder="Bundle ID (如 com.bytedance.lark)"
                      style={inputStyle}
                    />
                    <input
                      value={autoTaskAppDraft.appName}
                      onChange={event => setAutoTaskAppDraft(value => ({ ...value, appName: event.target.value }))}
                      placeholder="应用名称 (如 飞书)"
                      style={inputStyle}
                    />
                    <button type="button" onClick={handleAddAutoTaskApp} style={smallPrimaryButtonStyle}>
                      添加
                    </button>
                  </div>
                </div>
              </div>

              <div>
                <label style={labelStyle}>触发词</label>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 }}>
                  {autoTaskDraft.triggerWords.map(word => (
                    <span key={word} style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                      fontSize: 12, color: 'var(--mb-brand-text)', background: 'rgba(181,122,43,0.12)',
                      border: '1px solid rgba(181,122,43,0.18)', borderRadius: 999,
                      padding: '4px 8px',
                    }}>
                      {word}
                      <button
                        type="button"
                        onClick={() => handleRemoveTriggerWord(word)}
                        style={{ border: 'none', background: 'transparent', color: 'var(--mb-brand-text)', cursor: 'pointer', padding: 0 }}
                        title="删除触发词"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 8 }}>
                  <input
                    value={triggerWordDraft}
                    onChange={event => setTriggerWordDraft(event.target.value)}
                    onCompositionStart={triggerWordImeGuard.onCompositionStart}
                    onCompositionEnd={triggerWordImeGuard.onCompositionEnd}
                    onBlur={triggerWordImeGuard.onBlur}
                    onKeyDown={event => {
                      if (event.key === 'Enter' && !triggerWordImeGuard.isImeEvent(event)) {
                        event.preventDefault()
                        handleAddTriggerWord()
                      }
                    }}
                    placeholder="输入触发词后回车或点击添加"
                    style={inputStyle}
                  />
                  <button type="button" onClick={handleAddTriggerWord} style={smallPrimaryButtonStyle}>
                    添加触发词
                  </button>
                </div>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
                <button onClick={handleAutoTaskSave} style={{
                  fontSize: 12, padding: '7px 12px', borderRadius: 8, border: 'none',
                  background: '#007AFF', color: 'white', cursor: 'pointer',
                }}>保存配置</button>
              </div>
                </>
              )}
            </div>

            {loading && <div style={{ textAlign: 'center', color: 'var(--mb-text-faint)', padding: 20 }}>加载中...</div>}
            {!loading && tasks.length === 0 && (
              <div style={{ textAlign: 'center', color: 'var(--mb-text-faint)', padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 8 }}>⏰</div>
                <div style={{ fontSize: 14 }}>还没有定时任务</div>
                <div style={{ fontSize: 12, marginTop: 4 }}>点击「模板库」快速创建</div>
              </div>
            )}
            {tasks.map(task => (
              <TaskCard key={task.id} task={task}
                onToggle={handleToggle} onTrigger={handleTrigger}
                onEdit={handleEdit}
                onDelete={handleDelete} onViewResult={handleViewResult}
              />
            ))}
          </div>
        )}

        {/* 创建 / 编辑表单 */}
        {(view === 'create' || view === 'edit') && (
          <div style={{ background: 'var(--mb-bg-card)', borderRadius: 12, padding: 16, border: '1px solid var(--mb-border)' }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--mb-bg-warm-text)', marginBottom: 14 }}>
              {view === 'edit' ? '编辑任务' : '创建任务'}
              {selectedTask?.is_builtin && (
                <span style={{ fontSize: 11, color: 'var(--mb-brand-text)', fontWeight: 400, marginLeft: 8 }}>
                  内置日记任务可编辑，但不能删除
                </span>
              )}
            </div>
            <div style={{ marginBottom: 14 }}>
              <label style={labelStyle}>任务名称</label>
              <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                placeholder="例：每日工作日记" style={inputStyle} />
            </div>
            <div style={{ marginBottom: 14 }}>
              <label style={labelStyle}>执行指令（自然语言）</label>
              <div style={{ position: 'relative' }}>
                <MentionHighlightTextarea
                  ref={instructionTextareaRef}
                  mentionLabels={instructionMentionOptions.map(option => option.label)}
                  value={form.user_instruction}
                  onChange={e => {
                    setForm(f => ({ ...f, user_instruction: e.target.value }))
                    refreshInstructionMention(e.target.value, e.target)
                  }}
                  onKeyDown={handleInstructionKeyDown}
                  onCompositionStart={instructionImeGuard.onCompositionStart}
                  onCompositionEnd={instructionImeGuard.onCompositionEnd}
                  onBlur={() => { instructionImeGuard.onBlur(); setInstructionMentionQuery(null) }}
                  placeholder="描述你希望 AI 做什么，可 @ 工具、Agent 或技能，例如：用 @互联网检索 收集行业资讯后生成晨报..."
                  style={{ ...inputStyle, height: 100, resize: 'vertical' as const }}
                />
                {instructionMentionQuery !== null && filteredInstructionMentionOptions.length > 0 && (
                  <div style={{
                    position: 'absolute', left: 0, right: 0, top: '100%', marginTop: 4,
                    background: 'var(--mb-bg-elevated)', border: '1px solid var(--mb-border-strong)', borderRadius: 8,
                    boxShadow: '0 6px 18px rgba(0,0,0,0.12)', maxHeight: 200, overflow: 'auto', zIndex: 20,
                  }}>
                    {filteredInstructionMentionOptions.map((option, optionIndex) => (
                      <button
                        key={option.id}
                        type="button"
                        onMouseDown={event => event.preventDefault()}
                        onClick={() => insertInstructionMention(option)}
                        style={{
                          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
                          width: '100%', padding: '7px 10px', border: 'none', textAlign: 'left',
                          cursor: 'pointer', fontSize: 12,
                          background: optionIndex === instructionMentionActiveIndex
                            ? 'rgba(0,122,255,0.08)' : 'transparent',
                        }}
                      >
                        <span style={{ color: '#007AFF', fontWeight: 500 }}>@{option.label}</span>
                        <span style={{ color: 'var(--mb-text-faint)', fontSize: 11 }}>{option.description}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div style={{ marginBottom: 14 }}>
              <label style={labelStyle}>执行智能体</label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {([
                  { kind: 'consult', label: '咨询智能体', description: '检索本地知识后生成结果（默认）' },
                  { kind: 'creation', label: '创作智能体', description: '按创作 Agent 流程生成文档，可在创作页查看执行过程' },
                ] as const).map(option => (
                  <button key={option.kind} type="button"
                    onClick={() => setForm(f => ({ ...f, executor_kind: option.kind }))}
                    title={option.description}
                    style={scheduleChipStyle(form.executor_kind === option.kind)}>
                    {option.label}
                  </button>
                ))}
              </div>
            </div>
            <div style={{ marginBottom: 16 }}>
              <label style={labelStyle}>执行频率</label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
                {(Object.keys(SCHEDULE_KIND_LABELS) as ScheduleKind[]).map(kind => (
                  <button key={kind} type="button"
                    onClick={() => setSchedule(s => ({ ...s, kind }))}
                    style={scheduleChipStyle(schedule.kind === kind)}>
                    {SCHEDULE_KIND_LABELS[kind]}
                  </button>
                ))}
              </div>
              {schedule.kind === 'weekly' && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
                  {WEEKDAY_ORDER.map(day => (
                    <button key={day} type="button"
                      onClick={() => setSchedule(s => ({
                        ...s,
                        weekdays: s.weekdays.includes(day)
                          ? s.weekdays.filter(item => item !== day)
                          : [...s.weekdays, day],
                      }))}
                      style={scheduleChipStyle(schedule.weekdays.includes(day))}>
                      周{WEEKDAY_NAMES[day]}
                    </button>
                  ))}
                </div>
              )}
              {schedule.kind === 'monthly' && (
                <div style={{ marginBottom: 10 }}>
                  <select value={schedule.dayOfMonth}
                    onChange={e => setSchedule(s => ({ ...s, dayOfMonth: Number(e.target.value) }))}
                    style={{ ...inputStyle, width: 'auto' }}>
                    {Array.from({ length: 31 }, (_, index) => index + 1).map(day => (
                      <option key={day} value={day}>每月 {day} 日</option>
                    ))}
                  </select>
                  {schedule.dayOfMonth > 28 && (
                    <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 4 }}>
                      没有这一天的月份（如 2 月）将不会执行
                    </div>
                  )}
                </div>
              )}
              {schedule.kind === 'custom' ? (
                <>
                  <input value={schedule.customExpression}
                    onChange={e => setSchedule(s => ({ ...s, customExpression: e.target.value }))}
                    placeholder="0 20 * * *" style={inputStyle} />
                  <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 4 }}>
                    Cron 表达式（分 时 日 月 周），例如「0 20 * * *」表示每天 20:00
                  </div>
                </>
              ) : (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 12, color: 'var(--mb-text-secondary)' }}>执行时间</span>
                  <input type="time" value={schedule.time}
                    onChange={e => setSchedule(s => ({ ...s, time: e.target.value }))}
                    style={{ ...inputStyle, width: 'auto' }} />
                </div>
              )}
              <div style={{ fontSize: 11, color: 'var(--mb-text-tertiary)', marginTop: 6 }}>
                当前设置：{(() => {
                  const cron = buildCronFromSchedule(schedule)
                  return cron ? describeCron(cron) : '请完善频率设置'
                })()}
              </div>
            </div>
            <div style={{ marginBottom: 16 }}>
              <label style={labelStyle}>结果推送到</label>
              <div style={{ display: 'grid', gap: 8 }}>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 9, padding: '8px 10px',
                  border: '1px solid rgba(181,122,43,0.16)', borderRadius: 8, background: 'var(--mb-bg-warm)',
                }}>
                  <input type="checkbox" checked disabled />
                  <span style={{ fontSize: 12, color: 'var(--mb-bg-warm-text)', flex: 1 }}>站内消息</span>
                  <span style={{ fontSize: 11, color: 'var(--mb-text-tertiary)' }}>默认渠道</span>
                </div>
                {channels.map(channel => (
                  <label key={channel.id} style={{
                    display: 'flex', alignItems: 'center', gap: 9, padding: '8px 10px',
                    border: '1px solid rgba(181,122,43,0.16)', borderRadius: 8,
                    background: channel.enabled ? '#FFFCF7' : '#F5F5F5',
                    opacity: channel.enabled ? 1 : 0.58, cursor: channel.enabled ? 'pointer' : 'default',
                  }}>
                    <input
                      type="checkbox"
                      checked={form.notification_channel_ids.includes(channel.id)}
                      disabled={!channel.enabled}
                      onChange={() => toggleFormChannel(channel.id)}
                    />
                    <span style={{ fontSize: 12, color: 'var(--mb-bg-warm-text)', flex: 1 }}>{channel.name}</span>
                    <span style={{ fontSize: 11, color: 'var(--mb-text-tertiary)' }}>
                      {channelTypeLabel(channel.channel_type)}{channel.enabled ? '' : ' · 已停用'}
                    </span>
                  </label>
                ))}
              </div>
            </div>
            <button onClick={view === 'edit' ? handleUpdate : handleCreate} style={{
              width: '100%', padding: '10px', borderRadius: 8, border: 'none',
              background: '#007AFF', color: 'white', fontSize: 14, fontWeight: 500, cursor: 'pointer',
            }}>{view === 'edit' ? '保存修改' : '创建任务'}</button>
          </div>
        )}

        {/* 本地消息渠道 */}
        {view === 'channels' && (
          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{
              background: 'linear-gradient(135deg, var(--mb-bg-warm-deep) 0%, var(--mb-bg-warm) 100%)',
              border: '1px solid rgba(181,122,43,0.2)', borderRadius: 12, padding: 16,
            }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--mb-bg-warm-text)' }}>任务结果消息渠道</div>
              <p style={{ fontSize: 12, color: 'var(--mb-brand-text)', lineHeight: 1.6, margin: '6px 0 14px' }}>
                Webhook 地址只保存在这台电脑上，不会同步到云端。支持飞书、钉钉、企业微信和通用 Webhook。
              </p>
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, .7fr) minmax(120px, .55fr)', gap: 10, marginBottom: 10 }}>
                <input
                  value={channelForm.name}
                  onChange={event => setChannelForm(value => ({ ...value, name: event.target.value }))}
                  placeholder="渠道名称，如「我的工作群」"
                  style={inputStyle}
                />
                <select
                  value={channelForm.channel_type}
                  onChange={event => setChannelForm(value => ({
                    ...value,
                    channel_type: event.target.value as NotificationChannel['channel_type'],
                  }))}
                  style={{ ...inputStyle }}
                >
                  <option value="feishu">飞书</option>
                  <option value="dingtalk">钉钉</option>
                  <option value="wecom">企业微信</option>
                  <option value="webhook">通用 Webhook</option>
                </select>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 10 }}>
                <input
                  type="password"
                  autoComplete="off"
                  value={channelForm.webhook_url}
                  onChange={event => setChannelForm(value => ({ ...value, webhook_url: event.target.value }))}
                  placeholder="https://…"
                  style={inputStyle}
                />
                <button type="button" onClick={handleCreateChannel} style={smallPrimaryButtonStyle}>
                  添加渠道
                </button>
              </div>
            </div>

            {channels.length === 0 ? (
              <div style={{ textAlign: 'center', padding: 28, color: 'var(--mb-text-tertiary)', fontSize: 12 }}>
                暂无消息渠道。添加后可在创建或编辑任务时选择。
              </div>
            ) : channels.map(channel => (
              <div key={channel.id} style={{
                display: 'flex', alignItems: 'center', gap: 12, background: 'var(--mb-bg-card)',
                border: '1px solid var(--mb-border)', borderRadius: 10, padding: '12px 14px',
                opacity: channel.enabled ? 1 : 0.62,
              }}>
                <button
                  type="button"
                  onClick={() => handleToggleChannel(channel)}
                  aria-label={`${channel.enabled ? '停用' : '启用'}${channel.name}`}
                  style={{
                    width: 36, height: 20, borderRadius: 10, border: 'none', cursor: 'pointer',
                    background: channel.enabled ? '#34C759' : '#E5E5EA', flexShrink: 0,
                    position: 'relative',
                  }}
                >
                  <span style={{
                    position: 'absolute', top: 2, left: channel.enabled ? 18 : 2,
                    width: 16, height: 16, borderRadius: '50%', background: 'white',
                    transition: 'left .2s',
                  }} />
                </button>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--mb-bg-warm-text)' }}>{channel.name}</div>
                  <div style={{ fontSize: 11, color: 'var(--mb-text-tertiary)', marginTop: 3 }}>
                    {channelTypeLabel(channel.channel_type)} · {webhookHost(channel.webhook_url)}
                  </div>
                </div>
                <button type="button" onClick={() => handleDeleteChannel(channel)} style={smallDangerButtonStyle}>
                  删除
                </button>
              </div>
            ))}
          </div>
        )}

        {/* 模板库 */}
        {view === 'templates' && (
          <>
            {Object.entries(groupTemplatesByCategory(BUILTIN_TEMPLATES)).map(([category, tpls]) => (
              <div key={category} style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: CATEGORY_COLORS[category] || '#6E6E73',
                  marginBottom: 8, paddingLeft: 2 }}>{category}</div>
                {tpls.map(tpl => (
                  <div key={tpl.id} onClick={() => handleUseTemplate(tpl)} style={{
                    background: 'var(--mb-bg-card)', borderRadius: 10, padding: '10px 14px',
                    border: '1px solid var(--mb-border)', marginBottom: 8, cursor: 'pointer',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 13, fontWeight: 500 }}>{tpl.name}</span>
                      <span style={{ fontSize: 11, color: '#007AFF' }}>{describeCron(tpl.cron)}</span>
                    </div>
                    <p style={{ fontSize: 11, color: 'var(--mb-text-secondary)', margin: '4px 0 0',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {tpl.user_instruction}
                    </p>
                  </div>
                ))}
              </div>
            ))}
          </>
        )}

        {/* 执行结果 */}
        {view === 'result' && selectedTask && (
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 12 }}>{selectedTask.name} — 执行历史</div>
            {executions.length === 0 && (
              <div style={{ textAlign: 'center', padding: 32, color: 'var(--mb-text-tertiary)', fontSize: 12 }}>
                还没有执行记录
              </div>
            )}
            {executions.map(exec => (
              <div key={exec.id} style={{
                background: 'var(--mb-bg-card)', borderRadius: 10, padding: 14,
                border: '1px solid var(--mb-border)', marginBottom: 10,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                  <span style={{ fontSize: 12, color: exec.status === 'success' ? '#34C759' : '#FF3B30', fontWeight: 500 }}>
                    {exec.status === 'success' ? '成功' : exec.status === 'failed' ? '失败' : '执行中'}
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--mb-text-faint)', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                    <span>
                      {formatTs(exec.started_at)}
                      {exec.latency_ms && ` · ${(exec.latency_ms / 1000).toFixed(1)}s`}
                      {exec.knowledge_count && ` · ${exec.knowledge_count} 条知识`}
                    </span>
                    {exec.creation_history_id != null && (
                      <button type="button"
                        onClick={() => handleOpenCreationHistory(exec.creation_history_id as number)}
                        style={{
                          fontSize: 11, padding: '2px 8px', borderRadius: 999, cursor: 'pointer',
                          border: '1px solid rgba(0,122,255,0.25)',
                          background: 'rgba(0,122,255,0.08)', color: '#007AFF',
                        }}>
                        查看执行过程
                      </button>
                    )}
                  </span>
                </div>
                {exec.result_text && (
                  <pre style={{ fontSize: 12, color: 'var(--mb-text-primary)', margin: 0, whiteSpace: 'pre-wrap',
                    maxHeight: 300, overflow: 'auto', lineHeight: 1.6 }}>
                    {exec.result_text}
                  </pre>
                )}
                {exec.error_message && (
                  <div style={{ fontSize: 12, color: '#FF3B30' }}>{exec.error_message}</div>
                )}
                {(exec.notification_deliveries?.length || 0) > 0 && (
                  <div style={{
                    display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10,
                    borderTop: '1px solid var(--mb-divider)', paddingTop: 9,
                  }}>
                    {exec.notification_deliveries.map(delivery => (
                      <span key={delivery.channel_id} title={delivery.error_message || undefined} style={{
                        fontSize: 11, borderRadius: 999, padding: '3px 7px',
                        color: delivery.status === 'success' ? 'var(--mb-status-success-text)'
                          : delivery.status === 'failed' ? 'var(--mb-status-failed-text)' : 'var(--mb-status-pending-text)',
                        background: delivery.status === 'success' ? 'rgba(52,199,89,.1)'
                          : delivery.status === 'failed' ? 'rgba(255,59,48,.08)' : 'rgba(181,122,43,.1)',
                      }}>
                        {delivery.channel_name} · {
                          delivery.status === 'success' ? '已推送'
                            : delivery.status === 'failed' ? '推送失败' : '等待推送'
                        }
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)',
          background: 'rgba(0,0,0,0.75)', color: 'white', padding: '8px 16px',
          borderRadius: 20, fontSize: 13, zIndex: 9999,
        }}>{toast}</div>
      )}
    </div>
  )
}

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--mb-text-secondary)', marginBottom: 6,
}

const inputStyle: React.CSSProperties = {
  width: '100%', padding: '8px 10px', borderRadius: 8, fontSize: 13,
  border: '1px solid var(--mb-border-strong)', outline: 'none', boxSizing: 'border-box',
  background: 'var(--mb-bg-input)', color: 'var(--mb-text-primary)', colorScheme: 'light dark',
  fontFamily: 'inherit',
}

const smallPrimaryButtonStyle: React.CSSProperties = {
  fontSize: 12,
  padding: '7px 10px',
  borderRadius: 8,
  border: 'none',
  background: '#007AFF',
  color: 'white',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
}

const smallDangerButtonStyle: React.CSSProperties = {
  fontSize: 12,
  padding: '7px 10px',
  borderRadius: 8,
  border: '1px solid rgba(255,59,48,0.18)',
  background: 'rgba(255,59,48,0.08)',
  color: '#D70015',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
}

function scheduleChipStyle(active: boolean): React.CSSProperties {
  return {
    fontSize: 12, padding: '5px 12px', borderRadius: 999, cursor: 'pointer',
    border: active ? '1px solid #007AFF' : '1px solid var(--mb-border-strong)',
    background: active ? 'rgba(0,122,255,0.1)' : 'var(--mb-bg-input)',
    color: active ? '#007AFF' : 'var(--mb-text-secondary)',
  }
}

function btnStyle(bg: string): React.CSSProperties {
  return {
    background: bg, color: 'white', border: 'none', borderRadius: 6,
    padding: '5px 8px', cursor: 'pointer', display: 'flex', alignItems: 'center',
  }
}

export default ScheduledTasksPanel
