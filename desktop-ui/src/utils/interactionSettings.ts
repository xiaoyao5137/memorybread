import { emit } from '@tauri-apps/api/event'
import {
  register as registerGlobalShortcut,
  unregisterAll as unregisterAllGlobalShortcuts,
  type ShortcutEvent,
} from '@tauri-apps/plugin-global-shortcut'

export type ShortcutAction = 'open_consult' | 'open_creation' | 'recognize_screen_task'

export type FloatingBallAction =
  | 'none'
  | 'open_floating_consult'
  | 'open_main_panel'
  | 'recognize_screen_task'

export interface InteractionSettings {
  shortcuts: Record<ShortcutAction, string | null>
  floatingBall: {
    singleClick: FloatingBallAction
    doubleClick: FloatingBallAction
  }
}

export const INTERACTION_SETTINGS_KEY = 'memory-bread_interaction_settings_v1'
export const INTERACTION_SETTINGS_CHANGED_EVENT = 'interaction-settings-changed'

export const DEFAULT_INTERACTION_SETTINGS: InteractionSettings = {
  shortcuts: {
    open_consult: 'CommandOrControl+Alt+Space',
    open_creation: 'CommandOrControl+Alt+C',
    recognize_screen_task: 'CommandOrControl+Alt+R',
  },
  floatingBall: {
    singleClick: 'open_floating_consult',
    doubleClick: 'open_main_panel',
  },
}

export const SHORTCUT_ACTIONS: ReadonlyArray<{
  id: ShortcutAction
  label: string
  description: string
}> = [
  { id: 'open_consult', label: '打开咨询页', description: '显示主面板并进入咨询' },
  { id: 'open_creation', label: '打开创作页', description: '显示主面板并进入创作' },
  { id: 'recognize_screen_task', label: '识别当屏任务', description: '截取当前窗口并立即识别' },
]

export const FLOATING_BALL_ACTIONS: ReadonlyArray<{
  id: FloatingBallAction
  label: string
}> = [
  { id: 'none', label: '无事件' },
  { id: 'open_floating_consult', label: '打开悬浮球咨询框' },
  { id: 'open_main_panel', label: '打开主面板' },
  { id: 'recognize_screen_task', label: '触发当屏任务识别' },
]

const floatingBallActionLabels = new Map<FloatingBallAction, string>(
  FLOATING_BALL_ACTIONS.map(item => [item.id, item.label]),
)

export const floatingBallActionHint = (settings: InteractionSettings['floatingBall']): string =>
  `单击：${floatingBallActionLabels.get(settings.singleClick)}；双击：${floatingBallActionLabels.get(settings.doubleClick)}`

const shortcutActionIds = new Set<ShortcutAction>(SHORTCUT_ACTIONS.map(item => item.id))
const floatingBallActionIds = new Set<FloatingBallAction>(FLOATING_BALL_ACTIONS.map(item => item.id))

const cloneDefaults = (): InteractionSettings => ({
  shortcuts: { ...DEFAULT_INTERACTION_SETTINGS.shortcuts },
  floatingBall: { ...DEFAULT_INTERACTION_SETTINGS.floatingBall },
})

export const normalizeInteractionSettings = (value: unknown): InteractionSettings => {
  const defaults = cloneDefaults()
  if (!value || typeof value !== 'object') return defaults

  const candidate = value as Partial<InteractionSettings>
  const shortcuts = candidate.shortcuts && typeof candidate.shortcuts === 'object'
    ? candidate.shortcuts
    : {}
  const floatingBall = candidate.floatingBall && typeof candidate.floatingBall === 'object'
    ? candidate.floatingBall
    : {}

  for (const action of shortcutActionIds) {
    const shortcut = (shortcuts as Partial<Record<ShortcutAction, unknown>>)[action]
    if (shortcut === null || (typeof shortcut === 'string' && shortcut.trim())) {
      defaults.shortcuts[action] = typeof shortcut === 'string' ? shortcut.trim() : null
    }
  }

  const singleClick = (floatingBall as Partial<InteractionSettings['floatingBall']>).singleClick
  const doubleClick = (floatingBall as Partial<InteractionSettings['floatingBall']>).doubleClick
  if (singleClick && floatingBallActionIds.has(singleClick)) {
    defaults.floatingBall.singleClick = singleClick
  }
  if (doubleClick && floatingBallActionIds.has(doubleClick)) {
    defaults.floatingBall.doubleClick = doubleClick
  }

  return defaults
}

export const readInteractionSettings = (): InteractionSettings => {
  if (typeof window === 'undefined' || typeof window.localStorage?.getItem !== 'function') {
    return cloneDefaults()
  }
  try {
    const raw = window.localStorage.getItem(INTERACTION_SETTINGS_KEY)
    return raw ? normalizeInteractionSettings(JSON.parse(raw)) : cloneDefaults()
  } catch {
    return cloneDefaults()
  }
}

