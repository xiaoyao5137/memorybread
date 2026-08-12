import { beforeEach, describe, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import {
  CLOUD_DEVICE_ID_KEY,
  CLOUD_DEVICE_PUBLIC_KEY,
  cloudDevicePublicKeyStorageKey,
  cloudDeviceReportStatusStorageKey,
  cloudDeviceStorageKey,
  fetchSoftwareUpdate,
  getSoftwareUpdateCohort,
  registerCurrentDevice,
  shouldShowSoftwareUpdate,
  snoozeSoftwareUpdate,
  type SoftwareUpdateCheck,
} from '../utils/softwareUpdate'
import { useAppStore } from '../store/useAppStore'

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(),
}))

const invokeMock = vi.mocked(invoke)

const appMetadata = {
  product_name: '记忆面包',
  version: '1.0.0',
  build_number: '1',
  platform: 'macos' as const,
  architecture: 'aarch64' as const,
  distribution: 'direct' as const,
  update_supported: true,
}

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

const update: SoftwareUpdateCheck = {
  current_version: '1.0.0',
  latest_version: '1.1.0',
  update_available: true,
  is_mandatory: false,
  release: {
    id: 'release-1',
    version: '1.1.0',
    build_number: 2,
    channel: 'stable',
    distribution: 'direct',
    platform: 'macos',
    architecture: 'universal',
    title: '记忆面包 1.1.0',
    release_notes: '改进更新体验。',
    download_url: 'https://download.example.com/memorybread.dmg',
    rollout_percentage: 100,
    is_mandatory: false,
    status: 'published',
    created_at: '2026-07-24T00:00:00Z',
    updated_at: '2026-07-24T00:00:00Z',
  },
}

