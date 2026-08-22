import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { invoke } from '@tauri-apps/api/core'
import FloatingBuddy from '../components/FloatingBuddy'
import SoftwareUpdateNotice from '../components/SoftwareUpdateNotice'
import { useAppStore } from '../store/useAppStore'
import { useSoftwareUpdateSession } from '../utils/softwareUpdateSession'
import type { SoftwareUpdateCheck } from '../utils/softwareUpdate'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn(() => Promise.resolve(() => {})) }))

const invokeMock = vi.mocked(invoke)

const appMetadata = {
  product_name: '记忆面包',
  version: '1.0.0',
  build_number: '1',
  platform: 'macos' as const,
  architecture: 'aarch64' as const,
  distribution: 'direct' as const,
  update_supported: true,
}

const optionalUpdate: SoftwareUpdateCheck = {
  current_version: '1.0.0',
  latest_version: '1.1.0',
  update_available: true,
  is_mandatory: false,
  release: {
    id: 'release-1',
    version: '1.1.0',
    build_number: 2,
    channel: 'stable',
    distribution: 'direct',
    platform: 'macos',
    architecture: 'universal',
    title: '记忆面包 1.1.0',
    release_notes: '改进更新体验。',
    download_url: 'https://download.example.com/memorybread.app.tar.gz',
    rollout_percentage: 100,
    is_mandatory: false,
    status: 'published',
    created_at: '2026-08-19T00:00:00Z',
    updated_at: '2026-08-19T00:00:00Z',
  },
}

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.setState({ serviceEnvironment: 'production', adminApiBaseUrl: 'https://api.example.com' })
  useSoftwareUpdateSession.setState({ version: null, phase: 'idle', progress: null, error: '' })
  invokeMock.mockImplementation((command: string) =>
    command === 'get_app_metadata' ? Promise.resolve(appMetadata) : Promise.resolve(null))
})

describe('后台软件更新会话', () => {
  it('非强制更新下载中允许关闭弹窗，并提供后台继续入口', async () => {
    useSoftwareUpdateSession.setState({
      version: '1.1.0',
      phase: 'downloading',
      progress: { phase: 'downloading', downloaded_bytes: 42, total_bytes: 100, percent: 42 },
      error: '',
    })
    const onDismiss = vi.fn()
    render(<SoftwareUpdateNotice update={optionalUpdate} onDismiss={onDismiss} />)

    // 右上角关闭按钮与底部按钮都叫“后台继续更新”，两者都应可用。
    const backgroundButtons = await screen.findAllByRole('button', { name: '后台继续更新' })
    expect(backgroundButtons.length).toBe(2)
    await waitFor(() => {
      expect(screen.getAllByText('正在下载 42%').length).toBeGreaterThanOrEqual(1)
    })

    fireEvent.click(backgroundButtons[1])
    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('强制更新进行中仍然不允许关闭弹窗', async () => {
    useSoftwareUpdateSession.setState({
      version: '1.1.0',
      phase: 'downloading',
      progress: { phase: 'downloading', downloaded_bytes: 1, total_bytes: 100, percent: 1 },
      error: '',
    })
    render(<SoftwareUpdateNotice update={{ ...optionalUpdate, is_mandatory: true }} onDismiss={vi.fn()} />)

    await waitFor(() => {
      expect(screen.getAllByText('正在下载 1%').length).toBeGreaterThanOrEqual(1)
    })
    expect(screen.queryByRole('button', { name: '后台继续更新' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '24 小时后提醒' })).not.toBeInTheDocument()
  })

  it('左下角入口承接后台下载进度', () => {
    useSoftwareUpdateSession.setState({
      version: '1.1.0',
      phase: 'downloading',
      progress: { phase: 'downloading', downloaded_bytes: 42, total_bytes: 100, percent: 42 },
      error: '',
    })
    const onSoftwareUpdateClick = vi.fn()
    render(<FloatingBuddy softwareUpdate={optionalUpdate} onSoftwareUpdateClick={onSoftwareUpdateClick} />)

    const entry = screen.getByTestId('software-update-entry')
    expect(entry).toHaveTextContent('更新中 42%')
    expect(entry).toHaveTextContent('v1.1.0')
    expect(entry).toHaveClass('buddy-update-entry--busy')

    fireEvent.click(entry)
    expect(onSoftwareUpdateClick).toHaveBeenCalledOnce()
  })

  it('更新就绪时左下角入口引导重启', () => {
    useSoftwareUpdateSession.setState({ version: '1.1.0', phase: 'ready_to_restart', progress: null, error: '' })
    render(<FloatingBuddy softwareUpdate={optionalUpdate} onSoftwareUpdateClick={vi.fn()} />)

    const entry = screen.getByTestId('software-update-entry')
    expect(entry).toHaveTextContent('待重启完成更新')
    expect(entry).toHaveClass('buddy-update-entry--ready_to_restart')
  })

  it('更新失败时左下角入口允许重新打开详情', () => {
    useSoftwareUpdateSession.setState({
      version: '1.1.0',
      phase: 'failed',
      progress: null,
      error: '更新包下载或签名校验失败',
    })
    render(<FloatingBuddy softwareUpdate={optionalUpdate} onSoftwareUpdateClick={vi.fn()} />)

    const entry = screen.getByTestId('software-update-entry')
    expect(entry).toHaveTextContent('更新失败')
    expect(entry).toHaveClass('buddy-update-entry--failed')
  })
})
