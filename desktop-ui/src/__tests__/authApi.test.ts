import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  authenticateWithPassword,
  bindAccountContact,
  confirmPasswordReset,
  completeCloudSnapshotUpload,
  fetchCloudDevices,
  fetchCloudMessages,
  fetchCloudSnapshots,
  markAllCloudMessagesRead,
  markCloudMessageRead,
  sendEmailVerificationCode,
  sendAccountContactVerificationCode,
  sendPhoneVerificationCode,
  sendPasswordResetCode,
  upsertCloudDevice,
  updateUserProfile,
} from '../utils/authApi'
import { useAppStore } from '../store/useAppStore'

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

afterEach(() => {
  vi.restoreAllMocks()
})

beforeEach(() => {
  useAppStore.setState({ serviceEnvironment: 'production' })
})

describe('cloud device and snapshot API', () => {
  it('sends account name, nickname and company when registering with password auth', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      data: {
        access_token: 'mbs_token',
        expires_at: '2026-07-08T00:00:00Z',
        user: {
          id: '018f0000-0000-7000-8000-000000000004',
          username: '烘焙师土豆',
          email: 'tudou@memorybread.local',
          status: 'active',
          roles: ['user'],
          locale: 'zh-CN',
          timezone: 'Asia/Shanghai',
          created_at: '2026-07-07T00:00:00Z',
        },
      },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await authenticateWithPassword(
      'http://127.0.0.1:8080',
      'register',
      'tudou@memorybread.local',
      'MemoryBread@2026!',
      ' 烘焙师土豆 ',
      ' 土豆 ',
      ' 记忆面包科技 ',
      '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
      ' 123456 ',
    )

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8080/v1/auth/register', {
      method: 'POST',
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        email: 'tudou@memorybread.local',
        password: 'MemoryBread@2026!',
        username: '烘焙师土豆',
        nickname: '土豆',
        company_name: '记忆面包科技',
        email_verification: {
          challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
          code: '123456',
        },
      }),
    })
  })

  it('requests an email registration challenge with the selected environment', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      data: {
        challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
        retry_after_seconds: 60,
        expires_in_seconds: 600,
      },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(sendEmailVerificationCode(
      'http://127.0.0.1:8080',
      'xiaomai@example.com',
    )).resolves.toMatchObject({ retry_after_seconds: 60, expires_in_seconds: 600 })

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8080/v1/auth/email/send-code', {
      method: 'POST',
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email: 'xiaomai@example.com' }),
    })
  })

  it('preserves phone verification throttling metadata for the resend countdown', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(jsonResponse({
      error: {
        code: 'VERIFICATION_SEND_THROTTLED',
        message: '验证码发送过于频繁，请稍后再试',
        retry_after_seconds: 42,
      },
    }, 429)))

    await expect(sendPhoneVerificationCode(
      'https://memorybread.cn',
      '13800138000',
    )).rejects.toMatchObject({
      message: '验证码发送过于频繁，请稍后再试',
      status: 429,
      code: 'VERIFICATION_SEND_THROTTLED',
      retryAfterSeconds: 42,
    })
  })

  it('sends and confirms an authenticated email binding challenge', async () => {
    const updatedUser = {
      id: '018f0000-0000-7000-8000-000000000004',
      email: 'xiaomai@example.com',
      status: 'active',
      roles: ['user'],
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      created_at: '2026-07-07T00:00:00Z',
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        data: {
          challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
          retry_after_seconds: 60,
          expires_in_seconds: 600,
        },
      }))
      .mockResolvedValueOnce(jsonResponse({ data: updatedUser }))
    vi.stubGlobal('fetch', fetchMock)

    const challenge = await sendAccountContactVerificationCode(
      'http://127.0.0.1:8080',
      'mbs_token',
      'email',
      'xiaomai@example.com',
    )
    await expect(bindAccountContact(
      'http://127.0.0.1:8080',
      'mbs_token',
      'email',
      'xiaomai@example.com',
      '123456',
      challenge.challenge_id,
    )).resolves.toEqual(updatedUser)

    expect(fetchMock.mock.calls[0]).toEqual([
      'http://127.0.0.1:8080/v1/auth/bind/email/send-code',
      expect.objectContaining({
        method: 'POST',
        headers: {
          'X-MemoryBread-Environment': 'production',
          'X-MemoryBread-Client': 'desktop',
          Authorization: 'Bearer mbs_token',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email: 'xiaomai@example.com' }),
      }),
    ])
    expect(JSON.parse(String((fetchMock.mock.calls[1]?.[1] as RequestInit).body))).toEqual({
      email: 'xiaomai@example.com',
      email_verification: {
        challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb51',
        code: '123456',
      },
    })
  })

  it('sends and confirms a password reset with the selected environment', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        data: {
          challenge_id: '019c2c7e-706e-7a91-a61a-cd2a582cbb61',
          retry_after_seconds: 60,
          expires_in_seconds: 600,
        },
      }))
      .mockResolvedValueOnce(jsonResponse({ data: { ok: true } }))
    vi.stubGlobal('fetch', fetchMock)

    const challenge = await sendPasswordResetCode(
      'http://127.0.0.1:8080',
      'phone',
      '13800138000',
    )
    await expect(confirmPasswordReset('http://127.0.0.1:8080', {
      challenge_id: challenge.challenge_id,
      channel: 'phone',
      identifier: '13800138000',
      code: '123456',
      new_password: 'MemoryBread@2026!',
    })).resolves.toEqual({ ok: true })

    expect(fetchMock.mock.calls[0]).toEqual([
      'http://127.0.0.1:8080/v1/auth/password-reset/send-code',
      {
        method: 'POST',
        headers: {
          'X-MemoryBread-Environment': 'production',
          'X-MemoryBread-Client': 'desktop',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ channel: 'phone', identifier: '13800138000' }),
      },
    ])
    expect(fetchMock.mock.calls[1]?.[0]).toBe('http://127.0.0.1:8080/v1/auth/password-reset/confirm')
    expect(JSON.parse(String((fetchMock.mock.calls[1]?.[1] as RequestInit).body))).toEqual({
      challenge_id: challenge.challenge_id,
      channel: 'phone',
      identifier: '13800138000',
      code: '123456',
      new_password: 'MemoryBread@2026!',
    })
  })

  it('updates nickname and clears an empty company name', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      data: {
        id: '018f0000-0000-7000-8000-000000000004',
        username: '烘焙师土豆',
        nickname: '小麦',
        company_name: null,
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-07-07T00:00:00Z',
      },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await updateUserProfile(
      'http://127.0.0.1:8080',
      'mbs_token',
      ' 小麦 ',
      '  ',
    )

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8080/v1/auth/profile', {
      method: 'PUT',
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        Authorization: 'Bearer mbs_token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ nickname: '小麦', company_name: undefined }),
    })
  })

  it('explains when the account service does not support profile updates yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(null, { status: 404 })))

    await expect(updateUserProfile(
      'http://127.0.0.1:8080',
      'mbs_token',
      '小麦',
    )).rejects.toThrow('账户服务版本较旧，请更新或重启账户服务后重试。')
  })

  it('registers the current device with the account token', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      data: {
        id: 'device-1',
        name: 'MacBook',
        platform: 'macOS',
        client_version: '0.1.0',
        last_seen_at: '2026-07-02T12:00:00Z',
        revoked_at: null,
      },
    }))
    vi.stubGlobal('fetch', fetchMock)

    const device = await upsertCloudDevice('http://127.0.0.1:8080', 'mbs_token', {
      name: 'MacBook',
      platform: 'macOS',
      client_version: '0.1.0',
      public_key_base64: 'cHVibGljLWtleQ==',
    })

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8080/v1/devices', {
      method: 'POST',
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        Authorization: 'Bearer mbs_token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        name: 'MacBook',
        platform: 'macOS',
        client_version: '0.1.0',
        public_key_base64: 'cHVibGljLWtleQ==',
      }),
    })
    expect(device.id).toBe('device-1')
  })

  it('submits encrypted snapshot metadata after upload', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      data: {
        id: 'snapshot-1',
        device_id: 'device-1',
        encrypted_size: 42,
        status: 'committed',
        committed_at: '2026-07-02T12:00:00Z',
      },
    }))
    vi.stubGlobal('fetch', fetchMock)

    const snapshot = await completeCloudSnapshotUpload('http://127.0.0.1:8080', 'mbs_token', {
      device_id: 'device-1',
      encrypted_size: 42,
      oss_object_key: 'snapshots/user/device/file.bin',
      checksum_sha256: 'a'.repeat(64),
      format_version: 1,
      schema_version: 1,
      encryption_version: 1,
    })

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8080/v1/snapshots', expect.objectContaining({
      method: 'POST',
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        Authorization: 'Bearer mbs_token',
        'Content-Type': 'application/json',
      },
    }))
    expect(snapshot.status).toBe('committed')
  })

  it('reads cloud device and snapshot lists', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ data: [{ id: 'device-1' }] }))
      .mockResolvedValueOnce(jsonResponse({ data: [{ id: 'snapshot-1' }] }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchCloudDevices('http://127.0.0.1:8080', 'mbs_token')).resolves.toHaveLength(1)
    await expect(fetchCloudSnapshots('http://127.0.0.1:8080', 'mbs_token')).resolves.toHaveLength(1)
    expect(fetchMock).toHaveBeenNthCalledWith(1, 'http://127.0.0.1:8080/v1/devices', {
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        Authorization: 'Bearer mbs_token',
      },
    })
    expect(fetchMock).toHaveBeenNthCalledWith(2, 'http://127.0.0.1:8080/v1/snapshots', {
      headers: {
        'X-MemoryBread-Environment': 'production',
        'X-MemoryBread-Client': 'desktop',
        Authorization: 'Bearer mbs_token',
      },
    })
  })

  it('binds requests to the selected staging environment', async () => {
    useAppStore.setState({ serviceEnvironment: 'staging' })
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ data: [] }))
    vi.stubGlobal('fetch', fetchMock)

    await fetchCloudDevices('http://127.0.0.1:18080', 'mbs_token')

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:18080/v1/devices', {
      headers: {
        'X-MemoryBread-Environment': 'staging',
        'X-MemoryBread-Client': 'desktop',
        Authorization: 'Bearer mbs_token',
      },
    })
  })

  it('maps unready account service errors to user-safe copy', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(jsonResponse({
      error: {
        code: 'DATABASE_NOT_CONFIGURED',
        message: '账户服务尚未配置数据库',
      },
    }, 503)))

    await expect(fetchCloudDevices('http://127.0.0.1:8080', 'mbs_token')).rejects.toThrow('账户服务暂时未就绪')
  })

  it('reads and marks cloud messages with the account token', async () => {
    const message = {
      id: '018f0000-0000-7000-8000-000000000099',
      title: '新版本可用',
      body: '重启后即可更新。',
      category: 'product',
      priority: 'normal',
      read_at: null,
      published_at: '2026-07-24T08:00:00Z',
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        data: { items: [message], page: 1, page_size: 20, total: 1, unread_count: 1 },
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: { ...message, read_at: '2026-07-24T09:00:00Z' },
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: { updated_count: 0, read_at: '2026-07-24T09:00:00Z' },
      }))
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchCloudMessages(
      'http://127.0.0.1:8080',
      'mbs_token',
      { pageSize: 20, unreadOnly: true },
    )
    await markCloudMessageRead('http://127.0.0.1:8080', 'mbs_token', message.id)
    await markAllCloudMessagesRead('http://127.0.0.1:8080', 'mbs_token')

    expect(page.unread_count).toBe(1)
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:8080/v1/messages?page=1&page_size=20&unread_only=true',
      {
        headers: {
          'X-MemoryBread-Environment': 'production',
          'X-MemoryBread-Client': 'desktop',
          Authorization: 'Bearer mbs_token',
        },
      },
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://127.0.0.1:8080/v1/messages/${message.id}/read`,
      {
        method: 'PUT',
        headers: {
          'X-MemoryBread-Environment': 'production',
          'X-MemoryBread-Client': 'desktop',
          Authorization: 'Bearer mbs_token',
        },
      },
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      'http://127.0.0.1:8080/v1/messages/read-all',
      {
        method: 'PUT',
        headers: {
          'X-MemoryBread-Environment': 'production',
          'X-MemoryBread-Client': 'desktop',
          Authorization: 'Bearer mbs_token',
        },
      },
    )
  })
})
