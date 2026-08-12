export const FLOATING_ASSIST_ENABLED_KEY = 'memoryBread.floatingAssist.enabled'
export const FLOATING_ASSIST_AUTO_TASK_KEY = 'memoryBread.floatingAssist.autoTaskDetection'
export const FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY = 'memoryBread.floatingAssist.autoTaskConfig'

export interface FloatingAssistAutoTaskAppTarget {
  bundleId: string
  appName: string
}

export interface FloatingAssistAutoTaskConfig {
  enabled: boolean
  appTargets: FloatingAssistAutoTaskAppTarget[]
  triggerWords: string[]
}

export interface FloatingAssistTaskDetection {
  matched: boolean
  score: number
  reasons: string[]
  fingerprint: string
  snippets: string[]
  requiresConfirmation: boolean
}

export interface FloatingAssistTaskDetectionOptions {
  requireImWindow?: boolean
  screenshotSource?: string | null
  appBundleId?: string | null
  appName?: string | null
  windowTitle?: string | null
  appTargets?: FloatingAssistAutoTaskAppTarget[]
  appMatchers?: string[]
  triggerWords?: string[]
  keywords?: string[]
}

const MAX_FINGERPRINT_TEXT_LENGTH = 1600
export const DEFAULT_AUTO_TASK_APP_TARGETS: FloatingAssistAutoTaskAppTarget[] = [
  { bundleId: 'com.tencent.xinWeChat', appName: '微信' },
  { bundleId: 'com.tencent.WeWorkMac', appName: '企业微信' },
  { bundleId: 'com.bytedance.lark', appName: '飞书' },
  { bundleId: 'com.tinyspeck.slackmacgap', appName: 'Slack' },
  { bundleId: 'com.microsoft.teams2', appName: 'Microsoft Teams' },
  { bundleId: 'com.alibaba.DingTalkMac', appName: '钉钉' },
  { bundleId: 'com.tencent.qq', appName: 'QQ' },
  { bundleId: 'ru.keepcoder.Telegram', appName: 'Telegram' },
  { bundleId: 'com.hnc.Discord', appName: 'Discord' },
]

export const DEFAULT_AUTO_TASK_TRIGGER_WORDS = [
  '帮我',
  '请帮',
  '请你',
  '需要你',
  '麻烦你',
  '麻烦帮',
]

const directInstructionPattern = /(?:^|[:：]\s*)(?:帮我|帮忙|麻烦你|麻烦帮|需要你|请帮|请你|please|can you|could you)[\s，,]*(?![.。！？!?,，、\s]*$)\S/i
const directActionStartPattern = /^(?:写|撰写|生成|总结|整理|回复)(?:一份|一个|个|下|一下|周报|日报|月报|方案|邮件|总结|文档|说明|计划)\S*/i
const checkboxPattern = /^\s*(?:[-*]\s*)?(?:\[[ x]?\]|□|☐|☑|✅)\s*\S/i
const todoWithContentPattern = /^\s*(?:TODO|To-?do|待办|行动项|Action items?|Next steps?)\s*(?:[:：-]\s*)\S/i
const todoHeaderPattern = /^\s*(?:TODO|To-?do|待办|行动项|Action items?|Next steps?)\s*$/i
const bulletActionPattern = /^\s*(?:[-*•]\s*|\d+[.)、]\s*)?(?:完成|处理|跟进|推进|安排|指派|交付|提交|回复|确认|整理|总结|生成|撰写|实现|修复|优化|排查|定位|调试|测试|fix|implement|check|write|summarize|reply)\S/i
const issueWithActionPattern = /\b(?:jira|linear|issue|ticket|bug|BUG|Bug|P[0-3])\b.*(?:fix|implement|check|修复|实现|处理|排查|定位|跟进|确认)/i
const deadlinePattern = /(?:截止|到期|deadline|due date|ETA)\s*[:：]?\s*\S|(?:今天|明天|本周|下周|月底|周[一二三四五六日天])(?:前|内)/i
const taskObjectActionPattern = /(?:任务|需求|问题|故障|异常|缺陷|bug|Bug|BUG).*(?:完成|处理|跟进|推进|安排|提交|回复|确认|整理|实现|修复|优化|排查|定位|调试|测试)/i
const documentSurfacePattern = /(?:https?:\/\/|www\.)\S*(?:\/docs?(?:\/|\b)|\/wiki(?:\/|\b)|\/[dk]\/home\/)|云文档|所有改动已自动保存|正文，默认字体|可编辑|书签|新标签页/i
const imSurfacePattern = /(?:微信|WeChat|企业微信|飞书|Lark|Slack|Teams|Microsoft Teams|钉钉|DingTalk|QQ|Telegram|Discord|WhatsApp|消息|聊天|群聊|私信|DM|channel|频道)/i

