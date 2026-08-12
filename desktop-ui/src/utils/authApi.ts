import type {
  AuthSession,
  CloudBalance,
  CloudDevice,
  CloudMessage,
  CloudMessagePage,
  CloudSnapshot,
  CloudSubscription,
  CloudUser,
  CompleteCloudSnapshotRequest,
  ServiceEnvironment,
  UpsertCloudDeviceRequest,
} from '../types'
import { serviceEnvironmentHeaders } from '../store/useAppStore'

function normalizeAuthFetchError(error: unknown, adminApiBaseUrl: string): Error {
  if (error instanceof TypeError) {
    return new Error(`账户服务暂时无法连接，请稍后重试或检查账户连接地址：${adminApiBaseUrl}`)
  }
  if (error instanceof Error) return error
  return new Error('登录失败，请检查网络或账户信息')
}

function authErrorMessage(
  payload: { error?: { code?: string; message?: string } } | null,
  fallback: string,
): string {
  if (payload?.error?.code === 'DATABASE_NOT_CONFIGURED') {
    return '账户服务暂时未就绪，请稍后重试。'
  }
  return payload?.error?.message || fallback
}

interface CloudApiError extends Error {
  status?: number
  code?: string
  retryAfterSeconds?: number
}

const cloudApiError = (
  payload: { error?: { code?: string; message?: string } } | null,
  fallback: string,
  status: number,
): CloudApiError => {
  const error = new Error(authErrorMessage(payload, fallback)) as CloudApiError
  error.status = status
  error.code = payload?.error?.code
  const retryAfter = payload?.error && 'retry_after_seconds' in payload.error
    ? Number((payload.error as { retry_after_seconds?: number }).retry_after_seconds)
    : undefined
  if (retryAfter && Number.isFinite(retryAfter)) error.retryAfterSeconds = retryAfter
  return error
}

export const cloudSessionIsInvalid = (error: unknown): boolean => {
  const status = (error as CloudApiError | null)?.status
  return status === 401 || status === 403
}

export const cloudApiErrorCode = (error: unknown): string | undefined =>
  (error as CloudApiError | null)?.code

export async function authenticateWithPassword(
  adminApiBaseUrl: string,
  mode: 'login' | 'register',
  email: string,
  password: string,
  username?: string,
  nickname?: string,
  companyName?: string,
  emailChallengeId?: string,
  emailCode?: string,
): Promise<AuthSession> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/${mode}`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email,
      password,
      username: mode === 'register' ? username?.trim() || undefined : undefined,
      nickname: mode === 'register' ? nickname?.trim() || undefined : undefined,
      company_name: mode === 'register' ? companyName?.trim() || undefined : undefined,
      email_verification: mode === 'register' && emailChallengeId && emailCode
        ? { challenge_id: emailChallengeId, code: emailCode.trim() }
        : undefined,
    }),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `auth failed: ${response.status}`, response.status)
  }
  return payload.data as AuthSession
}

export async function sendEmailVerificationCode(
  adminApiBaseUrl: string,
  email: string,
): Promise<{ challenge_id: string; retry_after_seconds: number; expires_in_seconds: number }> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/email/send-code`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `send email code failed: ${response.status}`, response.status)
  }
  return payload.data
}

export type PasswordResetChannel = 'email' | 'phone'

export async function sendPasswordResetCode(
  adminApiBaseUrl: string,
  channel: PasswordResetChannel,
  identifier: string,
): Promise<{ challenge_id: string; retry_after_seconds: number; expires_in_seconds: number }> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/password-reset/send-code`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel, identifier }),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `send password reset code failed: ${response.status}`, response.status)
  }
  return payload.data
}

export async function confirmPasswordReset(
  adminApiBaseUrl: string,
  request: {
    challenge_id: string
    channel: PasswordResetChannel
    identifier: string
    code: string
    new_password: string
  },
): Promise<{ ok: boolean }> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/password-reset/confirm`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `password reset failed: ${response.status}`, response.status)
  }
  return payload.data
}

export async function sendPhoneVerificationCode(
  adminApiBaseUrl: string,
  phone: string,
): Promise<{ retry_after_seconds: number; expires_in_seconds: number }> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/phone/send-code`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone }),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `send phone code failed: ${response.status}`, response.status)
  }
  return payload.data
}

export async function authenticateWithPhoneCode(
  adminApiBaseUrl: string,
  phone: string,
  code: string,
  username?: string,
  nickname?: string,
  companyName?: string,
): Promise<AuthSession> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/phone/verify`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      phone,
      code,
      username: username?.trim() || undefined,
      nickname: nickname?.trim() || undefined,
      company_name: companyName?.trim() || undefined,
    }),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `phone auth failed: ${response.status}`, response.status)
  }
  return payload.data as AuthSession
}

