import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { invoke } from '@tauri-apps/api/core'
import App from '../App'
import { useAppStore } from '../store/useAppStore'
import type { ShortcutAction } from '../utils/interactionSettings'

const shortcutRuntime = vi.hoisted(() => ({
  handler: null as null | ((action: ShortcutAction) => void | Promise<void>),
}))

const initializationMocks = vi.hoisted(() => ({
  fetchInitializationStatus: vi.fn(),
  fetchRuntimeReadiness: vi.fn(),
}))

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn(async () => vi.fn()),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(async () => undefined),
}))

vi.mock('../utils/interactionSettings', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/interactionSettings')>()
  return {
    ...actual,
    startGlobalShortcutRuntime: vi.fn((handler: (action: ShortcutAction) => void | Promise<void>) => {
      shortcutRuntime.handler = handler
      return vi.fn()
    }),
  }
})

vi.mock('../utils/initialization', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/initialization')>()),
  fetchInitializationStatus: initializationMocks.fetchInitializationStatus,
  fetchRuntimeReadiness: initializationMocks.fetchRuntimeReadiness,
}))

vi.mock('../components/FloatingBuddy', () => ({ default: () => <aside /> }))
vi.mock('../components/RagPanel.v2', () => ({ default: () => <section data-testid="rag-panel" /> }))
vi.mock('../components/CreationPanel', () => ({ default: () => <section data-testid="creation-panel" /> }))
vi.mock('../components/ActionConfirm', () => ({ default: () => null }))

const mockedInvoke = vi.mocked(invoke)

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.getState().setHasCompletedSetup(true)
  initializationMocks.fetchInitializationStatus.mockResolvedValue({
    schema_version: 'initialization.v1',
    mode: 'normal',
    state: 'completed',
    progress: 100,
    current_stage: 'feature_smoke_tests',
    message: '初始化完成',
    stages: [],
    quality_gate: { passed: true, checks: [] },
    smoke_tests: [],
    can_retry: false,
    can_report: false,
    test_mode_enabled: false,
  })
  initializationMocks.fetchRuntimeReadiness.mockResolvedValue(true)
  shortcutRuntime.handler = null
  mockedInvoke.mockClear()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })))
})

describe('App global shortcut actions', () => {
  it('打开目标页面并把当屏识别交给悬浮球原生动作队列', async () => {
    render(<App />)
    await waitFor(() => expect(shortcutRuntime.handler).not.toBeNull())

    await act(async () => {
      await shortcutRuntime.handler?.('open_creation')
    })
    expect(useAppStore.getState().windowMode).toBe('creation')
    expect(mockedInvoke).toHaveBeenCalledWith('show_main_panel_from_floating_assist')

    await act(async () => {
      await shortcutRuntime.handler?.('open_consult')
    })
    expect(useAppStore.getState().windowMode).toBe('rag')

    await act(async () => {
      await shortcutRuntime.handler?.('recognize_screen_task')
    })
    expect(mockedInvoke).toHaveBeenCalledWith('trigger_floating_assist_action', {
      action: 'recognize_screen_task',
    })
  })
})
