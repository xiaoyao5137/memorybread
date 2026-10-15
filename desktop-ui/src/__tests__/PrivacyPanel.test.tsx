import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import PrivacyPanel from '../components/PrivacyPanel'
import type { AppBlacklistRecord } from '../types'

let fetchMock: ReturnType<typeof vi.fn>
let blacklistRows: AppBlacklistRecord[]
let postError: string | null
let failReloadAfterPost: boolean

beforeEach(() => {
  blacklistRows = [{
    id: 1,
    bundle_id: 'com.example.private',
    app_name: '私密应用',
    enabled: true,
    reason: '包含私密内容',
    created_at: '2026-07-18T00:00:00Z',
    updated_at: '2026-07-18T00:00:00Z',
    week_blocked: 3,
  }]
  postError = null
  failReloadAfterPost = false
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/api/privacy/blacklist')) {
      if (init?.method === 'POST') {
        if (postError) {
          return { ok: false, json: async () => ({ error: 'BAD_REQUEST', message: postError }) }
        }
        const payload = JSON.parse(String(init.body))
        blacklistRows.push({
          id: 2,
          bundle_id: payload.bundle_id ?? null,
          app_name: payload.app_name,
          enabled: true,
          reason: payload.reason ?? null,
          created_at: '2026-07-18T00:00:00Z',
          updated_at: '2026-07-18T00:00:00Z',
          week_blocked: 0,
        })
        return { ok: true, json: async () => ({ success: true, data: 2 }) }
      }
      if (failReloadAfterPost && blacklistRows.length > 1) {
        return { ok: false, json: async () => ({ error: 'INTERNAL_ERROR' }) }
      }
      return {
        ok: true,
        json: async () => ({ data: [...blacklistRows] }),
      }
    }

    return {
      ok: true,
      json: async () => ({
        data: [
          {
            id: 1,
            filter_type: 'chat',
            filter_name: '聊天敏感内容',
            enabled: true,
            config_json: null,
            updated_at: '2026-07-18T00:00:00Z',
            week_blocked: 5,
          },
          {
            id: 2,
            filter_type: 'other',
            filter_name: '其它敏感信息过滤',
            enabled: true,
            config_json: '{"keywords":["成人信息","色情信息"]}',
            updated_at: '2026-07-18T00:00:00Z',
            week_blocked: 2,
          },
        ],
      }),
    }
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('PrivacyPanel', () => {
  it('在撑满式布局中展示规则、黑名单和带标签的添加表单', async () => {
    const { container } = render(<PrivacyPanel />)

    expect(await screen.findByRole('heading', { name: '隐私设置' })).toBeInTheDocument()
    expect(container.querySelector('.privacy-grid')).toBeInTheDocument()
    expect(container.querySelectorAll('.privacy-section')).toHaveLength(2)
    expect(screen.getByText('聊天敏感内容')).toBeInTheDocument()
    expect(screen.getByText('其它敏感信息过滤')).toBeInTheDocument()
    expect(screen.getByText(/其它兜底性质的敏感内容过滤/)).toBeInTheDocument()
    expect(screen.getByText('私密应用')).toBeInTheDocument()
    expect(screen.getByLabelText(/Bundle ID.*选填/)).toBeInTheDocument()
    expect(screen.getByLabelText(/应用名称.*必填/)).toBeRequired()
    expect(screen.getByLabelText(/排除原因/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '聊天敏感内容已开启' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: '其它敏感信息过滤已开启' })).toBeChecked()
  })

  it('仅填写应用名称也能添加，并明确显示按名称匹配', async () => {
    render(<PrivacyPanel />)
    const appName = await screen.findByLabelText(/应用名称.*必填/)
    fireEvent.change(appName, { target: { value: '  钉钉  ' } })
    fireEvent.change(screen.getByLabelText(/排除原因/), { target: { value: '  隐私聊天  ' } })
    fireEvent.click(screen.getByRole('button', { name: '添加应用' }))

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
      expect(post).toBeDefined()
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({
        app_name: '钉钉',
        reason: '隐私聊天',
      })
    })
    expect(await screen.findByRole('status')).toHaveTextContent('已添加“钉钉”')
    const row = screen.getByText('钉钉').closest('.privacy-list-item')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).getByText(/按应用名称匹配/)).toBeInTheDocument()
    expect(appName).toHaveValue('')
  })

  it('提供 Bundle ID 时继续连同应用名称提交', async () => {
    render(<PrivacyPanel />)
    fireEvent.change(await screen.findByLabelText(/Bundle ID.*选填/), {
      target: { value: '  com.alibaba.DingTalkMac  ' },
    })
    fireEvent.change(screen.getByLabelText(/应用名称.*必填/), { target: { value: '钉钉' } })
    fireEvent.click(screen.getByRole('button', { name: '添加应用' }))

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({
        app_name: '钉钉',
        bundle_id: 'com.alibaba.DingTalkMac',
      })
    })
  })

  it('只拒绝空应用名称，并显示表单内校验提示', async () => {
    render(<PrivacyPanel />)
    const appName = await screen.findByLabelText(/应用名称.*必填/)
    fireEvent.change(appName, { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: '添加应用' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('请填写应用名称')
    expect(appName).toHaveAttribute('aria-invalid', 'true')
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('提交失败时保留输入并显示服务端原因', async () => {
    postError = '该应用名称已经添加'
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<PrivacyPanel />)
    const appName = await screen.findByLabelText(/应用名称.*必填/)
    fireEvent.change(appName, { target: { value: '钉钉' } })
    fireEvent.click(screen.getByRole('button', { name: '添加应用' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('该应用名称已经添加')
    expect(appName).toHaveValue('钉钉')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('添加成功但列表刷新失败时不显示完成提示', async () => {
    failReloadAfterPost = true
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<PrivacyPanel />)
    fireEvent.change(await screen.findByLabelText(/应用名称.*必填/), { target: { value: '钉钉' } })
    fireEvent.click(screen.getByRole('button', { name: '添加应用' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('隐私设置加载失败')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
