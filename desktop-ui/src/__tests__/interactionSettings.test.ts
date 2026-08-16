import { beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_INTERACTION_SETTINGS,
  INTERACTION_SETTINGS_KEY,
  findShortcutConflict,
  floatingBallActionHint,
  normalizeInteractionSettings,
  readInteractionSettings,
  shortcutFromKeyboardEvent,
} from '../utils/interactionSettings'

describe('interaction settings', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('首次使用提供快捷键与悬浮球默认动作', () => {
    expect(readInteractionSettings()).toEqual(DEFAULT_INTERACTION_SETTINGS)
    expect(readInteractionSettings().floatingBall).toEqual({
      singleClick: 'open_floating_consult',
      doubleClick: 'open_main_panel',
    })
  })

  it('根据当前单击和双击动作生成悬浮提示', () => {
    expect(floatingBallActionHint({
      singleClick: 'open_floating_consult',
      doubleClick: 'open_main_panel',
    })).toBe('单击：打开悬浮球咨询框；双击：打开主面板')

    expect(floatingBallActionHint({
      singleClick: 'none',
      doubleClick: 'recognize_screen_task',
    })).toBe('单击：无事件；双击：触发当屏任务识别')
  })

  it('损坏或未知配置会安全回退到默认值', () => {
    window.localStorage.setItem(INTERACTION_SETTINGS_KEY, '{broken')
    expect(readInteractionSettings()).toEqual(DEFAULT_INTERACTION_SETTINGS)

    expect(normalizeInteractionSettings({
      shortcuts: { open_consult: 'CommandOrControl+Alt+Q' },
      floatingBall: { singleClick: 'unknown', doubleClick: 'none' },
    })).toEqual({
      shortcuts: {
        ...DEFAULT_INTERACTION_SETTINGS.shortcuts,
        open_consult: 'CommandOrControl+Alt+Q',
      },
      floatingBall: {
        singleClick: 'open_floating_consult',
        doubleClick: 'none',
      },
    })
  })

  it('把键盘组合转换为跨平台 Tauri accelerator', () => {
    expect(shortcutFromKeyboardEvent({
      code: 'KeyK',
      key: 'k',
      metaKey: true,
      ctrlKey: false,
      altKey: true,
      shiftKey: true,
    } as KeyboardEvent)).toBe('CommandOrControl+Alt+Shift+K')

    expect(shortcutFromKeyboardEvent({
      code: 'KeyK',
      key: 'k',
      metaKey: false,
      ctrlKey: false,
      altKey: false,
      shiftKey: false,
    } as KeyboardEvent)).toBeNull()
  })

  it('识别重复快捷键但允许禁用某一项', () => {
    expect(findShortcutConflict({
      open_consult: 'CommandOrControl+Alt+K',
      open_creation: 'commandorcontrol+alt+k',
      recognize_screen_task: null,
    })).toEqual(['open_consult', 'open_creation'])

    expect(findShortcutConflict({
      open_consult: null,
      open_creation: 'CommandOrControl+Alt+C',
      recognize_screen_task: null,
    })).toBeNull()
  })
})
