import { beforeEach, describe, expect, it } from 'vitest'
import { getCustomerLogInstallationId, scrubDiagnosticLog } from '../utils/customerLogReport'

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
})
