import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AppMetadata } from '../utils/appMetadata'
import {
  getCustomerLogInstallationId,
  reportCustomerLogs,
  scrubDiagnosticLog,
} from '../utils/customerLogReport'
import { useAppStore } from '../store/useAppStore'

const metadata: AppMetadata = {
  product_name: '记忆面包',
  version: '0.1.3',
  build_number: '1',
  platform: 'macos',
  architecture: 'aarch64',
  distribution: 'direct',
  update_supported: true,
}

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

describe('customer log privacy', () => {
  beforeEach(() => window.localStorage.clear())

  it('scrubs common credentials and personal identifiers', () => {
    const source = [
      'path=/Users/alice/Library/Application Support/MemoryBread',
      String.raw`path=C:\Users\bob\AppData\Local\MemoryBread`,
      'Authorization: Bearer eyJhbGciOiJIUzI1Ni.test.signature',
      'api_key=sk-secretvalue123456789',
      'email=alice@example.com phone=13800138000',
      'password=hunter2',
    ].join('\n')

    const result = scrubDiagnosticLog(source)

    expect(result).not.toContain('alice')
    expect(result).not.toContain('bob')
    expect(result).not.toContain('example.com')
    expect(result).not.toContain('13800138000')
    expect(result).not.toContain('hunter2')
    expect(result).not.toContain('eyJhbGci')
    expect(result).toContain('[USER_HOME]')
    expect(result).toContain('[REDACTED_EMAIL]')
    expect(result).toContain('[REDACTED_PHONE]')
  })

  it('keeps a stable anonymous installation identifier', () => {
    const first = getCustomerLogInstallationId()
    const second = getCustomerLogInstallationId()

    expect(second).toBe(first)
    expect(first).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('uploads the core log-files response envelope and completes the report', async () => {
    useAppStore.setState({ serviceEnvironment: 'production' })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        items: [{
          key: 'core',
          label: '核心服务日志',
          exists: true,
          size_bytes: 42,
          modified_at: 1,
        }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        key: 'core',
        label: '核心服务日志',
        content: 'startup complete',
        truncated: false,
        total_size_bytes: 16,
        returned_bytes: 16,
        modified_at: 1,
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: {
          upload_id: '018f0000-0000-7000-8000-000000000001',
          oss_object_key: 'customer-logs/production/2026/08/17/report.zip',
          upload_url: 'https://example-bucket.oss.example.com/report.zip',
          required_headers: { 'content-type': 'application/zip' },
        },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse({
        data: {
          log_id: '018f0000-0000-7000-8000-000000000001',
          received_at: '2026-08-17T05:00:00Z',
          duplicate: false,
        },
      }))
    vi.stubGlobal('fetch', fetchMock)

    const receipt = await reportCustomerLogs({
      adminApiBaseUrl: 'https://memorybread.cn',
      localApiBaseUrl: 'http://127.0.0.1:7070/',
      metadata,
      description: '无法启动',
    })

    expect(receipt.log_id).toBe('018f0000-0000-7000-8000-000000000001')
    expect(fetchMock).toHaveBeenCalledTimes(5)
    expect(fetchMock.mock.calls[0][0]).toBe('http://127.0.0.1:7070/api/debug/log-files')
    expect(fetchMock.mock.calls[2][0]).toBe('https://memorybread.cn/v1/customer-logs/upload-url')
    expect(fetchMock.mock.calls[3][1]).toMatchObject({
      method: 'PUT',
      headers: { 'content-type': 'application/zip' },
    })
    expect(fetchMock.mock.calls[4][0]).toBe('https://memorybread.cn/v1/customer-logs')
    expect(fetchMock.mock.calls[4][1]).toMatchObject({ method: 'POST' })
    expect(JSON.parse(String(fetchMock.mock.calls[4][1]?.body))).toMatchObject({
      upload_id: '018f0000-0000-7000-8000-000000000001',
      oss_object_key: 'customer-logs/production/2026/08/17/report.zip',
      platform: 'macos',
      architecture: 'aarch64',
      description: '无法启动',
    })
  })
})
