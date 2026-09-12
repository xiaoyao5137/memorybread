import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import CreationBriefEditor from '../components/CreationBriefEditor'
import type { CreationBrainstormState, CreationBrainstormQuestion } from '../store/useAppStore'
const question = (id: string): CreationBrainstormQuestion => ({ id, dimension: id, type: 'multi_choice', prompt: `${id}的具体题目`, why_now: '确认思路', required: true, allow_custom: true, answer_template: '', options: [
  { id: 'a', label: '方向 A', description: 'A 的取舍', recommended: true },
  { id: 'b', label: '方向 B', description: 'B 的取舍', recommended: false },
] })
const state: CreationBrainstormState = { session_id: 'test', revision: 2, phase: 'exploring', root_request: '写方案', brief_markdown: '', answered_count: 1, depth: 1, can_continue_brainstorm: false, open_flags: ['待确认'], readiness_reason: '', continuation_directions: [], invalidated_question_ids: [], decisions: [{ question_id: 'root', dimension: '方案方向', summary: '方向 A；方向 B', source: 'user' }], history: [{ question: question('root'), answer: { selected_option_ids: ['a','b'], custom_text: '', source: 'user' } }], current_question: { ...question('child'), parent_question_id: 'root', parent_option_id: 'a' } }
describe('创作简报编辑器', () => {
  it('shows branches and opens complete questions from tree nodes', () => {
    render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    fireEvent.click(within(tree).getByRole('button', { name: 'child 当前问题 · 问题与方向' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('child的具体题目')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('A 的取舍')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('父问题root的具体题目所属分支方向 A')
    expect(within(tree).getByText('待深入')).toBeInTheDocument()
  })
  it('expands and collapses the tree while retaining node details and unsaved brief edits', () => {
    const original = structuredClone(state)
    const onSave = vi.fn()
    const onDraftChange = vi.fn()
    render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={onSave} onDraftChange={onDraftChange} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    const expand = within(tree).getByRole('button', { name: '展开完整思路树' })
    const viewport = document.getElementById(expand.getAttribute('aria-controls')!)!
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    expect(viewport).not.toHaveClass('is-expanded')

    const brief = screen.getByLabelText('简报：方案方向')
    fireEvent.change(brief, { target: { value: '保留待保存的简报修订' } })
    fireEvent.click(expand)
    expect(viewport).toHaveClass('is-expanded')
    const collapse = within(tree).getByRole('button', { name: '收起思路树' })
    expect(collapse).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(within(tree).getByRole('button', { name: 'child 当前问题 · 问题与方向' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('child的具体题目')
    fireEvent.click(collapse)

    expect(viewport).not.toHaveClass('is-expanded')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('child的具体题目')
    expect(brief).toHaveValue('保留待保存的简报修订')
    expect(onDraftChange).toHaveBeenCalledTimes(1)
    expect(onSave).not.toHaveBeenCalled()
    expect(state).toEqual(original)
  })
  it('brings newly selected and reselected details into view and returns to the heading when collapsed from the bottom', () => {
    const originalDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
    const scrollTargets: HTMLElement[] = []
    const scrollIntoView = vi.fn(function (this: HTMLElement) { scrollTargets.push(this) })
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
    try {
      render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
      const tree = screen.getByRole('region', { name: '脑暴思路树' })
      fireEvent.click(within(tree).getByRole('button', { name: '展开完整思路树' }))
      expect(scrollIntoView).not.toHaveBeenCalled()
      const node = within(tree).getByRole('button', { name: 'child 当前问题 · 问题与方向' })
      fireEvent.click(node)
      const detail = screen.getByRole('region', { name: '思路节点题目' })
      expect(scrollTargets).toEqual([detail.parentElement])
      expect(scrollIntoView).toHaveBeenLastCalledWith({ block: 'nearest', inline: 'nearest' })
      fireEvent.click(node)
      expect(scrollTargets).toEqual([detail.parentElement, detail.parentElement])

      const bottomCollapse = within(tree).getByRole('button', { name: '收起思路树并返回顶部' })
      const viewport = document.getElementById(bottomCollapse.getAttribute('aria-controls')!)!
      expect(bottomCollapse).toHaveAttribute('aria-expanded', 'true')
      fireEvent.click(bottomCollapse)
      expect(viewport).not.toHaveClass('is-expanded')
      expect(scrollTargets[2]).toBe(within(tree).getByRole('heading', { name: '脑暴思路树' }).parentElement)
      expect(within(tree).queryByRole('button', { name: '收起思路树并返回顶部' })).not.toBeInTheDocument()
      expect(detail).toHaveTextContent('child的具体题目')
    } finally {
      if (originalDescriptor) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalDescriptor)
      else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
    }
  })
  it('persists empty edits and retains unsaved inputs after a failed save', async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new Error('保存失败')).mockResolvedValueOnce(undefined)
    const dirty = vi.fn()
    render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={onSave} onDraftChange={dirty} />)
    const field = screen.getByLabelText('简报：方案方向')
    fireEvent.change(field, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: '保存简报修改' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('保存失败'))
    expect(field).toHaveValue('')
    fireEvent.click(screen.getByRole('button', { name: '保存简报修改' }))
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith({}))
    expect(onSave).toHaveBeenLastCalledWith({ root: '' })
  })
  it('keeps manual edits while newly generated decisions become editable', () => {
    render(<CreationBriefEditor state={{ ...state, brief_edits: { root: '用户修订' } }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    expect(screen.getByLabelText('简报：方案方向')).toHaveValue('用户修订')
    expect(screen.getByLabelText('简报：创作需求')).toHaveValue('写方案')
  })

  it('preserves supplemental suggestions without treating them as unanswered choices or writing the brief', () => {
    const suggestion = '后续可进一步细化执行安排。'
    const completeState: CreationBrainstormState = {
      ...state, answered_count: 2, current_question: null, open_flags: [suggestion],
      decisions: [...state.decisions, { question_id: 'audience', dimension: '目标读者', summary: '项目执行团队', source: 'user' }],
      history: [...state.history!, { question: question('audience'), answer: { selected_option_ids: ['b'], custom_text: '', source: 'user' } }],
    }
    const original = structuredClone(completeState)
    const onSave = vi.fn()
    const onDraftChange = vi.fn()
    render(<CreationBriefEditor state={completeState} rootRequest="" disabled={false} onSave={onSave} onDraftChange={onDraftChange} />)

    expect(screen.getByText('已确认 2 项决定')).toBeInTheDocument()
    expect(screen.getByLabelText('简报：待决定与补充')).toHaveValue(suggestion)
    expect(screen.queryByText(/开放假设|其余/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存简报修改' })).toBeDisabled()
    expect(onSave).not.toHaveBeenCalled()
    expect(onDraftChange).not.toHaveBeenCalled()
    expect(completeState).toEqual(original)
  })

  it('does not include skipped or excluded decisions in the user-confirmed count', () => {
    render(<CreationBriefEditor state={{ ...state, answered_count: 3, decisions: [
      ...state.decisions,
      { question_id: 'schedule', dimension: '排期', summary: '暂按下月交付', source: 'agent_assumption' },
      { question_id: 'channel', dimension: '分发渠道', summary: '不使用公开渠道', source: 'user_excluded' },
    ] }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)

    expect(screen.getByText('已确认 1 项决定，1 项合理假设，1 项排除约束')).toBeInTheDocument()
    expect(screen.queryByText(/已确认 3 项决定/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('简报：排期')).toBeEnabled()
    expect(screen.getByLabelText('简报：分发渠道')).toBeDisabled()
  })

  it('counts a manually revised decision by its effective source while keeping its original answer history', () => {
    const revisedState: CreationBrainstormState = {
      ...state, brief_edits: { root: '用户明确修订的方向' },
      decisions: [{ ...state.decisions[0], summary: '用户明确修订的方向', source: 'user' }],
      history: [{ question: question('root'), answer: { selected_option_ids: [], custom_text: '', source: 'agent_assumption' } }],
    }
    render(<CreationBriefEditor state={revisedState} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)

    expect(screen.getByText('已确认 1 项决定')).toBeInTheDocument()
    expect(screen.getByLabelText('简报：方案方向')).toHaveValue('用户明确修订的方向')
    expect(screen.queryByText(/合理假设/)).not.toBeInTheDocument()
    expect(revisedState.history![0].answer.source).toBe('agent_assumption')
  })

  it('does not count a user-cleared decision as confirmed', () => {
    render(<CreationBriefEditor state={{ ...state, brief_edits: { root: '' },
      decisions: [{ ...state.decisions[0], summary: '' }],
    }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)

    expect(screen.getByText('已确认 0 项决定')).toBeInTheDocument()
    expect(screen.getByLabelText('简报：方案方向')).toHaveValue('')
  })
  it('retains invalidated questions and original answers in the tree', () => {
    const archived = { ...question('old'), parent_question_id: 'root', parent_option_id: 'b' }
    render(<CreationBriefEditor state={{ ...state, archived_questions: [{ question: archived, answer: { selected_option_ids: ['b'], custom_text: '', source: 'user' } }] }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'old 已失效 · 可回看 · 问题与方向' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('old的具体题目')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('✓ 方向 B')
  })

  it('shows archived descendants after their parent option is deselected', () => {
    const archived = { ...question('old'), parent_question_id: 'root', parent_option_id: 'b' }
    render(<CreationBriefEditor state={{ ...state,
      history: [{ question: question('root'), answer: { selected_option_ids: ['a'], custom_text: '', source: 'user' } }],
      archived_questions: [{ question: archived, answer: { selected_option_ids: ['b'], custom_text: '', source: 'user' } }],
    }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'old 已失效 · 可回看 · 问题与方向' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('old的具体题目')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('✓ 方向 B')
  })

  it('does not clear another session draft when a save completes after leaving the editor', async () => {
    let completeSave!: () => void
    const onSave = vi.fn(() => new Promise<void>(resolve => { completeSave = resolve }))
    const dirty = vi.fn()
    const view = render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={onSave} onDraftChange={dirty} />)
    fireEvent.change(screen.getByLabelText('简报：方案方向'), { target: { value: '保留我的修订' } })
    fireEvent.click(screen.getByRole('button', { name: '保存简报修改' }))
    view.unmount()
    await act(async () => { completeSave(); await Promise.resolve() })
    expect(dirty).toHaveBeenCalledTimes(1)
    expect(dirty).toHaveBeenLastCalledWith({ root: '保留我的修订' })
  })

  it('keeps a single selected issue pending for solution exploration', () => {
    const singleIssue = { ...state, current_question: null,
      history: [{ question: question('root'), answer: { selected_option_ids: ['a'], custom_text: '', source: 'user' } }],
    }
    render(<CreationBriefEditor state={singleIssue} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    expect(within(tree).getByText('待深入')).toBeInTheDocument()
    expect(within(tree).queryByText('已讨论')).not.toBeInTheDocument()
  })

  it('shows solution, implementation and validation questions along their actual branch', () => {
    const solutions = { ...question('解法'), parent_question_id: 'root', parent_option_id: 'a', exploration_stage: 'solutions' as const }
    const implementation = { ...question('落地'), parent_question_id: '解法', parent_option_id: 'a', exploration_stage: 'implementation' as const }
    const validation = { ...question('验证方法'), parent_question_id: '落地', parent_option_id: 'a', exploration_stage: 'validation' as const }
    const answered = [question('root'), solutions, implementation, validation].map(item => ({
      question: item, answer: { selected_option_ids: ['a'], custom_text: '', source: 'user' },
    }))
    render(<CreationBriefEditor state={{ ...state, current_question: null, history: answered }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    expect(within(tree).getByRole('button', { name: '解法 已回答 · 探索解法' })).toBeInTheDocument()
    expect(within(tree).getByRole('button', { name: '落地 已回答 · 细化落地' })).toBeInTheDocument()
    fireEvent.click(within(tree).getByRole('button', { name: '验证方法 已回答 · 验证效果' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('讨论阶段：验证效果')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('父问题落地的具体题目所属分支方向 A')
    expect(within(tree).getByText('已讨论')).toBeInTheDocument()
    expect(within(tree).queryByText('待深入')).not.toBeInTheDocument()
    expect(within(tree).queryByText(/已落实|验证通过/)).not.toBeInTheDocument()
  })

  it('renders custom-answer descendants without a parent option and keeps the original answer inspectable', () => {
    const customState = { ...state,
      history: [{ question: question('root'), answer: { selected_option_ids: [], custom_text: '先做小规模试点，再按反馈调整。', source: 'user' } }],
      current_question: { ...question('定制方案'), parent_question_id: 'root', parent_option_id: null, exploration_stage: 'solutions' as const },
    }
    const original = structuredClone(customState)
    render(<CreationBriefEditor state={customState} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    fireEvent.click(within(tree).getByRole('button', { name: '定制方案 当前问题 · 探索解法' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('定制方案的具体题目')
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('父问题root的具体题目所属分支自定义思路')
    fireEvent.click(within(tree).getByRole('button', { name: '自定义思路' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('先做小规模试点，再按反馈调整。')
    expect(within(tree).queryByText('待深入')).not.toBeInTheDocument()
    expect(customState).toEqual(original)
  })

  it('renders whole-question descendants even when no custom option was selected', () => {
    render(<CreationBriefEditor state={{ ...state,
      current_question: { ...question('综合落地'), parent_question_id: 'root', parent_option_id: null, exploration_stage: 'implementation' },
    }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '综合落地 当前问题 · 细化落地' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('父问题root的具体题目所属分支整体思路')
  })

  it.each(['agent_assumption', 'user_excluded'])('does not schedule an implied branch for %s', source => {
    render(<CreationBriefEditor state={{ ...state, current_question: null,
      decisions: [{ ...state.decisions[0], source }],
      history: [{ question: question('root'), answer: { selected_option_ids: ['a'], custom_text: '', source } }],
    }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    expect(within(tree).queryByText('待深入')).not.toBeInTheDocument()
    expect(within(tree).queryByText('已讨论')).not.toBeInTheDocument()
  })

  it('keeps original options as history after a brief override without claiming they remain pending', () => {
    render(<CreationBriefEditor state={{ ...state, current_question: null, brief_edits: { root: '按用户重新修订的整体方案展开' } }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    expect(within(tree).getAllByText('原分支')).toHaveLength(2)
    expect(within(tree).queryByText('待深入')).not.toBeInTheDocument()
  })

  it('does not promise pending exploration after the user ended the session', () => {
    render(<CreationBriefEditor state={{ ...state, phase: 'abandoned', current_question: null }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    expect(within(screen.getByRole('region', { name: '脑暴思路树' })).queryByText('待深入')).not.toBeInTheDocument()
  })

  it('retains the detailed rationale and constraints when reviewing a generated solution', () => {
    render(<CreationBriefEditor state={{ ...state, current_question: {
      ...question('深入解法'), exploration_stage: 'solutions', context_details: '仅针对当前已确认的问题讨论，资源条件仍需核对。',
      options: [{ id: 'a', label: '先做试点', description: '控制投入后验证可行性。', details: '需先约定试点范围；失败时停止扩展。' }],
    } }} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '深入解法 当前问题 · 探索解法' }))
    const detail = screen.getByRole('region', { name: '思路节点题目' })
    const background = within(detail).getByText('查看背景与依据').closest('details')!
    const rationale = within(detail).getByText('查看选项依据').closest('details')!
    expect(background).toHaveTextContent('仅针对当前已确认的问题讨论，资源条件仍需核对。')
    expect(rationale).toHaveTextContent('需先约定试点范围；失败时停止扩展。')
    expect(background).not.toHaveAttribute('open')
    expect(rationale).not.toHaveAttribute('open')
  })

})
