// Development-only entry: initializes the real app against a fresh fixture API.
// It does not persist or change the regular desktop application's API settings.
import { useAppStore } from '../src/store/useAppStore'

if (!import.meta.env.DEV) throw new Error('Acceptance entry is development-only')
const api = new URL(new URLSearchParams(location.search).get('fixture') || '')
if (api.protocol !== 'http:' || api.hostname !== '127.0.0.1' || !api.port || api.port === '7070') {
  throw new Error('A separate loopback fixture API is required')
}
const response = await fetch(`${api.origin}/api/bake/documents/1`)
const fixture = await response.json()
if (!response.ok || fixture.title !== '摘要验收：旧摘要重建') {
  throw new Error('The isolated fixture was not found')
}
useAppStore.getState().setApiBaseUrl(api.origin)
await import('../src/main')
