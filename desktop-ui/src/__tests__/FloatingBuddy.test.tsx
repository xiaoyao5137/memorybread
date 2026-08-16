import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FloatingBuddy from '../components/FloatingBuddy'
import BakeTabs from '../components/bake/BakeTabs'
import { useAppStore } from '../store/useAppStore'
import type { SoftwareUpdateCheck } from '../utils/softwareUpdate'

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.setState({
    debugModeEnabled: false,
    localDebugModeEnabled: false,
    serviceEnvironment: 'production',
  })
})

describe('FloatingBuddy', () => {
  it('渲染主菜单按钮', () => {
    render(<FloatingBuddy />)
    expect(screen.getByTestId('buddy-avatar')).toBeInTheDocument()
    expect(screen.getByTestId('settings-btn')).toBeInTheDocument()
    expect(screen.queryByTestId('messages-btn')).not.toBeInTheDocument()
  })

  it('点击菜单按钮会切换到对应模式', () => {
    render(<FloatingBuddy />)
    fireEvent.click(screen.getByTestId('settings-btn'))
    expect(useAppStore.getState().windowMode).toBe('settings')
  })

  it('普通菜单导航会清除关联返回栈', () => {
    useAppStore.getState().pushBakeNavigationTarget({ windowMode: 'rag' })

    render(<FloatingBuddy />)
    fireEvent.click(screen.getByTestId('settings-btn'))

    expect(useAppStore.getState().bakeNavigationStack).toHaveLength(0)
    expect(useAppStore.getState().captureBackTarget).toBeNull()
  })

  it('记忆菜单排在第二位，采集排在第三位', () => {
    render(<FloatingBuddy />)
    const buttonTestIds = screen.getAllByRole('button').map(button => button.getAttribute('data-testid'))
    expect(buttonTestIds.indexOf('knowledge-btn')).toBe(buttonTestIds.indexOf('bake-btn') + 1)
    expect(screen.getByText('记忆')).toBeInTheDocument()
  })

  it('集成菜单紧跟在日记下方', () => {
    render(<FloatingBuddy />)
    const buttonTestIds = screen.getAllByRole('button').map(button => button.getAttribute('data-testid'))

    expect(buttonTestIds.indexOf('integration-btn')).toBe(buttonTestIds.indexOf('diary-btn') + 1)
    fireEvent.click(screen.getByTestId('integration-btn'))
    expect(useAppStore.getState().windowMode).toBe('integration')
  })

  it('菜单栏不展示环境切换', () => {
    render(<FloatingBuddy />)

    expect(screen.queryByLabelText('服务环境切换')).not.toBeInTheDocument()
  })

  it('调试模式也不在菜单栏展示环境切换', () => {
    useAppStore.getState().setDebugModeEnabled(true)
    render(<FloatingBuddy />)

    expect(screen.queryByLabelText('服务环境切换')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('选择服务环境')).not.toBeInTheDocument()
    expect(useAppStore.getState().serviceEnvironment).toBe('staging')
    expect(useAppStore.getState().adminApiBaseUrl).toBe('http://127.0.0.1:18080')
    expect(useAppStore.getState().gatewayApiBaseUrl).toBe('http://127.0.0.1:18090')
  })

  it('账号入口显示用户名和中文运行模式', () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_test_token',
      expires_at: new Date(Date.now() + 86400_000).toISOString(),
      user: {
        id: '018f0000-0000-7000-8000-000000000002',
        username: '烘焙师土豆',
        display_name: '土豆账户',
        nickname: '土豆',
        email: 'tudou@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: new Date().toISOString(),
      },
    })
    useAppStore.getState().setCloudSubscription({
      id: 'sub_001',
      status: 'active',
      plan_key: 'gold',
      name: '黄金',
    })

    render(<FloatingBuddy />)

    expect(screen.getByText('土豆')).toBeInTheDocument()
    expect(screen.queryByText('烘焙师土豆')).not.toBeInTheDocument()
    expect(screen.queryByText('土豆账户')).not.toBeInTheDocument()
    expect(screen.getByText('增强模式')).toBeInTheDocument()
    expect(screen.queryByText('云账户已连接')).not.toBeInTheDocument()
    expect(screen.getByTestId('account-avatar')).toHaveTextContent('土')
  })

  it('账号入口是侧栏底部导航并支持当前页面状态', () => {
    render(<FloatingBuddy />)

    const accountEntry = screen.getByTestId('account-entry')
    expect(accountEntry.closest('footer')).toHaveClass('buddy-sidebar-footer')

    fireEvent.click(accountEntry)

    expect(useAppStore.getState().windowMode).toBe('account')
    expect(accountEntry).toHaveAttribute('aria-current', 'page')
  })

  it('旧消息深链仍归入个人页入口', () => {
    useAppStore.getState().setWindowMode('messages')

    render(<FloatingBuddy />)

    expect(screen.getByTestId('account-entry')).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByTestId('messages-btn')).not.toBeInTheDocument()
  })

  it('支持折叠左侧菜单并持久化选择', () => {
    const values = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => values.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => values.set(key, value)),
      removeItem: vi.fn((key: string) => values.delete(key)),
      clear: vi.fn(() => values.clear()),
    })

    const { unmount } = render(<FloatingBuddy />)
    const sidebar = screen.getByTestId('floating-buddy')
    const accountEntry = screen.getByTestId('account-entry')
    const collapseButton = screen.getByRole('button', { name: '折叠左侧菜单' })

    expect(collapseButton).toHaveAttribute('aria-expanded', 'true')
    expect(accountEntry.querySelector('.buddy-account-entry__identity')).toBeInTheDocument()
    fireEvent.click(collapseButton)

    expect(sidebar).toHaveAttribute('data-collapsed', 'true')
    expect(screen.getByRole('button', { name: '展开左侧菜单' })).toHaveAttribute('aria-expanded', 'false')
    expect(accountEntry.querySelector('.buddy-account-entry__identity')).toBeInTheDocument()
    expect(localStorage.setItem).toHaveBeenCalledWith('memory-bread_sidebar_collapsed', 'true')
    expect(screen.getByTestId('creation-btn')).toHaveAttribute('title', '创作')

    unmount()
    render(<FloatingBuddy />)
    expect(screen.getByTestId('floating-buddy')).toHaveAttribute('data-collapsed', 'true')
    expect(screen.getByRole('button', { name: '展开左侧菜单' })).toBeInTheDocument()
    vi.unstubAllGlobals()
  })

  it('在左下角显示可直接触发的更新入口', () => {
    const onSoftwareUpdateClick = vi.fn()
    const softwareUpdate: SoftwareUpdateCheck = {
      current_version: '1.1.0',
      latest_version: '1.2.0',
      update_available: true,
      is_mandatory: false,
      release: {
        id: 'release-1',
        version: '1.2.0',
        build_number: 12,
        channel: 'stable',
        distribution: 'direct',
        platform: 'macos',
        architecture: 'universal',
        title: '记忆面包 1.2.0',
        release_notes: '更新说明',
        download_url: 'https://download.example.com/memorybread.app.tar.gz',
        rollout_percentage: 100,
        is_mandatory: false,
        status: 'published',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    }

    render(<FloatingBuddy softwareUpdate={softwareUpdate} onSoftwareUpdateClick={onSoftwareUpdateClick} />)
    const updateEntry = screen.getByTestId('software-update-entry')
    expect(updateEntry.closest('footer')).toHaveClass('buddy-sidebar-footer')
    expect(updateEntry).toHaveTextContent('v1.2.0')

    fireEvent.click(updateEntry)
    expect(onSoftwareUpdateClick).toHaveBeenCalledOnce()
  })
})

describe('BakeTabs', () => {
  it('按要求渲染新的标签顺序和文案', () => {
    const onChange = vi.fn()
    render(<BakeTabs current="overview" onChange={onChange} />)

    const tabs = screen.getAllByRole('button').map(button => button.textContent)
    expect(tabs).toEqual(['总览', '文档', '知识', '操作', '数据'])
    expect(screen.queryByText('高价值文档')).not.toBeInTheDocument()
  })
})
