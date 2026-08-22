import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import RepositoryPanel from '../components/RepositoryPanel'
import { useAppStore } from '../store/useAppStore'

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

describe('采集与时间线删除', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('确认后删除时间线并刷新当前列表', async () => {
    let deleted = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/bake/memories/7' && init?.method === 'DELETE') {
        deleted = true
        return new Response(null, { status: 204 })
      }
      if (url.includes('/api/knowledge')) {
        return jsonResponse({
          entries: deleted ? [] : [{
            id: 7,
            summary: '待删除时间线',
            overview: '时间线摘要',
            capture_id: 42,
            capture_ids: [],
            importance: 4,
            created_at: '2026-07-27 09:30:00',
            created_at_ms: 1,
          }],
          total: deleted ? 0 : 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.includes('/api/bake/documents') || url.includes('/api/bake/knowledge') || url.includes('/api/bake/sops')) {
        return jsonResponse({ items: [], total: 0, limit: 1000, offset: 0 })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    useAppStore.setState({ repositoryTab: 'memory' })

    render(<RepositoryPanel />)

    expect((await screen.findAllByText('待删除时间线')).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: '查看时间线：待删除时间线' }))
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(screen.getByRole('alertdialog', { name: '删除时间线？' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith(
      'http://localhost:7070/api/bake/memories/7',
      { method: 'DELETE' },
    )
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        'http://localhost:7070/api/bake/memories/7',
        { method: 'DELETE' },
      )
    })
    expect(await screen.findByText('已删除时间线')).toBeInTheDocument()
    expect(screen.getByText('还没有时间线')).toBeInTheDocument()
  })

  it('确认后删除采集记录并清除详情', async () => {
    let deleted = false
    const capture = {
      id: 11,
      ts: 1_721_000_000_000,
      app_name: 'Chrome',
      win_title: '待删除采集记录',
      event_type: 'manual',
      ax_text: '原始采集正文',
      is_sensitive: false,
      pii_scrubbed: false,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === 'http://localhost:7070/api/bake/captures/11' && init?.method === 'DELETE') {
        deleted = true
        return new Response(null, { status: 204 })
      }
      if (url === 'http://localhost:7070/api/bake/captures/11') {
        return jsonResponse(capture)
      }
      if (url.includes('/api/bake/captures')) {
        return jsonResponse({
          items: deleted ? [] : [capture],
          total: deleted ? 0 : 1,
          limit: 20,
          offset: 0,
        })
      }
      throw new Error(`unexpected url: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    useAppStore.setState({ repositoryTab: 'capture' })

    render(<RepositoryPanel />)

    expect((await screen.findAllByText('待删除采集记录')).length).toBeGreaterThan(0)
    expect(screen.queryByText('尚未归入时间线')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看采集记录 #11 详情' }))
    const deleteButton = screen.getByRole('button', { name: '删除' })
    expect(deleteButton).toHaveClass('bake-btn--compact')
    fireEvent.click(deleteButton)
    expect(screen.getByRole('alertdialog', { name: '删除采集记录？' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalledWith(
      'http://localhost:7070/api/bake/captures/11',
      { method: 'DELETE' },
    )
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        'http://localhost:7070/api/bake/captures/11',
        { method: 'DELETE' },
      )
    })
    expect(await screen.findByText('已删除采集记录')).toBeInTheDocument()
    expect(screen.getByText('暂无采集记录')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '待删除采集记录' })).not.toBeInTheDocument()
  })
})
