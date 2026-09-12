import { beforeEach, describe, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import type { AppMetadata } from '../utils/appMetadata'
import {
  getCustomerLogInstallationId,
  reportCustomerLogs,
  scrubDiagnosticLog,
} from '../utils/customerLogReport'
import { useAppStore } from '../store/useAppStore'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))

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
  beforeEach(() => {
    window.localStorage.clear()
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__
    vi.mocked(invoke).mockReset()
    vi.stubEnv('DEV', false)
  })

  it('scrubs common credentials and personal identifiers', () => {
    const source = [
      'path=/Users/alice/Library/Application Support/MemoryBread',
      String.raw`path=C:\Users\bob\AppData\Local\MemoryBread`,
      'Authorization: Bearer eyJhbGciOiJIUzI1Ni.test.signature',
      'api_key=sk-secretvalue123456789',
      'email=alice@example.com phone=13800138000',
      'password=hunter2',
      'url=https://example.com/private?token=secret',
      'local=http://127.0.0.1:7071/status?token=local-secret',
      'temp=/private/tmp/alice/trace.log hostname=alice-mac',
    ].join('\n')

    const result = scrubDiagnosticLog(source)

    expect(result).not.toContain('alice')
    expect(result).not.toContain('bob')
    expect(result).not.toContain('example.com')
    expect(result).not.toContain('13800138000')
    expect(result).not.toContain('hunter2')
    expect(result).not.toContain('eyJhbGci')
    expect(result).not.toContain('example.com')
    expect(result).not.toContain('alice-mac')
    expect(result).not.toContain('local-secret')
    expect(result).toContain('http://127.0.0.1:7071/status?token=[REDACTED]')
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

  it.each([false, true])('uploads and completes the report, browser development=%s', async (development) => {
    vi.stubEnv('DEV', development)
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
      description: '无法启动 token=private-token-value',
    })

    expect(receipt.log_id).toBe('018f0000-0000-7000-8000-000000000001')
    expect(fetchMock).toHaveBeenCalledTimes(5)
    expect(fetchMock.mock.calls[0][0]).toBe('http://127.0.0.1:7070/api/debug/log-files')
    expect(fetchMock.mock.calls[2][0]).toBe('https://memorybread.cn/v1/customer-logs/upload-url')
    if (development) {
      expect(fetchMock.mock.calls[3][0]).toBe('/__memorybread/diagnostics/upload')
      expect(fetchMock.mock.calls[3][1].method).toBe('POST')
      expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toMatchObject({
        uploadUrl: 'https://example-bucket.oss.example.com/report.zip',
        contentBase64: expect.any(String),
      })
    } else {
      expect(fetchMock.mock.calls[3][1]).toMatchObject({
        method: 'PUT', headers: { 'content-type': 'application/zip' },
      })
    }
    expect(fetchMock.mock.calls[4][0]).toBe('https://memorybread.cn/v1/customer-logs')
    expect(fetchMock.mock.calls[4][1]).toMatchObject({ method: 'POST' })
    expect(JSON.parse(String(fetchMock.mock.calls[4][1]?.body))).toMatchObject({
      upload_id: '018f0000-0000-7000-8000-000000000001',
      oss_object_key: 'customer-logs/production/2026/08/17/report.zip',
      platform: 'macos',
      architecture: 'aarch64',
      description: '无法启动 token=[REDACTED]',
      initialization_report_id: null,
    })
  })

  it('falls back to sidecar logs and links an initialization report when core is down', async () => {
    useAppStore.setState({ serviceEnvironment: 'production' })
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('Load failed'))
      .mockResolvedValueOnce(jsonResponse({
        items: [{ key: 'core', label: '核心服务日志', exists: true, size_bytes: 42, modified_at: 1 }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        key: 'core',
        label: '核心服务日志',
        content: 'database migration failed at /Users/alice/private.db',
        truncated: false,
        total_size_bytes: 56,
        returned_bytes: 56,
        modified_at: 1,
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: {
          upload_id: '018f0000-0000-7000-8000-000000000002',
          oss_object_key: 'customer-logs/production/2026/09/01/report.zip',
          upload_url: 'https://example-bucket.oss.example.com/report.zip',
          required_headers: { 'content-type': 'application/zip' },
        },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse({
        data: {
          log_id: '018f0000-0000-7000-8000-000000000002',
          received_at: '2026-09-01T05:00:00Z',
          duplicate: false,
        },
      }))
    vi.stubGlobal('fetch', fetchMock)

    await reportCustomerLogs({
      adminApiBaseUrl: 'https://memorybread.cn',
      localApiBaseUrl: 'http://127.0.0.1:7070',
      metadata,
      installationId: '018f0000-0000-7000-8000-000000000010',
      initializationReportId: '018f0000-0000-7000-8000-000000000011',
    })

    expect(fetchMock.mock.calls[1][0]).toBe('http://127.0.0.1:7071/api/debug/log-files')
    const prepareBody = JSON.parse(String(fetchMock.mock.calls[3][1]?.body))
    expect(prepareBody).toMatchObject({
      installation_id: '018f0000-0000-7000-8000-000000000010',
      initialization_report_id: '018f0000-0000-7000-8000-000000000011',
    })
  })

  it.each([false, true])('uses native upload and resumes pending initialization logs from settings: %s', async (hasPending) => {
    vi.stubEnv('DEV', true)
    const pendingKey = 'memorybread.pending-initialization-log:production:https://memorybread.cn'
    if (hasPending) localStorage.setItem(pendingKey, JSON.stringify({ reportId: 'pending-report', installationId: 'pending-installation' }))
    useAppStore.setState({ serviceEnvironment: 'production' })
    ;(window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {}
    vi.mocked(invoke).mockResolvedValue(undefined)
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        items: [{ key: 'core', label: '核心服务日志', exists: true, size_bytes: 16, modified_at: 1 }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        key: 'core', label: '核心服务日志', content: 'startup failed', truncated: false,
        total_size_bytes: 14, returned_bytes: 14, modified_at: 1,
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: {
          upload_id: '018f0000-0000-7000-8000-000000000012',
          oss_object_key: 'customer-logs/production/2026/09/05/report.zip',
          upload_url: 'https://memory-bread.oss-cn-beijing.aliyuncs.com/report.zip',
          required_headers: { 'content-type': 'application/zip' },
        },
      }))
      .mockResolvedValueOnce(jsonResponse({
        data: {
          log_id: '018f0000-0000-7000-8000-000000000012',
          received_at: '2026-09-05T05:00:00Z',
          duplicate: false,
        },
      }))
    vi.stubGlobal('fetch', fetchMock)

    await reportCustomerLogs({
      adminApiBaseUrl: 'https://memorybread.cn',
      localApiBaseUrl: 'http://127.0.0.1:7070',
      metadata,
    })

    expect(invoke).toHaveBeenCalledWith('upload_customer_log_archive', expect.objectContaining({
      uploadUrl: 'https://memory-bread.oss-cn-beijing.aliyuncs.com/report.zip',
      requiredHeaders: { 'content-type': 'application/zip' },
      contentBase64: expect.any(String),
    }))
    expect(fetchMock).toHaveBeenCalledTimes(4)
    if (hasPending) {
      const body = JSON.parse(fetchMock.mock.calls[3][1].body)
      expect(body.initialization_report_id).toBe('pending-report')
      expect(body.installation_id).toBe('pending-installation')
      expect(localStorage.getItem(pendingKey)).toBeNull()
    }
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('aliyuncs.com'))).toBe(false)
  })
})