export async function updateUserProfile(
  adminApiBaseUrl: string,
  token: string,
  nickname: string,
  companyName?: string,
): Promise<CloudUser> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/profile`, {
    method: 'PUT',
    headers: {
      ...serviceEnvironmentHeaders(),
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      nickname: nickname.trim(),
      company_name: companyName?.trim() || undefined,
    }),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 404 || response.status === 405) {
      throw new Error('账户服务版本较旧，请更新或重启账户服务后重试。')
    }
    throw new Error(authErrorMessage(payload, `profile update failed: ${response.status}`))
  }
  return payload.data as CloudUser
}

export async function fetchCurrentUser(
  adminApiBaseUrl: string,
  token: string,
  signal?: AbortSignal,
): Promise<CloudUser> {
  const response = await fetch(`${adminApiBaseUrl}/v1/auth/me`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
    ...(signal ? { signal } : {}),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `auth session invalid: ${response.status}`, response.status)
  }
  return payload.data as CloudUser
}

export async function fetchBillingBalance(
  adminApiBaseUrl: string,
  token: string,
  signal?: AbortSignal,
): Promise<CloudBalance> {
  const response = await fetch(`${adminApiBaseUrl}/v1/billing/balance`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
    ...(signal ? { signal } : {}),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `balance fetch failed: ${response.status}`))
  }
  return payload.data as CloudBalance
}

export interface CloudConsoleSummary {
  balance?: CloudBalance
  current_subscription?: CloudSubscription | null
}

export async function fetchConsoleSummary(
  adminApiBaseUrl: string,
  token: string,
  signal?: AbortSignal,
): Promise<CloudConsoleSummary> {
  const response = await fetch(`${adminApiBaseUrl}/v1/console/summary`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
    ...(signal ? { signal } : {}),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `console summary fetch failed: ${response.status}`))
  }
  return payload.data as CloudConsoleSummary
}

export async function logoutSession(adminApiBaseUrl: string, token: string): Promise<void> {
  await fetch(`${adminApiBaseUrl}/v1/auth/logout`, {
    method: 'POST',
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
  }).catch(() => undefined)
}

export async function upsertCloudDevice(
  adminApiBaseUrl: string,
  token: string,
  device: UpsertCloudDeviceRequest,
  signal?: AbortSignal,
  environment?: ServiceEnvironment,
): Promise<CloudDevice> {
  const response = await fetch(`${adminApiBaseUrl}/v1/devices`, {
    method: 'POST',
    headers: {
      ...serviceEnvironmentHeaders(environment),
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(device),
    ...(signal ? { signal } : {}),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw cloudApiError(payload, `device sync failed: ${response.status}`, response.status)
  }
  return payload.data as CloudDevice
}

export async function fetchCloudDevices(
  adminApiBaseUrl: string,
  token: string,
): Promise<CloudDevice[]> {
  const response = await fetch(`${adminApiBaseUrl}/v1/devices`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `devices fetch failed: ${response.status}`))
  }
  return payload.data as CloudDevice[]
}

export async function completeCloudSnapshotUpload(
  adminApiBaseUrl: string,
  token: string,
  snapshot: CompleteCloudSnapshotRequest,
): Promise<CloudSnapshot> {
  const response = await fetch(`${adminApiBaseUrl}/v1/snapshots`, {
    method: 'POST',
    headers: {
      ...serviceEnvironmentHeaders(),
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(snapshot),
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `snapshot sync failed: ${response.status}`))
  }
  return payload.data as CloudSnapshot
}

export async function fetchCloudSnapshots(
  adminApiBaseUrl: string,
  token: string,
): Promise<CloudSnapshot[]> {
  const response = await fetch(`${adminApiBaseUrl}/v1/snapshots`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `snapshots fetch failed: ${response.status}`))
  }
  return payload.data as CloudSnapshot[]
}

export async function fetchCloudMessages(
  adminApiBaseUrl: string,
  token: string,
  options: { page?: number; pageSize?: number; unreadOnly?: boolean } = {},
): Promise<CloudMessagePage> {
  const search = new URLSearchParams({
    page: String(options.page || 1),
    page_size: String(options.pageSize || 50),
  })
  if (options.unreadOnly) search.set('unread_only', 'true')
  const response = await fetch(`${adminApiBaseUrl}/v1/messages?${search}`, {
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `messages fetch failed: ${response.status}`))
  }
  const data = (payload?.data || {}) as Partial<CloudMessagePage>
  return {
    items: Array.isArray(data.items) ? data.items : [],
    page: Number(data.page || 1),
    page_size: Number(data.page_size || options.pageSize || 50),
    total: Number(data.total || 0),
    unread_count: Number(data.unread_count || 0),
  }
}

export async function markCloudMessageRead(
  adminApiBaseUrl: string,
  token: string,
  messageId: string,
): Promise<CloudMessage> {
  const response = await fetch(`${adminApiBaseUrl}/v1/messages/${encodeURIComponent(messageId)}/read`, {
    method: 'PUT',
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `message read failed: ${response.status}`))
  }
  return payload.data as CloudMessage
}

export async function markAllCloudMessagesRead(
  adminApiBaseUrl: string,
  token: string,
): Promise<{ updated_count: number; read_at: string }> {
  const response = await fetch(`${adminApiBaseUrl}/v1/messages/read-all`, {
    method: 'PUT',
    headers: { ...serviceEnvironmentHeaders(), Authorization: `Bearer ${token}` },
  }).catch((error) => {
    throw normalizeAuthFetchError(error, adminApiBaseUrl)
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(authErrorMessage(payload, `messages read failed: ${response.status}`))
  }
  return payload.data
}
