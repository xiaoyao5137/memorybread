import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import BakePanel from '../components/BakePanel'
import { useAppStore } from '../store/useAppStore'

const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
})

const knowledgeItem = (id: number, isFavorite = false) => ({
  id,
  summary: `分页知识 ${id}`,
  overview: `知识 ${id} 的概述`,
  detailed_content: `知识 ${id} 的详细内容`,
  importance: 5,
  is_favorite: isFavorite,
  created_at: '2026-09-08 10:00:00',
  created_at_ms: 1,
})

const deferred = <T,>() => {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}

const installKnowledgeApi = (
  count = 23,
  favorites = false,
  respondToList: (url: URL, response: Response) => Response | Promise<Response> = (_url, response) => response,
) => {
  let items = Array.from({ length: count }, (_, index) => knowledgeItem(index + 1, favorites))
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    if (url.pathname === '/api/models') return jsonResponse({ ollama: true, llm: true, embedding: true })
    if (url.pathname === '/api/bake/overview') return jsonResponse({
      capture_count: 0, memory_count: 0, knowledge_count: items.length,
      template_count: 0, pending_candidates: 0, recent_activities: [],
    })
    if (url.pathname === '/api/bake/knowledge') {
      if (init?.method === 'POST') {
        const input = JSON.parse(String(init.body))
        const created = { ...knowledgeItem(Math.max(...items.map(item => item.id)) + 1), ...input }
        items = [created, ...items]
        return jsonResponse(created)
      }
      const limit = Number(url.searchParams.get('limit') ?? 20)
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const favorite = url.searchParams.get('favorite')
      const query = url.searchParams.get('q') ?? ''
      const filtered = items.filter(item => (
        (favorite === null || item.is_favorite === (favorite === 'true')) && item.summary.includes(query)
      ))
      return respondToList(url, jsonResponse({ items: filtered.slice(offset, offset + limit), total: filtered.length, limit, offset }))
    }
    if (url.pathname.startsWith('/api/bake/knowledge/') && init?.method === 'DELETE') {
      const id = Number(url.pathname.split('/').pop())
      items = items.filter(item => item.id !== id)
      return jsonResponse({ deleted: true })
    }
    if (url.pathname.startsWith('/api/memory-favorites/knowledge/')) {
      const id = Number(url.pathname.split('/').pop())
      const { is_favorite: isFavorite } = JSON.parse(String(init?.body))
      items = items.map(item => item.id === id ? { ...item, is_favorite: isFavorite } : item)
      return jsonResponse({ resource_kind: 'knowledge', resource_id: String(id), is_favorite: isFavorite })
    }
    throw new Error(`unexpected request: ${init?.method ?? 'GET'} ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const tableRows = () => within(screen.getByRole('table', { name: '知识表格' })).getAllByRole('row').slice(1)
const listRequestCount = (fetchMock: ReturnType<typeof installKnowledgeApi>) => fetchMock.mock.calls.filter(([input, init]) => (
  new URL(String(input)).pathname === '/api/bake/knowledge' && !init?.method
)).length

describe('知识记忆分页', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
    useAppStore.setState({ bakeTab: 'knowledge', bakeKnowledgeLimit: 10 })
  })

  it('23 条知识按 10、10、3 条翻页，切换每页 20 条后回到第一页', async () => {
    const fetchMock = installKnowledgeApi()
    render(<BakePanel />)

    await waitFor(() => expect(tableRows()).toHaveLength(10))
    expect(screen.getByRole('combobox', { name: '每页条数' })).toHaveValue('10')
    expect(screen.getByText('分页知识 1')).toBeInTheDocument()
    expect(screen.getByText('分页知识 10')).toBeInTheDocument()
    expect(screen.getByText('第 1/3 页')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('分页知识 11')).toBeInTheDocument()
    expect(tableRows()).toHaveLength(10)
    expect(screen.getByText('分页知识 20')).toBeInTheDocument()
    expect(screen.queryByText('分页知识 1')).not.toBeInTheDocument()
    expect(screen.getByText('第 2/3 页')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('分页知识 21')).toBeInTheDocument()
    expect(tableRows()).toHaveLength(3)
    expect(screen.getByText('分页知识 23')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
    expect(screen.getByText('第 3/3 页')).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: '每页条数' }), { target: { value: '20' } })
    await waitFor(() => expect(tableRows()).toHaveLength(20))
    expect(screen.getByText('分页知识 1')).toBeInTheDocument()
    expect(screen.getByText('分页知识 20')).toBeInTheDocument()
    expect(screen.getByText('第 1/2 页')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?limit=20&offset=0')
  })

  it('首屏新建知识后重新获取当前分页，仍展示 10 条且总数增加', async () => {
    const fetchMock = installKnowledgeApi()
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    const previousListRequests = listRequestCount(fetchMock)

    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    fireEvent.change(screen.getByPlaceholderText('简短描述这条知识'), { target: { value: '新建的分页知识' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    expect(await screen.findByText('新建的分页知识')).toBeInTheDocument()
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    expect(screen.getByText('共 24 条知识')).toBeInTheDocument()
    expect(listRequestCount(fetchMock)).toBeGreaterThan(previousListRequests)
    expect(screen.queryByText('分页知识 10')).not.toBeInTheDocument()
  })

  it('已收藏筛选中取消收藏后从下一页补齐 10 条，并同步筛选总数', async () => {
    const fetchMock = installKnowledgeApi(23, true)
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    fireEvent.change(screen.getByRole('combobox', { name: '收藏状态' }), { target: { value: 'favorite' } })
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?favorite=true&limit=10&offset=0')
      expect(tableRows()).toHaveLength(10)
    })

    fireEvent.click(screen.getByRole('button', { name: '查看知识「分页知识 1」详情' }))
    fireEvent.click(screen.getByRole('button', { name: '取消收藏' }))

    expect(await screen.findByText('分页知识 11')).toBeInTheDocument()
    expect(tableRows()).toHaveLength(10)
    expect(screen.queryByText('分页知识 1')).not.toBeInTheDocument()
    expect(screen.getByText('共 22 条知识')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '收藏状态' })).toHaveValue('favorite')
  })

  it('已收藏最后一页清空后回退有效页，并重新加载该页完整 10 条', async () => {
    installKnowledgeApi(23, true)
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    fireEvent.change(screen.getByRole('combobox', { name: '收藏状态' }), { target: { value: 'favorite' } })
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    fireEvent.change(screen.getByRole('spinbutton', { name: '跳转页码' }), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: '前往' }))
    await waitFor(() => expect(tableRows()).toHaveLength(3))

    for (const [index, id] of [21, 22, 23].entries()) {
      fireEvent.click(screen.getByRole('button', { name: `查看知识「分页知识 ${id}」详情` }))
      fireEvent.click(screen.getByRole('button', { name: '取消收藏' }))
      await waitFor(() => {
        expect(screen.getByText(`共 ${22 - index} 条知识`)).toBeInTheDocument()
        expect(tableRows()).toHaveLength(index === 2 ? 10 : 2 - index)
      })
    }

    expect(screen.getByText('第 2/2 页')).toBeInTheDocument()
    expect(screen.getByText('分页知识 11')).toBeInTheDocument()
    expect(screen.getByText('分页知识 20')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
  })

  it('从每页 20 条切换为 10 条时，请求完成前隐藏旧的 20 条记录', async () => {
    const nextPage = deferred<Response>()
    const fetchMock = installKnowledgeApi(23, false, (url, response) => (
      url.searchParams.get('limit') === '10' ? nextPage.promise : response
    ))
    useAppStore.setState({ bakeKnowledgeLimit: 20 })
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(20))

    fireEvent.change(screen.getByRole('combobox', { name: '每页条数' }), { target: { value: '10' } })
    expect(screen.getByRole('combobox', { name: '每页条数' })).toHaveValue('10')
    expect(fetchMock).toHaveBeenCalledWith('http://localhost:7070/api/bake/knowledge?limit=10&offset=0')
    expect(screen.queryByRole('table', { name: '知识表格' })).not.toBeInTheDocument()
    expect(screen.getByText('正在加载…')).toBeInTheDocument()
    expect(screen.queryByText('分页知识 20')).not.toBeInTheDocument()

    await act(async () => nextPage.resolve(jsonResponse({
      items: Array.from({ length: 10 }, (_, index) => knowledgeItem(index + 1)),
      total: 23, limit: 10, offset: 0,
    })))
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    expect(screen.getByText('分页知识 10')).toBeInTheDocument()
    expect(screen.queryByText('分页知识 11')).not.toBeInTheDocument()
  })

  it('删除后刷新 20 条分页的迟到响应不会覆盖已切换的 10 条分页', async () => {
    const oldRefresh = deferred<Response>()
    let oldResponse: Response | undefined
    let twentyItemRequests = 0
    installKnowledgeApi(23, false, (url, response) => {
      if (url.searchParams.get('limit') === '20') {
        twentyItemRequests += 1
        if (twentyItemRequests === 2) {
          oldResponse = response
          return oldRefresh.promise
        }
      }
      return response
    })
    useAppStore.setState({ bakeKnowledgeLimit: 20 })
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(20))
    fireEvent.click(screen.getByRole('button', { name: '查看知识「分页知识 1」详情' }))
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(oldResponse).toBeDefined())

    fireEvent.change(screen.getByRole('combobox', { name: '每页条数' }), { target: { value: '10' } })
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    expect(screen.getByText('分页知识 2')).toBeInTheDocument()
    expect(screen.getByText('分页知识 11')).toBeInTheDocument()

    await act(async () => oldRefresh.resolve(oldResponse!))
    expect(tableRows()).toHaveLength(10)
    expect(screen.getByText('共 22 条知识')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '每页条数' })).toHaveValue('10')
    expect(screen.queryByText('分页知识 21')).not.toBeInTheDocument()
  })

  it('从每页 20 条切换为 10 条请求失败后，不恢复旧的 20 条记录', async () => {
    const failedPage = deferred<Response>()
    installKnowledgeApi(23, false, (url, response) => (
      url.searchParams.get('limit') === '10' ? failedPage.promise : response
    ))
    useAppStore.setState({ bakeKnowledgeLimit: 20 })
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(20))
    fireEvent.change(screen.getByRole('combobox', { name: '每页条数' }), { target: { value: '10' } })

    await act(async () => failedPage.resolve(new Response('Knowledge unavailable', { status: 500 })))
    await waitFor(() => expect(screen.queryByText('正在加载…')).not.toBeInTheDocument())
    expect(screen.getByRole('combobox', { name: '每页条数' })).toHaveValue('10')
    expect(screen.queryByRole('table', { name: '知识表格' })).not.toBeInTheDocument()
    expect(screen.queryByText('分页知识 20')).not.toBeInTheDocument()
  })

  it('关键词筛选下新建不匹配知识后保留匹配的 10 条和筛选总数', async () => {
    const fetchMock = installKnowledgeApi()
    useAppStore.setState({ bakeKnowledgeQuery: '分页知识' })
    render(<BakePanel />)
    await waitFor(() => expect(tableRows()).toHaveLength(10))
    const previousListRequests = listRequestCount(fetchMock)
    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    fireEvent.change(screen.getByPlaceholderText('简短描述这条知识'), { target: { value: '不匹配的主题' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => {
      expect(listRequestCount(fetchMock)).toBeGreaterThan(previousListRequests)
      expect(tableRows()).toHaveLength(10)
    })
    expect(screen.getByText('共 23 条知识')).toBeInTheDocument()
    expect(screen.queryByText('不匹配的主题')).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText('搜索知识 ID、标题、内容、分类或来源 URL')).toHaveValue('分页知识')
  })
})
