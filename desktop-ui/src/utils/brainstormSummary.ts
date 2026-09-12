import type { CreationBrainstormExplorationStage, CreationBrainstormState } from '../store/useAppStore'

const explorationStageLabels: Record<CreationBrainstormExplorationStage, string> = {
  explore: '问题与方向',
  solutions: '探索解法',
  implementation: '细化落地',
  validation: '验证效果',
}

export function brainstormExplorationStageLabel(stage?: CreationBrainstormExplorationStage) {
  return explorationStageLabels[stage || 'explore'] || explorationStageLabels.explore
}

// A discussion of validation methods is not evidence that a solution has been
// implemented or passed validation. Only report stages with effective answers.
export function brainstormDiscussionSummary(state: CreationBrainstormState) {
  const confirmedIds = new Set(state.decisions.filter(decision => (
    decision.source === 'user' && decision.summary.trim().length > 0
  )).map(decision => decision.question_id))
  const discussed = new Set((state.history || []).filter(turn => confirmedIds.has(turn.question.id))
    .map(turn => turn.question.exploration_stage || 'explore'))
  const stages: CreationBrainstormExplorationStage[] = ['solutions', 'implementation', 'validation']
  const labels = stages.filter(stage => discussed.has(stage)).map(brainstormExplorationStageLabel)
  return labels.length ? `已讨论到：${labels.join('、')}。` : ''
}

// answered_count counts processed questions, including skipped and excluded
// ones. Only an explicit user decision belongs in the confirmed total.
export function brainstormDecisionSummary(decisions: CreationBrainstormState['decisions']) {
  const count = (source: string) => decisions.filter(decision => (
    decision.source === source && decision.summary.trim().length > 0
  )).length
  const parts = [`已确认 ${count('user')} 项决定`]
  const assumptions = count('agent_assumption')
  const excluded = count('user_excluded')
  if (assumptions) parts.push(`${assumptions} 项合理假设`)
  if (excluded) parts.push(`${excluded} 项排除约束`)
  return parts.join('，')
}
