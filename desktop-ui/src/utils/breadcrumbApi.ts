import type {
  BreadcrumbAwardResult,
  BreadcrumbProfile,
  BreadcrumbRule,
  BreadcrumbSurface,
} from '../types'
import { serviceEnvironmentHeaders } from '../store/useAppStore'

export const BREADCRUMBS_CHANGED_KEY = 'memorybread.breadcrumbs.changed'

export const notifyBreadcrumbsChanged = (): void => {
  try {
    localStorage.setItem(BREADCRUMBS_CHANGED_KEY, String(Date.now()))
  } catch {
    // 同窗口事件仍可刷新；跨窗口广播属于尽力通知。
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(BREADCRUMBS_CHANGED_KEY))
  }
}

const responseError = async (response: Response, fallback: string): Promise<Error> => {
  const payload = await response.json().catch(() => null)
  return new Error(payload?.error?.message || payload?.message || fallback)
}

export async function fetchRemoteBreadcrumbRules(
  adminApiBaseUrl: string,
  token: string,
  signal?: AbortSignal,
): Promise<BreadcrumbRule[]> {
  const response = await fetch(`${adminApiBaseUrl}/v1/breadcrumb-rules`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) {
    throw await responseError(response, `breadcrumb rules fetch failed: ${response.status}`)
  }
  const payload = await response.json().catch(() => null)
  return Array.isArray(payload?.data) ? payload.data as BreadcrumbRule[] : []
}

export async function syncLocalBreadcrumbRules(
  apiBaseUrl: string,
  rules: BreadcrumbRule[],
  signal?: AbortSignal,
): Promise<BreadcrumbProfile> {
  const response = await fetch(`${apiBaseUrl}/api/breadcrumbs/rules/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rules }),
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) {
    throw await responseError(response, `breadcrumb rules sync failed: ${response.status}`)
  }
  return response.json() as Promise<BreadcrumbProfile>
}

export async function fetchLocalBreadcrumbRules(
  apiBaseUrl: string,
  signal?: AbortSignal,
): Promise<BreadcrumbRule[]> {
  const response = await fetch(`${apiBaseUrl}/api/breadcrumbs/rules`, {
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) {
    throw await responseError(response, `local breadcrumb rules fetch failed: ${response.status}`)
  }
  const payload = await response.json().catch(() => null)
  return Array.isArray(payload) ? payload as BreadcrumbRule[] : []
}

export async function fetchBreadcrumbProfile(
  apiBaseUrl: string,
  signal?: AbortSignal,
): Promise<BreadcrumbProfile> {
  const response = await fetch(`${apiBaseUrl}/api/breadcrumbs`, {
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) {
    throw await responseError(response, `breadcrumbs fetch failed: ${response.status}`)
  }
  const profile = await response.json() as Partial<BreadcrumbProfile>
  return {
    breadcrumbs: Array.isArray(profile.breadcrumbs) ? profile.breadcrumbs : [],
    equipped: profile.equipped || {},
  }
}

export async function awardBreadcrumb(
  apiBaseUrl: string,
  ruleId: string,
  periodKey: string,
  observedValue: number,
  signal?: AbortSignal,
): Promise<BreadcrumbAwardResult> {
  const response = await fetch(`${apiBaseUrl}/api/breadcrumbs/awards`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      rule_id: ruleId,
      period_key: periodKey,
      observed_value: observedValue,
    }),
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) {
    throw await responseError(response, `breadcrumb award failed: ${response.status}`)
  }
  return response.json() as Promise<BreadcrumbAwardResult>
}

export async function equipBreadcrumb(
  apiBaseUrl: string,
  surface: BreadcrumbSurface,
  breadcrumbId: string | null,
): Promise<BreadcrumbProfile> {
  const response = await fetch(`${apiBaseUrl}/api/breadcrumbs/equipped`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ surface, breadcrumb_id: breadcrumbId }),
  })
  if (!response.ok) {
    throw await responseError(response, `breadcrumb equip failed: ${response.status}`)
  }
  const profile = await response.json() as BreadcrumbProfile
  notifyBreadcrumbsChanged()
  return profile
}
