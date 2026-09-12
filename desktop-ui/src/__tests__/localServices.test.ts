import { beforeEach, describe, expect, it } from 'vitest'

import {
  applyLocalServiceRegistry,
  getLocalServiceBaseUrl,
  isLocalServiceUrl,
  resetLocalServicesForTests,
  rewriteLocalServiceUrl,
} from '../utils/localServices'

describe('local service routing', () => {
  beforeEach(() => resetLocalServicesForTests())

  it('keeps development defaults before a packaged registry is loaded', () => {
    expect(getLocalServiceBaseUrl('core')).toBe('http://127.0.0.1:7070')
  })

  it('normalizes both localhost spellings through the logical service map', () => {
    expect(rewriteLocalServiceUrl('http://localhost:7071/api/models?ready=1')).toBe(
      'http://127.0.0.1:7071/api/models?ready=1',
    )
  })

  it('rewrites a legacy caller to the current logical endpoint generation', () => {
    const ports: Array<[string, number]> = [
      ['core', 41001], ['model_api', 41002], ['creation', 41003],
      ['vector_search', 41004], ['ollama', 41005],
    ]
    const services = Object.fromEntries(ports.map(([name, port]) => [name, {
      scheme: 'http', host: '127.0.0.1', port, health_path: '/health', status: 'allocated',
    }]))
    applyLocalServiceRegistry({
      schema_version: 'local-services.v1', instance_id: 'test', generation: 2,
      services, auth_token: 'a'.repeat(64),
    } as never)

    expect(rewriteLocalServiceUrl('http://127.0.0.1:8001/creation/references')).toBe(
      'http://127.0.0.1:41003/creation/references',
    )

    const nextServices = Object.fromEntries(Object.entries(services).map(([name, endpoint]) => [
      name, { ...endpoint, port: endpoint.port + 100 },
    ]))
    applyLocalServiceRegistry({
      schema_version: 'local-services.v1', instance_id: 'test-next', generation: 3,
      services: nextServices, auth_token: 'b'.repeat(64),
    } as never)
    expect(rewriteLocalServiceUrl('http://127.0.0.1:41003/creation/references')).toBe(
      'http://127.0.0.1:41103/creation/references',
    )
  })

  it('does not rewrite unrelated remote origins', () => {
    const remote = 'https://example.com:7071/api/models'
    expect(rewriteLocalServiceUrl(remote)).toBe(remote)
    expect(isLocalServiceUrl(remote)).toBe(false)
  })
})
