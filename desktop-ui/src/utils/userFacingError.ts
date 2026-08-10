const blockedDetailPattern = /(?:provider|secret|api[ _-]?key|base[_-]?url|endpoint|ollama|sidecar|qwen|anthropic|openai|huggingface|deepseek|doubao|tongyi|kimi|kling|gemma|llama|traceback|stack|sql|database|sqlite|localhost|127\.0\.0\.1|https?:\/\/|\/Users\/|HTTP\s*\d{3}|request[_ -]?id|trace[_ -]?id|\b[A-Z][A-Z0-9_]{3,}\b|\{[^}]*\})/i

function extractMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  return ''
}

export function toUserFacingError(error: unknown, fallback: string): string {
  const message = extractMessage(error).trim()
  if (!message) return fallback

  if (/余额不足|insufficient.+(?:balance|credit)/i.test(message)) {
    return '可用 Credit 不足，请充值或切换到本地能力'
  }
  if (/unauthorized|auth_required|登录.*(?:失效|过期)/i.test(message)) {
    return '登录状态已失效，请重新登录'
  }
  if (/environment_mismatch/i.test(message)) {
    return '当前服务环境不可用，请恢复默认设置后重试'
  }
  if (/failed to fetch|networkerror|load failed|connection refused|econnrefused/i.test(message)) return fallback
  if (message.length > 160 || blockedDetailPattern.test(message)) return fallback
  return message
}

/**
 * 仅用于本机 core-engine / 创作服务等本地 API 的错误展示：
 * 本地服务返回的中文校验原因不包含供应商密钥等敏感信息，
 * 不应被 toUserFacingError 的敏感词过滤吞成笼统兜底文案，
 * 否则用户会看到“保存技能失败”这类不明原因的提示。
 */
export function toLocalApiError(error: unknown, fallback: string): string {
  const message = extractMessage(error).trim()
  if (!message) return fallback
  if (/failed to fetch|networkerror|load failed|connection refused|econnrefused/i.test(message)) {
    return `${fallback}：本地服务暂不可用，请稍后重试`
  }
  return message.length > 240 ? `${message.slice(0, 240)}…` : message
}
