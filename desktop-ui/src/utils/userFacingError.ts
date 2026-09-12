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

/**
 * 创作 Agent 的失败原因。sidecar 已把异常收敛为不含供应商信息的稳定文案，
 * 并随 run.failed 下发 CREATION_* 错误码。这类文案常出现 Token、source_id
 * 等四个以上字母的技术词，如果走 toUserFacingError 会被敏感词过滤吞成
 * “生成失败，请稍后重试”，用户因此丢掉唯一的可执行修正线索。
 * 仅对带稳定错误码的本地创作失败直接展示，其他异常仍走通用过滤。
 */
export function toCreationFailureMessage(error: unknown, fallback: string): string {
  const code = String((error as { errorCode?: unknown } | null | undefined)?.errorCode || '')
  const message = extractMessage(error).trim()
  if (!message || !/^CREATION_[A-Z0-9_]*$/.test(code)) return toUserFacingError(error, fallback)
  if (/failed to fetch|networkerror|load failed|connection refused|econnrefused/i.test(message)) {
    return fallback
  }
  return message.length > 200 ? `${message.slice(0, 200)}…` : message
}

/**
 * 只有服务端标为可重试的失败才提示“重试继续”。确定性失败（如正文未通过
 * 验收）再喊重试，只会让用户对着同一个结果原地打转。
 */
export function recoverableCreationHint(error: unknown): string {
  const retryable = (error as { retryable?: unknown } | null | undefined)?.retryable
  return retryable === true ? '，可重试继续' : ''
}
