import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import AccountProfile from '../components/AccountProfile'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AccountProfile breadcrumbs', () => {
  it('shows the local breadcrumb inventory without waiting for rule sync', async () => {
    const onInitialSectionHandled = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/breadcrumbs')) {
        return {
          ok: true,
          json: async () => ({
              breadcrumbs: [{
                breadcrumb: {
                  id: 'badge-1',
                  breadcrumb_key: 'overnight_writer',
                  name: '通宵赶稿',
                  tagline: '月落前还在落笔',
                  description: '一个自然周内，曾在某个本地夜晚从 0 点到 6 点保持连续有效工作。',
                  icon_key: 'moon',
                  palette_key: 'midnight',
                  rarity: 'rare',
                },
                quantity: 1,
                first_earned_at: 1753056000000,
                last_earned_at: 1753056000000,
              }],
              equipped: {},
          }),
        }
      }
      if (url.includes('/v1/messages')) {
        return {
          ok: true,
          json: async () => ({
            data: {
              items: [{
                id: 'message-1',
                title: '系统维护完成',
                body: '服务已经恢复。',
                category: 'system',
                priority: 'normal',
                read_at: null,
                published_at: '2026-07-24T08:00:00Z',
              }],
              page: 1,
              page_size: 50,
              total: 1,
              unread_count: 1,
            },
          }),
        }
      }
      if (url.includes('/api/work-profile')) {
        return new Promise(() => undefined)
      }
      throw new Error(`unexpected request: ${url}`)
    }))

    render(<AccountProfile
      accountLabel="普通账户"
      adminApiBaseUrl="http://127.0.0.1:8080"
      apiBaseUrl="http://127.0.0.1:7070"
      authToken="mbs_token"
      balanceError={null}
      cloudBalance={null}
      highlightedAchievementKeys={['overnight_writer']}
      initialSection="achievements"
      onInitialSectionHandled={onInitialSectionHandled}
      onLogout={vi.fn()}
      onUserChange={vi.fn()}
      runModeLabel="本地模式"
      user={{
        id: 'user-1',
        username: '小麦',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-07-21T00:00:00Z',
      }}
    />)

    expect(screen.getByRole('tab', { name: '面包屑' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('通宵赶稿')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: '通宵赶稿，刚刚获得' })).toBeInTheDocument()
    expect(screen.getByText('刚刚获得')).toBeInTheDocument()
    expect(onInitialSectionHandled).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('一个自然周内，曾在某个本地夜晚从 0 点到 6 点保持连续有效工作。')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '查看「通宵赶稿」面包屑详情' }))

    expect(screen.getByRole('dialog', { name: '通宵赶稿' })).toBeInTheDocument()
    expect(screen.getByText('一个自然周内，曾在某个本地夜晚从 0 点到 6 点保持连续有效工作。')).toBeInTheDocument()
    expect(screen.getByText('这是一枚通宵纪念卡。完成赶稿后，请尽快补充睡眠。')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '关闭「通宵赶稿」面包屑详情' }))
    fireEvent.click(screen.getByRole('tab', { name: '消息' }))

    expect(screen.getByRole('tab', { name: '消息' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('系统维护完成')).toBeInTheDocument()
  })
})
