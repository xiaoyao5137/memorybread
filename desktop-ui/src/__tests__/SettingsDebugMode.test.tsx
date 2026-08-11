import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import Settings from '../components/Settings'
import { useAppStore } from '../store/useAppStore'

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.setState({
    debugModeEnabled: false,
    serviceEnvironment: 'production',
  })
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ preferences: [], items: [] }),
  })))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Settings debug mode visibility', () => {
  it('普通启动时隐藏调试面板入口', async () => {
    render(<Settings />)

    expect(screen.queryByTestId('settings-debug-section')).not.toBeInTheDocument()
    expect(screen.queryByTestId('open-debug-btn')).not.toBeInTheDocument()
    expect(screen.queryByTestId('debug-mode-toggle')).not.toBeInTheDocument()
    expect(screen.queryByTestId('settings-api-section')).not.toBeInTheDocument()
    expect(screen.queryByTestId('local-debug-mode-toggle')).not.toBeInTheDocument()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })

  it('Debug 启动时默认选择测试环境，但普通账号仍隐藏本机 Core 配置', async () => {
    useAppStore.getState().setDebugModeEnabled(true)
    render(<Settings />)

    expect(screen.getByTestId('settings-debug-section')).toBeInTheDocument()
    expect(screen.getByTestId('open-debug-btn')).toBeInTheDocument()
    expect(screen.getByTestId('debug-mode-toggle')).toBeChecked()
    expect(screen.queryByTestId('settings-api-section')).not.toBeInTheDocument()
    expect(screen.queryByTestId('local-debug-mode-toggle')).not.toBeInTheDocument()
    expect(screen.getByRole('group', { name: '选择服务环境' })).toBeInTheDocument()
    expect(screen.getByTestId('service-environment-staging')).toHaveAttribute('aria-pressed', 'true')
    expect(useAppStore.getState().adminApiBaseUrl).toBe('http://127.0.0.1:18080')
    expect(useAppStore.getState().gatewayApiBaseUrl).toBe('http://127.0.0.1:18090')

    fireEvent.click(screen.getByTestId('open-debug-btn'))
    expect(useAppStore.getState().windowMode).toBe('debug')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })

  it('测试账号以 Debug 启动后显示本机服务配置', async () => {
    useAppStore.getState().setDebugModeEnabled(true)
    useAppStore.setState({
      currentUser: {
        id: '01900000-0000-7000-8000-000000000001',
        status: 'active',
        roles: ['user'],
        feature_flags: ['local_service_settings'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-07-18T00:00:00Z',
      },
    })
    render(<Settings />)

    expect(screen.getByTestId('debug-mode-toggle')).toBeChecked()
    expect(screen.getByTestId('settings-api-section')).toBeInTheDocument()
    expect(screen.getByTestId('api-url-input')).toBeInTheDocument()
    expect(screen.queryByTestId('local-debug-mode-toggle')).not.toBeInTheDocument()
    expect(useAppStore.getState().debugModeEnabled).toBe(true)
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })

  it('在开发者模式中切换正式和测试服务环境', async () => {
    useAppStore.getState().setDebugModeEnabled(true)
    render(<Settings />)

    fireEvent.click(screen.getByTestId('service-environment-production'))

    expect(useAppStore.getState().serviceEnvironment).toBe('production')
    expect(useAppStore.getState().adminApiBaseUrl).toBe('https://memorybread.work')
    expect(useAppStore.getState().gatewayApiBaseUrl).toBe('https://gateway.memorybread.work')
    expect(screen.getByTestId('service-environment-production')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('service-environment-staging')).toHaveAttribute('aria-pressed', 'false')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })
})