export const findShortcutConflict = (
  shortcuts: InteractionSettings['shortcuts'],
): [ShortcutAction, ShortcutAction] | null => {
  const assigned = new Map<string, ShortcutAction>()
  for (const action of shortcutActionIds) {
    const shortcut = shortcuts[action]?.trim().toLowerCase()
    if (!shortcut) continue
    const previous = assigned.get(shortcut)
    if (previous) return [previous, action]
    assigned.set(shortcut, action)
  }
  return null
}

const keyFromKeyboardEvent = (event: KeyboardEvent | React.KeyboardEvent): string | null => {
  if (/^Key[A-Z]$/.test(event.code)) return event.code.slice(3)
  if (/^Digit[0-9]$/.test(event.code)) return event.code.slice(5)
  if (/^F(?:[1-9]|1[0-2])$/.test(event.code)) return event.code
  const specialKeys: Record<string, string> = {
    Space: 'Space',
    Enter: 'Enter',
    ArrowUp: 'ArrowUp',
    ArrowDown: 'ArrowDown',
    ArrowLeft: 'ArrowLeft',
    ArrowRight: 'ArrowRight',
    Home: 'Home',
    End: 'End',
    PageUp: 'PageUp',
    PageDown: 'PageDown',
  }
  return specialKeys[event.code] ?? null
}

export const shortcutFromKeyboardEvent = (
  event: KeyboardEvent | React.KeyboardEvent,
): string | null => {
  const key = keyFromKeyboardEvent(event)
  if (!key) return null

  const modifiers: string[] = []
  if (event.metaKey || event.ctrlKey) modifiers.push('CommandOrControl')
  if (event.altKey) modifiers.push('Alt')
  if (event.shiftKey) modifiers.push('Shift')
  if (modifiers.length === 0) return null
  return [...modifiers, key].join('+')
}

export const shortcutDisplayParts = (shortcut: string | null): string[] => {
  if (!shortcut) return []
  const isMac = typeof navigator !== 'undefined'
    && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
  return shortcut.split('+').map(part => {
    if (part === 'CommandOrControl') return isMac ? '⌘' : 'Ctrl'
    if (part === 'Alt') return isMac ? '⌥' : 'Alt'
    if (part === 'Shift') return isMac ? '⇧' : 'Shift'
    if (part === 'Space') return 'Space'
    return part
  })
}

type ShortcutActionHandler = (action: ShortcutAction) => void | Promise<void>

let runtimeActionHandler: ShortcutActionHandler | null = null
let registrationQueue: Promise<void> = Promise.resolve()

const isTauriRuntime = () => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

const installGlobalShortcuts = async (shortcuts: InteractionSettings['shortcuts']) => {
  if (!isTauriRuntime()) return
  await unregisterAllGlobalShortcuts()
  try {
    for (const action of shortcutActionIds) {
      const shortcut = shortcuts[action]
      if (!shortcut) continue
      await registerGlobalShortcut(shortcut, (event: ShortcutEvent) => {
        if (event.state === 'Pressed') void runtimeActionHandler?.(action)
      })
    }
  } catch (error) {
    await unregisterAllGlobalShortcuts().catch(() => {})
    throw error
  }
}

const enqueueRegistration = (task: () => Promise<void>) => {
  const result = registrationQueue.then(task)
  registrationQueue = result.catch(() => {})
  return result
}

const broadcastInteractionSettings = async (settings: InteractionSettings) => {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(INTERACTION_SETTINGS_CHANGED_EVENT, { detail: settings }))
  if (!isTauriRuntime()) return
  await emit(INTERACTION_SETTINGS_CHANGED_EVENT, settings).catch(() => {})
}

const persistInteractionSettings = async (settings: InteractionSettings) => {
  if (typeof window !== 'undefined' && typeof window.localStorage?.setItem === 'function') {
    window.localStorage.setItem(INTERACTION_SETTINGS_KEY, JSON.stringify(settings))
  }
  await broadcastInteractionSettings(settings)
}

export const saveInteractionSettings = async (value: InteractionSettings) => {
  const next = normalizeInteractionSettings(value)
  const conflict = findShortcutConflict(next.shortcuts)
  if (conflict) throw new Error('快捷键不能重复')

  const previous = readInteractionSettings()
  if (runtimeActionHandler) {
    await enqueueRegistration(async () => {
      try {
        await installGlobalShortcuts(next.shortcuts)
      } catch (error) {
        await installGlobalShortcuts(previous.shortcuts).catch(() => {})
        throw error
      }
    })
  }
  await persistInteractionSettings(next)
  return next
}

export const startGlobalShortcutRuntime = (handler: ShortcutActionHandler) => {
  runtimeActionHandler = handler
  void enqueueRegistration(() => installGlobalShortcuts(readInteractionSettings().shortcuts))
    .catch(error => console.warn('global shortcut registration failed', error))

  return () => {
    if (runtimeActionHandler === handler) runtimeActionHandler = null
    void enqueueRegistration(async () => {
      if (isTauriRuntime()) await unregisterAllGlobalShortcuts()
    }).catch(() => {})
  }
}
