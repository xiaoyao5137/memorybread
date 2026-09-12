import type { CreationBrainstormQuestion, CreationBrainstormState } from '../store/useAppStore'

interface Props {
  state: CreationBrainstormState
  question: CreationBrainstormQuestion
}

export default function CreationBrainstormBranch({ state, question }: Props) {
  const turns = [...(state.history || []), ...(state.archived_questions || [])]
  const path: string[] = []
  const visited = new Set([question.id])
  let current = question
  // Sibling questions arrive in answer order. Follow explicit ancestry instead
  // of treating the most recently answered question as the current parent.
  while (current.parent_question_id && !visited.has(current.parent_question_id)) {
    const parent = turns.find(turn => turn.question.id === current.parent_question_id)
    if (!parent) break
    visited.add(parent.question.id)
    const label = state.brief_edits?.[parent.question.id] !== undefined
      ? parent.question.dimension
      : parent.question.options.find(option => option.id === current.parent_option_id)?.label
        || (parent.answer?.custom_text ? '自定义思路' : parent.question.dimension)
    if (label) path.unshift(label)
    current = parent.question
  }
  if (!path.length) return null
  return <p className="creation-brainstorm-card__why creation-brainstorm-context" aria-label="当前脑暴方向">
    当前方向：{path.join(' › ')}
  </p>
}
