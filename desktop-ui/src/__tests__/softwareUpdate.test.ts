import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchSoftwareUpdate,
  getSoftwareUpdateCohort,
  shouldShowSoftwareUpdate,
  snoozeSoftwareUpdate,
  type SoftwareUpdateCheck,
} from '../utils/softwareUpdate'

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
})
