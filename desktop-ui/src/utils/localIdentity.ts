export const LOCAL_NICKNAME_KEY = 'memory-bread_local_nickname_v1'

const FALLBACK_NICKNAMES = [
  '倔强的牛角面包',
  '慢烤的酸种面包',
  '好奇的小法棍',
  '踏实的吐司片',
  '发光的碱水结',
  '温柔的奶香餐包',
]

const readStoredNickname = (): string | null => {
  try {
    const nickname = window.localStorage.getItem(LOCAL_NICKNAME_KEY)?.trim()
    return nickname || null
  } catch {
    return null
  }
}

export const getLocalNickname = (): string => (
  readStoredNickname() || '新鲜出炉的面包'
)

const persistNickname = (nickname: string): string => {
  const normalized = nickname.trim().slice(0, 24)
  try {
    window.localStorage.setItem(LOCAL_NICKNAME_KEY, normalized)
  } catch {
    // 受限 WebView 中仍可在当前会话展示，不阻塞本地功能。
  }
  return normalized
}

const fallbackNickname = (): string => {
  const cryptoApi = globalThis.crypto
  const randomValue = cryptoApi?.getRandomValues
    ? cryptoApi.getRandomValues(new Uint32Array(1))[0]
    : Date.now()
  return FALLBACK_NICKNAMES[randomValue % FALLBACK_NICKNAMES.length]
}

export const ensureLocalNickname = async (
  sidecarBaseUrl = 'http://127.0.0.1:7071',
): Promise<string> => {
  const existing = readStoredNickname()
  if (existing) return existing

  try {
    const response = await fetch(`${sidecarBaseUrl.replace(/\/$/, '')}/api/local-identity/nickname`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    const payload = await response.json().catch(() => ({})) as { nickname?: unknown }
    if (response.ok && typeof payload.nickname === 'string' && payload.nickname.trim()) {
      return persistNickname(payload.nickname)
    }
  } catch {
    // 初始化刚完成时服务可能短暂重启，用本地面包名保证入口不回退为“登录账户”。
  }
  return persistNickname(fallbackNickname())
}
