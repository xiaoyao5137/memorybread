import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import CreationBrainstormBranch from '../components/CreationBrainstormBranch'
import type { CreationBrainstormQuestion, CreationBrainstormState } from '../store/useAppStore'

const question = (id: string, parentId?: string, optionId?: string): CreationBrainstormQuestion => ({
  id, parent_question_id: parentId, parent_option_id: optionId, dimension: `${id} 维度`,
  type: 'multi_choice', prompt: `${id} 问题`, why_now: '', required: true, allow_custom: true,
  answer_template: '', options: [
    { id: 'a', label: `${id} 方向 A`, description: '' },
    { id: 'b', label: `${id} 方向 B`, description: '' },
  ],
})
const root = question('root')
const branchA = question('branch-a', 'root', 'a')
const branchB = question('branch-b', 'root', 'b')
const answer = { selected_option_ids: ['a', 'b'], custom_text: '', source: 'user' }
const state: CreationBrainstormState = {
  session_id: 'branch-path', phase: 'exploring', revision: 2, answered_count: 2, depth: 2,
  current_question: branchB, brief_markdown: '', can_continue_brainstorm: false,
  open_flags: [], readiness_reason: '', continuation_directions: [], invalidated_question_ids: [], decisions: [],
  history: [{ question: root, answer }, { question: branchA, answer }],
}

describe('脑暴方向路径', () => {
  it('shows the actual sibling branch and retains ancestry when reviewing another question', () => {
    const view = render(<CreationBrainstormBranch state={state} question={branchB} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：root 方向 B')
    expect(screen.queryByText(/branch-a 方向/)).not.toBeInTheDocument()
    view.rerender(<CreationBrainstormBranch state={state} question={branchA} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：root 方向 A')
    view.rerender(<CreationBrainstormBranch state={state} question={question('child', 'branch-a', 'b')} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：root 方向 A › branch-a 方向 B')
  })

  it('does not invent ancestry for a root or an unavailable parent', () => {
    const view = render(<CreationBrainstormBranch state={state} question={root} />)
    expect(screen.queryByLabelText('当前脑暴方向')).not.toBeInTheDocument()
    view.rerender(<CreationBrainstormBranch state={state} question={question('missing', 'unknown')} />)
    expect(screen.queryByLabelText('当前脑暴方向')).not.toBeInTheDocument()
  })

  it('keeps custom answers concise and does not show stale options after manual brief edits', () => {
    const custom = { ...state, history: [{ question: root, answer: { ...answer, selected_option_ids: [], custom_text: '用户的完整约束' } }] }
    const view = render(<CreationBrainstormBranch state={custom} question={question('custom-child', 'root')} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：自定义思路')
    view.rerender(<CreationBrainstormBranch state={{ ...state, brief_edits: { root: '新方案' } }} question={branchB} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：root 维度')
  })

  it('can inspect archived ancestry and terminates a malformed cycle', () => {
    const archived = { ...state, history: [], archived_questions: [{ question: root, answer: null }] }
    const view = render(<CreationBrainstormBranch state={archived} question={branchB} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：root 方向 B')
    const cyclic = { ...state, history: [{ question: question('root', 'branch-b', 'a'), answer }] }
    view.rerender(<CreationBrainstormBranch state={cyclic} question={branchB} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：root 方向 B')
  })
})
