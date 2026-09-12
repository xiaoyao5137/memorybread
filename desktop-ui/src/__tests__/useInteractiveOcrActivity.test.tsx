import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { useInteractiveOcrActivity } from '../hooks/useInteractiveOcrActivity'
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn().mockResolvedValue(undefined) }))
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers() })
it('keeps a lease across the operation and releases it on completion', async () => {
  vi.useFakeTimers()
  const { rerender, unmount } = renderHook(({ active }) => useInteractiveOcrActivity(active), { initialProps: { active: true } })
  await act(async () => {})
  const id = (vi.mocked(invoke).mock.calls[0][1] as any).activityId
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(invoke).toHaveBeenCalledTimes(2)
  rerender({ active: false })
  await act(async () => {})
  expect(invoke).toHaveBeenLastCalledWith('set_interactive_ocr_activity', { activityId: id, active: false })
  unmount()
})
it('releases after pending renewal and uses independent IDs for simultaneous operations', async () => {
  let finish!: () => void
  vi.mocked(invoke).mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve(undefined) }))
  const a = renderHook(() => useInteractiveOcrActivity(true))
  const first = (vi.mocked(invoke).mock.calls[0][1] as any).activityId
  const b = renderHook(() => useInteractiveOcrActivity(true))
  const second = (vi.mocked(invoke).mock.calls[1][1] as any).activityId
  expect(first).not.toBe(second)
  a.unmount()
  await act(async () => { finish() })
  expect(invoke).toHaveBeenCalledWith('set_interactive_ocr_activity', { activityId: first, active: false })
  b.unmount()
  await act(async () => {})
})