const cleanStringList = (values: unknown, fallback: string[]) => {
  if (!Array.isArray(values)) return fallback
  const seen = new Set<string>()
  const cleaned: string[] = []
  values.forEach(value => {
    const text = String(value ?? '').trim()
    const key = text.toLocaleLowerCase()
    if (!text || seen.has(key)) return
    seen.add(key)
    cleaned.push(text)
  })
  return cleaned.length > 0 ? cleaned : fallback
}

const cleanAppTargets = (values: unknown, fallback: FloatingAssistAutoTaskAppTarget[]) => {
  if (!Array.isArray(values)) return fallback
  const seen = new Set<string>()
  const cleaned: FloatingAssistAutoTaskAppTarget[] = []
  values.forEach(value => {
    if (typeof value === 'string') {
      const text = value.trim()
      const key = `name:${text.toLocaleLowerCase()}`
      if (!text || seen.has(key)) return
      seen.add(key)
      cleaned.push({ bundleId: '', appName: text })
      return
    }
    if (!value || typeof value !== 'object') return
    const record = value as { bundleId?: unknown; bundle_id?: unknown; appName?: unknown; app_name?: unknown }
    const bundleId = String(record.bundleId ?? record.bundle_id ?? '').trim()
    const appName = String(record.appName ?? record.app_name ?? '').trim()
    if (!bundleId && !appName) return
    const key = `${bundleId.toLocaleLowerCase()}|${appName.toLocaleLowerCase()}`
    if (seen.has(key)) return
    seen.add(key)
    cleaned.push({ bundleId, appName })
  })
  return cleaned.length > 0 ? cleaned : fallback
}

export const normalizeFloatingAssistAutoTaskConfig = (
  value: (Partial<FloatingAssistAutoTaskConfig> & { appMatchers?: unknown; keywords?: unknown }) | null | undefined,
): FloatingAssistAutoTaskConfig => {
  const legacyAppMatchers = value?.appMatchers
  const legacyKeywords = value?.keywords
  return {
    enabled: Boolean(value?.enabled),
    appTargets: cleanAppTargets(value?.appTargets ?? legacyAppMatchers, DEFAULT_AUTO_TASK_APP_TARGETS),
    triggerWords: cleanStringList(value?.triggerWords ?? legacyKeywords, DEFAULT_AUTO_TASK_TRIGGER_WORDS),
  }
}

export const readFloatingAssistAutoTaskConfig = (): FloatingAssistAutoTaskConfig => {
  try {
    const raw = localStorage.getItem(FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY)
    if (raw) {
      return normalizeFloatingAssistAutoTaskConfig(JSON.parse(raw))
    }
    return normalizeFloatingAssistAutoTaskConfig({
      enabled: localStorage.getItem(FLOATING_ASSIST_AUTO_TASK_KEY) === 'true',
    })
  } catch {
    return normalizeFloatingAssistAutoTaskConfig(null)
  }
}

export const writeFloatingAssistAutoTaskConfig = (config: FloatingAssistAutoTaskConfig) => {
  const normalized = normalizeFloatingAssistAutoTaskConfig(config)
  localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY, JSON.stringify(normalized))
  localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_KEY, String(normalized.enabled))
  return normalized
}

const hashText = (text: string) => {
  let hash = 2166136261
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(16)
}

const addReason = (reasons: string[], reason: string) => {
  if (!reasons.includes(reason)) reasons.push(reason)
}

const normalizeLine = (line: string) =>
  line
    .replace(/^显示器\s*\d+\s*[:：]\s*/g, '')
    .replace(/\s+/g, ' ')
    .trim()

const getUniqueLines = (ocrText: string) => {
  const seen = new Set<string>()
  const lines: string[] = []
  ocrText.split('\n').forEach(rawLine => {
    const line = normalizeLine(rawLine)
    if (!line || line.length < 2) return
    const key = line.toLocaleLowerCase()
    if (seen.has(key)) return
    seen.add(key)
    lines.push(line)
  })
  return lines
}

const hasDocumentReadingSurface = (normalizedText: string) =>
  documentSurfacePattern.test(normalizedText)

const hasImSurface = (normalizedText: string, options: FloatingAssistTaskDetectionOptions) => {
  const metadataText = [options.appBundleId, options.appName, options.windowTitle].filter(Boolean).join(' ')
  const lowerBundleId = (options.appBundleId || '').toLocaleLowerCase()
  const lowerAppName = (options.appName || '').toLocaleLowerCase()
  const lowerMetadata = metadataText.toLocaleLowerCase()
  const configuredTargets = cleanAppTargets(options.appTargets ?? options.appMatchers, [])
  const configuredPatternHit = configuredTargets.some(target =>
    (target.bundleId && lowerBundleId.includes(target.bundleId.toLocaleLowerCase()))
    || (target.appName && (
      lowerAppName.includes(target.appName.toLocaleLowerCase())
      || lowerMetadata.includes(target.appName.toLocaleLowerCase())
    )),
  )
  return configuredPatternHit || imSurfacePattern.test(metadataText) || (!options.requireImWindow && imSurfacePattern.test(normalizedText))
}

