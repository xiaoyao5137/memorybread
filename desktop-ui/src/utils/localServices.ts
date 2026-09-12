export type LocalServiceName =
  | 'core'
  | 'model_api'
  | 'creation'
  | 'vector_search'
  | 'ollama'

interface LocalServiceEndpoint {
  scheme: 'http'
  host: '127.0.0.1'
  port: number
  health_path: string
  status: string
}

export interface LocalServiceRegistryResponse {
  schema_version: 'local-services.v1'
  instance_id: string
  generation: number
  services: Record<LocalServiceName, LocalServiceEndpoint>
  auth_token: string
}

const LEGACY_PORTS: Record<LocalServiceName, number> = {
  core: 7070,
  model_api: 7071,
  creation: 8001,
  vector_search: 7072,
  ollama: 11434,
}

const LEGACY_ORIGIN_TO_SERVICE = new Map(
  Object.entries(LEGACY_PORTS).flatMap(([name, port]) => [
    [`http://127.0.0.1:${port}`, name as LocalServiceName],
    [`http://localhost:${port}`, name as LocalServiceName],
  ]),
)
const knownOriginToService = new Map(LEGACY_ORIGIN_TO_SERVICE)

let registry: LocalServiceRegistryResponse | null = null
let routingInstalled = false

const isTauriRuntime = () => '__TAURI_INTERNALS__' in globalThis

const endpointBaseUrl = (endpoint: LocalServiceEndpoint) =>
  `${endpoint.scheme}://${endpoint.host}:${endpoint.port}`

export const getLocalServiceBaseUrl = (name: LocalServiceName) => {
  const endpoint = registry?.services?.[name]
  return endpoint ? endpointBaseUrl(endpoint) : `http://127.0.0.1:${LEGACY_PORTS[name]}`
}

export const getLocalServiceAuthToken = () => registry?.auth_token ?? ''

export const applyLocalServiceRegistry = (next: LocalServiceRegistryResponse) => {
  if (next.schema_version !== 'local-services.v1') {
    throw new Error('本机服务注册表版本不受支持')
  }
  for (const name of Object.keys(LEGACY_PORTS) as LocalServiceName[]) {
    const endpoint = next.services?.[name]
    if (!endpoint || endpoint.scheme !== 'http' || endpoint.host !== '127.0.0.1'
      || !Number.isInteger(endpoint.port) || endpoint.port < 1 || endpoint.port > 65535) {
      throw new Error(`本机服务 ${name} 端点不安全`)
    }
    knownOriginToService.set(endpointBaseUrl(endpoint), name)
  }
  registry = next
}

export const rewriteLocalServiceUrl = (value: string) => {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return value
  }
  const serviceName = knownOriginToService.get(url.origin)
  if (!serviceName) return value
  return `${getLocalServiceBaseUrl(serviceName)}${url.pathname}${url.search}${url.hash}`
}

export const isLocalServiceUrl = (value: string) => {
  try {
    const origin = new URL(value).origin
    return (Object.keys(LEGACY_PORTS) as LocalServiceName[]).some(
      name => origin === getLocalServiceBaseUrl(name),
    )
  } catch {
    return false
  }
}

const rewriteFetchInput = (input: RequestInfo | URL): RequestInfo | URL => {
  if (typeof input === 'string') return rewriteLocalServiceUrl(input)
  if (input instanceof URL) return new URL(rewriteLocalServiceUrl(input.toString()))
  const rewritten = rewriteLocalServiceUrl(input.url)
  return rewritten === input.url ? input : new Request(rewritten, input)
}

export const installLocalServiceFetchRouting = () => {
  if (routingInstalled || typeof globalThis.fetch !== 'function') return
  routingInstalled = true
  const originalFetch = globalThis.fetch.bind(globalThis)
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const rewritten = rewriteFetchInput(input)
    const headers = new Headers(init?.headers ?? (rewritten instanceof Request ? rewritten.headers : undefined))
    const token = getLocalServiceAuthToken()
    const targetUrl = typeof rewritten === 'string'
      ? rewritten
      : rewritten instanceof URL ? rewritten.toString() : rewritten.url
    if (token && isLocalServiceUrl(targetUrl)) {
      headers.set('X-MemoryBread-Local-Token', token)
    }
    return originalFetch(rewritten, { ...init, headers })
  }
}

const refreshRegistry = async () => {
  const { invoke } = await import('@tauri-apps/api/core')
  applyLocalServiceRegistry(
    await invoke<LocalServiceRegistryResponse>('get_local_service_endpoints'),
  )
}

export const bootstrapLocalServices = async () => {
  installLocalServiceFetchRouting()
  if (!isTauriRuntime()) return
  let lastError: unknown
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      await refreshRegistry()
      lastError = undefined
      break
    } catch (error) {
      lastError = error
      await new Promise(resolve => globalThis.setTimeout(resolve, 200))
    }
  }
  if (lastError) throw lastError
  const { listen } = await import('@tauri-apps/api/event')
  await listen('local-services-changed', async () => {
    try {
      await refreshRegistry()
    } catch (error) {
      console.error('刷新本机服务注册表失败:', error)
    }
  })
}

export const resetLocalServicesForTests = () => {
  registry = null
}
