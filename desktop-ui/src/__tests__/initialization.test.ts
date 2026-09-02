import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchRuntimeReadiness } from '../utils/initialization'

describe('fetchRuntimeReadiness', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('AI 管线仍在预热时不阻塞已初始化应用启动', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === 'http://127.0.0.1:7070/health') {
        return {
          ok: true,
          json: async () => ({ status: 'ok', service: 'memory-bread-core', version: '0.1.0' }),
        }
      }
      if (url === 'http://127.0.0.1:7071/health') {
        return {
          ok: true,
          json: async () => ({ status: 'ok', pipeline_ready: false }),
        }
      }
      throw new Error(`unexpected request: ${url}`)
    }))

    await expect(fetchRuntimeReadiness()).resolves.toBe(true)
  })

  it('本地服务不可用时仍保持启动门禁', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => ({
      ok: String(input) !== 'http://127.0.0.1:7070/health',
      json: async () => ({ status: 'ok' }),
    })))

    await expect(fetchRuntimeReadiness()).resolves.toBe(false)
  })
})