const buildFingerprint = (snippets: string[], fallbackText: string) => {
  const source = snippets.length > 0 ? snippets.join('\n') : fallbackText.slice(0, MAX_FINGERPRINT_TEXT_LENGTH)
  return hashText(source.toLocaleLowerCase())
}

const hasConfiguredKeyword = (line: string, keywords?: string[]) => {
  const cleaned = cleanStringList(keywords, [])
  if (cleaned.length === 0) return false
  const lowerLine = line.toLocaleLowerCase()
  return cleaned.some(keyword => lowerLine.includes(keyword.toLocaleLowerCase()))
}

export const detectFloatingAssistTaskFromOcr = (
  ocrText: string,
  options: FloatingAssistTaskDetectionOptions = {},
): FloatingAssistTaskDetection => {
  const normalized = ocrText
    .replace(/显示器\s*\d+\s*[:：]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  const reasons: string[] = []
  const snippets: string[] = []
  if (options.requireImWindow) {
    if (options.screenshotSource && options.screenshotSource !== 'window') {
      return {
        matched: false,
        score: 0,
        reasons: ['非前台窗口截图'],
        fingerprint: hashText(normalized),
        snippets,
        requiresConfirmation: false,
      }
    }
    if (hasDocumentReadingSurface(normalized) || !hasImSurface(normalized, options)) {
      return {
        matched: false,
        score: 0,
        reasons: ['非IM窗口'],
        fingerprint: hashText(normalized),
        snippets,
        requiresConfirmation: false,
      }
    }
  }

  const configuredTriggerWords = options.triggerWords ?? options.keywords
  if (normalized.length < 16 && !hasConfiguredKeyword(normalized, configuredTriggerWords)) {
    return { matched: false, score: 0, reasons, fingerprint: hashText(normalized), snippets, requiresConfirmation: false }
  }

  let score = 0
  let decisiveSignals = 0
  let hasDirectInstruction = false
  let hasTriggerKeyword = false

  const addEvidence = (line: string, points: number, reason: string, decisive = false) => {
    score += points
    addReason(reasons, reason)
    if (snippets.length < 6 && !snippets.includes(line)) snippets.push(line)
    if (decisive) decisiveSignals += 1
  }

  getUniqueLines(ocrText).forEach(line => {
    if (hasConfiguredKeyword(line, configuredTriggerWords)) {
      hasTriggerKeyword = true
      addEvidence(line, 6, '配置触发词', true)
      return
    }
    if (directInstructionPattern.test(line) || directActionStartPattern.test(line)) {
      hasDirectInstruction = true
      addEvidence(line, 6, '直接指令', true)
      return
    }
    if (checkboxPattern.test(line)) {
      addEvidence(line, 5, '勾选待办', true)
      return
    }
    if (todoWithContentPattern.test(line)) {
      addEvidence(line, 5, '任务标记', true)
      return
    }
    if (bulletActionPattern.test(line)) {
      addEvidence(line, 4, '任务列表', true)
      return
    }
    if (issueWithActionPattern.test(line)) {
      addEvidence(line, 4, '工单动作', true)
      return
    }

    if (deadlinePattern.test(line)) {
      addEvidence(line, 2, '时间约束')
    } else if (taskObjectActionPattern.test(line)) {
      addEvidence(line, 2, '任务对象动作')
    } else if (todoHeaderPattern.test(line)) {
      addEvidence(line, 1, '任务标题')
    }
  })

  if (hasDocumentReadingSurface(normalized) && !hasDirectInstruction) {
    addReason(reasons, '文档页面需确认')
    return {
      matched: false,
      score,
      reasons,
      fingerprint: buildFingerprint(snippets, normalized),
      snippets,
      requiresConfirmation: decisiveSignals > 0 && score >= 4,
    }
  }

  const matched = decisiveSignals > 0 && score >= 6
  if (options.requireImWindow && matched && !hasDirectInstruction && !hasTriggerKeyword) {
    addReason(reasons, '任务列表需确认')
    return {
      matched: false,
      score,
      reasons,
      fingerprint: buildFingerprint(snippets, normalized),
      snippets,
      requiresConfirmation: true,
    }
  }

  const requiresConfirmation = !matched && decisiveSignals > 0 && score >= 4

  return {
    matched,
    score,
    reasons,
    fingerprint: buildFingerprint(snippets, normalized),
    snippets,
    requiresConfirmation,
  }
}
