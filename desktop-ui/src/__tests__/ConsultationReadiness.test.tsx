import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchConsultationReadiness, useConsultationReadiness } from '../hooks/useConsultationReadiness'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })
const ready = { ready: true, runtime: true, llm: true, embedding: true, message: '已就绪', action: 'models' }
describe('consultation capability', () => {
  it('fails closed for an old sidecar or offline service', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    expect((await fetchConsultationReadiness()).ready).toBe(false)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Load failed')))
    expect((await fetchConsultationReadiness()).action).toBe('retry')
  })
  it('keeps warmup distinct from initialization failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({
      ...ready, ready: false, action: 'retry', error_code: 'LOCAL_AI_WARMING_UP', message: '正在准备咨询能力',
    }) }))
    const status = await fetchConsultationReadiness()
    expect(status.ready).toBe(false)
    expect(status.action).toBe('retry')
    expect(status.error_code).toBe('LOCAL_AI_WARMING_UP')
  })
  it('recovers from an aborted probe without sending the user to repair', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockImplementationOnce((_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })).mockResolvedValue({ ok: true, json: async () => ready })
    vi.stubGlobal('fetch', fetcher)
    const pending = fetchConsultationReadiness()
    await vi.advanceTimersByTimeAsync(8000)
    expect((await pending).action).toBe('retry')
    expect((await fetchConsultationReadiness()).ready).toBe(true)
  })
  it('updates when the model becomes unavailable and recovers without remounting', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ready })
    vi.stubGlobal('fetch', fetcher)
    const { result } = renderHook(useConsultationReadiness)
    await waitFor(() => expect(result.current.ready).toBe(true))
    fetcher.mockResolvedValue({ ok: true, json: async () => ({ ...ready, ready: false }) })
    await act(async () => { await result.current.refresh() })
    expect(result.current.ready).toBe(false)
    fetcher.mockResolvedValue({ ok: true, json: async () => ready })
    await act(async () => { await result.current.refresh() })
    expect(result.current.ready).toBe(true)
  })
})
