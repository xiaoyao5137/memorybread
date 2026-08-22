import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ensureLocalNickname, LOCAL_NICKNAME_KEY } from '../utils/localIdentity'

describe('local identity', () => {
  beforeEach(() => {
    window.localStorage.removeItem(LOCAL_NICKNAME_KEY)
  })

  it('调用本地模型生成昵称并持久化', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ nickname: '倔强的牛角面包' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(ensureLocalNickname()).resolves.toBe('倔强的牛角面包')
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:7071/api/local-identity/nickname',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(window.localStorage.getItem(LOCAL_NICKNAME_KEY)).toBe('倔强的牛角面包')
  })

  it('已有安装昵称时不因账户变化重新生成', async () => {
    window.localStorage.setItem(LOCAL_NICKNAME_KEY, '慢烤的酸种面包')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(ensureLocalNickname()).resolves.toBe('慢烤的酸种面包')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
