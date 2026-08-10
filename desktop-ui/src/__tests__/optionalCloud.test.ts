import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createOptionalCloudRequestSignal,
  optionalCloudIsReachable,
} from '../utils/optionalCloud'

describe('optional cloud runtime', () => {
  beforeEach(() => {
    vi.useRealTimers()
    Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: true })
  })

  it('does not consider cloud reachable when the operating system reports offline', () => {
    Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: false })
    expect(optionalCloudIsReachable()).toBe(false)
  })

  it('aborts an optional cloud request at its deadline', () => {
    vi.useFakeTimers()
    const request = createOptionalCloudRequestSignal(undefined, 250)

    expect(request.signal.aborted).toBe(false)
    vi.advanceTimersByTime(250)
    expect(request.signal.aborted).toBe(true)

    request.dispose()
  })

  it('inherits cancellation from its local lifecycle', () => {
    const lifecycle = new AbortController()
    const request = createOptionalCloudRequestSignal(lifecycle.signal)

    lifecycle.abort()

    expect(request.signal.aborted).toBe(true)
    request.dispose()
  })
})
