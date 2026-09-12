import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import CreationBriefEditor from '../components/CreationBriefEditor'
import CreationBrainstormBranch from '../components/CreationBrainstormBranch'
import { useAppStore, type CreationBrainstormQuestion, type CreationBrainstormState } from '../store/useAppStore'

const question = (id: string, dimension: string, parentOption?: string): CreationBrainstormQuestion => ({
  id, dimension, prompt: `${dimension}如何安排？`, type: 'multi_choice',
  parent_question_id: parentOption ? 'root' : undefined, parent_option_id: parentOption,
  exploration_stage: parentOption ? 'solutions' : 'explore',
  why_now: '从不同角度完整讨论已经确认的方向。', required: true, allow_custom: true,
  answer_template: '补充你的约束。', options: [
    { id: 'a', label: '小范围试点', description: '先验证可行性。' },
    { id: 'b', label: '全面铺开', description: '需要更多投入。' },
  ],
})
const root = { ...question('root', '总体方向'), options: [
  { id: 'process', label: '评估流程', description: '讨论样本和分工。' },
  { id: 'delivery', label: '交付反馈', description: '讨论结果如何使用。' },
] }
const first = question('process.samples', '样本组织', 'process')
const second = question('process.roles', '人员分工', 'process')
const other = question('delivery.feedback', '结果反馈', 'delivery')
const rootAnswer = { selected_option_ids: ['process', 'delivery'], custom_text: '', source: 'user' }
const answer = { selected_option_ids: ['a'], custom_text: '', source: 'user' }
const initial: CreationBrainstormState = {
  session_id: 'same-option-siblings', root_request: '讨论评估流程和交付方式。', phase: 'exploring',
  revision: 1, current_question: first, brief_markdown: '', answered_count: 1, depth: 1,
  can_continue_brainstorm: false, open_flags: [], readiness_reason: '', continuation_directions: [],
  invalidated_question_ids: [], history: [{ question: root, answer: rootAnswer }],
  decisions: [{ question_id: 'root', dimension: root.dimension, summary: '评估流程；交付反馈', source: 'user' }],
}
const next: CreationBrainstormState = {
  ...initial, revision: 2, current_question: second, answered_count: 2, depth: 2,
  history: [...initial.history!, { question: first, answer }],
  decisions: [...initial.decisions, { question_id: first.id, dimension: first.dimension, summary: '小范围试点', source: 'user' }],
}

describe('同一选项的多个同层延展问题', () => {
  beforeEach(() => {
    window.localStorage.clear()
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  it('按问题 ID 切换并清空选择，回看保持原答案且不会提交后续问题', async () => {
    const requests: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname
      if (path === '/api/creation/skills') return Response.json([])
      if (path === '/api/creation/history') return Response.json({ items: [], total: 0 })
      if (path === '/api/creation/brainstorm/turn') {
        requests.push(JSON.parse(String(init?.body)))
        return Response.json(next)
      }
      return new Response('{}', { status: 404 })
    }))
    useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', sessionId: initial.session_id,
      rootRequest: initial.root_request!, brainstormState: initial })
    render(<CreationPanel />)
    expect(screen.getByText('样本组织如何安排？')).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /小范围试点/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))

    expect(await screen.findByText('人员分工如何安排？')).toBeInTheDocument()
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：评估流程')
    expect(screen.getByText('第 3 / 3 题')).toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /小范围试点/ })).not.toBeChecked()
    expect(screen.getByRole('button', { name: /确认并继续/ })).toBeDisabled()
    expect(requests).toEqual([expect.objectContaining({ action: 'answer', question_id: first.id, revision: 1,
      answer: { selected_option_ids: ['a'], custom_text: '' } })])
    expect(useAppStore.getState().creationDraft.brainstormState?.decisions).toEqual(next.decisions)

    fireEvent.click(screen.getByRole('button', { name: '上一题' }))
    expect(screen.getByText('样本组织如何安排？')).toBeInTheDocument()
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：评估流程')
    expect(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /小范围试点/ })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '下一题' }))
    expect(screen.getByText('人员分工如何安排？')).toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: '答案选项' })).getByRole('checkbox', { name: /小范围试点/ })).not.toBeChecked()
    expect(requests).toHaveLength(1)
  })

  it('将同选项的两道问题放在同一层，树节点查看不会形成新答案或简报决定', () => {
    const original = structuredClone(next)
    const onSave = vi.fn()
    const onDraftChange = vi.fn()
    render(<CreationBriefEditor state={next} rootRequest="" disabled={false} onSave={onSave} onDraftChange={onDraftChange} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    const firstNode = within(tree).getByRole('button', { name: '样本组织 已回答 · 探索解法' })
    const secondNode = within(tree).getByRole('button', { name: '人员分工 当前问题 · 探索解法' })
    expect(firstNode.parentElement?.parentElement).toBe(secondNode.parentElement?.parentElement)
    expect(firstNode.parentElement?.style.getPropertyValue('--creation-brief-tree-depth')).toBe('3')
    expect(secondNode.parentElement?.style.getPropertyValue('--creation-brief-tree-depth')).toBe('3')
    fireEvent.click(secondNode)
    const details = screen.getByRole('region', { name: '思路节点题目' })
    expect(details).toHaveTextContent('父问题总体方向如何安排？所属分支评估流程')
    expect(details).not.toHaveTextContent('✓')
    expect(screen.getByText('已确认 2 项决定')).toBeInTheDocument()
    expect(screen.queryByLabelText('简报：人员分工')).not.toBeInTheDocument()
    expect(onSave).not.toHaveBeenCalled()
    expect(onDraftChange).not.toHaveBeenCalled()
    expect(next).toEqual(original)
  })

  it('回看同层题使用共同父选项，轮转到其他方向后也不误接上一题', () => {
    const view = render(<CreationBrainstormBranch state={next} question={second} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：评估流程')
    view.rerender(<CreationBrainstormBranch state={next} question={first} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：评估流程')
    view.rerender(<CreationBrainstormBranch state={next} question={other} />)
    expect(screen.getByLabelText('当前脑暴方向')).toHaveTextContent('当前方向：交付反馈')
    expect(screen.queryByText(/评估流程 ›|样本组织 ›/)).not.toBeInTheDocument()
  })

  it('上游回改后同一选项的旧同层题可回看，但不混入新当前题或确认数', () => {
    const revised: CreationBrainstormState = {
      ...initial, revision: 3, current_question: other,
      invalidated_question_ids: [first.id, second.id],
      archived_questions: [{ question: first, answer }, { question: second, answer: null }],
    }
    render(<CreationBriefEditor state={revised} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '人员分工 已失效 · 可回看 · 探索解法' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('人员分工如何安排？')
    expect(screen.getByRole('region', { name: '思路节点题目' })).not.toHaveTextContent('✓')
    expect(screen.getByRole('button', { name: '结果反馈 当前问题 · 探索解法' })).toBeInTheDocument()
    expect(screen.getByText('已确认 1 项决定')).toBeInTheDocument()
  })
})
