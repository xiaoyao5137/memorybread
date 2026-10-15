import qualityProfiles from '../../../shared/creation-work-quality/profiles.json'

export interface CreationWorkContext {
  schema_version: 'creation.work-context.v1'
  purpose: string
  decision_goal: string
  reader_context: string
  constraints: string
  style_samples: string[]
  profile_id: string
  review_enabled: boolean
}

export type WorkPreviewAction = 'trial' | 'review' | 'summary'
export interface WorkPreviewResult {
  status: 'candidate' | 'pass' | 'revise' | 'unverified'
  content?: string
  review?: Record<string, unknown>
  document_hash?: string
  [key: string]: unknown
}

export const workQualityProfiles: Array<{ id: string; label: string; description: string }> = qualityProfiles.profiles
export const emptyWorkContext = (): CreationWorkContext => ({
  schema_version: 'creation.work-context.v1', purpose: '', decision_goal: '', reader_context: '',
  constraints: '', style_samples: [], profile_id: 'general', review_enabled: false,
})

/** Old drafts and history can omit this optional contract. Never truncate saved input. */
export const readWorkContext = (value: unknown): CreationWorkContext | null => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  if (source.schema_version != null && source.schema_version !== 'creation.work-context.v1') return null
  const result = emptyWorkContext()
  for (const key of ['purpose', 'decision_goal', 'reader_context', 'constraints', 'profile_id'] as const) {
    if (source[key] != null && (typeof source[key] !== 'string' || String(source[key]).length > 1500)) return null
    if (typeof source[key] === 'string') result[key] = source[key] as string
  }
  if (source.style_samples != null) {
    if (!Array.isArray(source.style_samples) || source.style_samples.length > 3
      || source.style_samples.some(sample => typeof sample !== 'string' || sample.length > 1500)) return null
    result.style_samples = source.style_samples as string[]
  }
  result.review_enabled = source.review_enabled === true
  return result
}

export const hasWorkContext = (context?: CreationWorkContext | null) => Boolean(context && (
  context.purpose.trim() || context.decision_goal.trim() || context.reader_context.trim()
  || context.constraints.trim() || context.style_samples.some(sample => sample.trim())
  || context.profile_id !== 'general' || context.review_enabled
))

export const workContextForRequest = (context?: CreationWorkContext | null): CreationWorkContext | undefined => context == null
  ? undefined : { ...context, style_samples: context.style_samples.filter(sample => sample.trim()) }

/** Embed just this optional field; all existing confirmed choices stay byte-for-byte intact. */
export const creationBriefWithWorkContext = <T extends object>(brief: T | null, context?: CreationWorkContext | null) => {
  if (context == null) return brief
  return { ...(brief || {}), work_context: workContextForRequest(context) }
}

export const workPreviewText = (result: WorkPreviewResult): string => {
  const content = typeof result.content === 'string' ? result.content.trim() : ''
  const report = workPreviewReviewText(result)
  return content ? `${content}${report ? `\n\n审阅说明\n${report}` : ''}`
    : report || '本次预览未取得可核验的审阅结果，正文已保留。'
}

export const workPreviewReviewText = (result: WorkPreviewResult): string => {
  const review = result.review || {}
  const checks = Array.isArray(review.checks) ? review.checks : []
  const issues = Array.isArray(review.issues) ? review.issues : []
  if (checks.length) return checks.map((item: unknown) => {
    const check = item as Record<string, unknown>
    const dimension = qualityProfiles.criteria.find(criterion => criterion.issue_code === check.id)
    const issue = issues.find(item => item && typeof item === 'object' && (item as Record<string, unknown>).code === check.id) as Record<string, unknown> | undefined
    return `${dimension?.label || check.label || check.criterion || '审阅项'}：${check.passed === true ? '通过' : '需调整'}\n${check.reason || ''}`
      + (issue?.quote ? `\n原文：${issue.quote}` : '') + (issue?.correction ? `\n建议：${issue.correction}` : '')
  }).join('\n\n')
  if (issues.length) return issues.map(item => {
    const issue = item as Record<string, unknown>
    return [issue.reason, issue.correction].filter(Boolean).join('\n')
  }).join('\n\n')
  return ''
}