describe('software update runtime', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
    invokeMock.mockResolvedValue(appMetadata)
    useAppStore.setState({ serviceEnvironment: 'production' })
  })

  it('shows a new version and snoozes the same optional release', () => {
    expect(shouldShowSoftwareUpdate(update)).toBe(true)
    snoozeSoftwareUpdate(update.latest_version)
    expect(shouldShowSoftwareUpdate(update)).toBe(false)
  })

  it('does not suppress mandatory updates', () => {
    snoozeSoftwareUpdate(update.latest_version)
    expect(shouldShowSoftwareUpdate({ ...update, is_mandatory: true })).toBe(true)
  })

  it('sends only version targeting fields when checking for updates', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ data: update }), { status: 200 }))
    await fetchSoftwareUpdate('https://api.example.com', {
      product_name: '记忆面包',
      version: '1.0.0',
      build_number: '1',
      platform: 'macos',
      architecture: 'aarch64',
      distribution: 'direct',
      update_supported: true,
    })

    const requestedUrl = String(fetchMock.mock.calls[0][0])
    expect(requestedUrl).toContain('/v1/software-updates/check?')
    expect(requestedUrl).toContain('current_version=1.0.0')
    expect(requestedUrl).toContain('platform=macos')
    expect(requestedUrl).toContain('architecture=aarch64')
    expect(requestedUrl).toContain('distribution=direct')
    const requestHeaders = fetchMock.mock.calls[0][1]?.headers as Record<string, string>
    expect(requestHeaders['X-MemoryBread-Update-Cohort']).toMatch(/^[0-9a-f]{64}$/)
  })

  it('keeps a stable anonymous rollout cohort on the device', () => {
    const first = getSoftwareUpdateCohort()
    const second = getSoftwareUpdateCohort()

    expect(first).toMatch(/^[0-9a-f]{64}$/)
    expect(second).toBe(first)
  })

  it('stores device identities separately for each environment and account', async () => {
    const fetchMock = vi.fn().mockImplementation(async (_url, init?: RequestInit) => {
      const request = JSON.parse(String(init?.body)) as { device_id: string; client_version: string }
      return jsonResponse({
        data: {
          id: request.device_id,
          name: '记忆面包 macos',
          platform: 'macos',
          client_version: request.client_version,
          last_seen_at: '2026-08-12T00:00:00Z',
          revoked_at: null,
        },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const productionScope = { environment: 'production' as const, userId: 'user-a' }
    const stagingScope = { environment: 'staging' as const, userId: 'user-a' }
    const productionDevice = await registerCurrentDevice(
      'https://memorybread.cn',
      'production-token',
      productionScope,
    )
    const stagingDevice = await registerCurrentDevice(
      'http://127.0.0.1:18080',
      'staging-token',
      stagingScope,
    )

    expect(productionDevice.id).not.toBe(stagingDevice.id)
    expect(window.localStorage.getItem(cloudDeviceStorageKey(productionScope))).toBe(productionDevice.id)
    expect(window.localStorage.getItem(cloudDeviceStorageKey(stagingScope))).toBe(stagingDevice.id)
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toMatchObject({
      'X-MemoryBread-Environment': 'production',
    })
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toMatchObject({
      'X-MemoryBread-Environment': 'staging',
    })
  })

  it('rotates a legacy device identity once when another account owns it', async () => {
    const legacyDeviceId = '018f0000-0000-7000-8000-000000000001'
    const legacyPublicKey = btoa('legacy-device-public-key-material')
    window.localStorage.setItem(CLOUD_DEVICE_ID_KEY, legacyDeviceId)
    window.localStorage.setItem(CLOUD_DEVICE_PUBLIC_KEY, legacyPublicKey)
    const scope = { environment: 'production' as const, userId: 'user-b' }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        error: {
          code: 'DEVICE_OWNERSHIP_CONFLICT',
          message: '设备标识已被占用',
        },
      }, 409))
      .mockImplementationOnce(async (_url, init?: RequestInit) => {
        const request = JSON.parse(String(init?.body)) as {
          device_id: string
          public_key_base64: string
          client_version: string
        }
        return jsonResponse({
          data: {
            id: request.device_id,
            name: '记忆面包 macos',
            platform: 'macos',
            client_version: request.client_version,
            last_seen_at: '2026-08-12T00:00:00Z',
            revoked_at: null,
          },
        })
      })
    vi.stubGlobal('fetch', fetchMock)

    const device = await registerCurrentDevice(
      'https://memorybread.cn',
      'production-token',
      scope,
    )
    const firstBody = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))
    const secondBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))
    const reportStatus = JSON.parse(String(
      window.localStorage.getItem(cloudDeviceReportStatusStorageKey(scope)),
    ))

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(firstBody.device_id).toBe(legacyDeviceId)
    expect(secondBody.device_id).not.toBe(legacyDeviceId)
    expect(secondBody.public_key_base64).not.toBe(legacyPublicKey)
    expect(window.localStorage.getItem(cloudDeviceStorageKey(scope))).toBe(device.id)
    expect(window.localStorage.getItem(cloudDevicePublicKeyStorageKey(scope))).toBe(secondBody.public_key_base64)
    expect(reportStatus).toMatchObject({ status: 'success', client_version: '1.0.0' })
  })

  it('reuses the scoped candidate when a device response is lost', async () => {
    const scope = { environment: 'production' as const, userId: 'user-c' }
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('connection reset'))
      .mockImplementationOnce(async (_url, init?: RequestInit) => {
        const request = JSON.parse(String(init?.body)) as { device_id: string; client_version: string }
        return jsonResponse({
          data: {
            id: request.device_id,
            name: '记忆面包 macos',
            platform: 'macos',
            client_version: request.client_version,
            last_seen_at: '2026-08-12T00:00:00Z',
            revoked_at: null,
          },
        })
      })
    vi.stubGlobal('fetch', fetchMock)

    await expect(registerCurrentDevice(
      'https://memorybread.cn',
      'production-token',
      scope,
    )).rejects.toThrow('账户服务暂时无法连接')
    const firstBody = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))

    const device = await registerCurrentDevice(
      'https://memorybread.cn',
      'production-token',
      scope,
    )
    const secondBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))

    expect(secondBody.device_id).toBe(firstBody.device_id)
    expect(device.id).toBe(firstBody.device_id)
  })
})
