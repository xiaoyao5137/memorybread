import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import CreationBrainstormSummary from '../components/CreationBrainstormSummary'
import type { CreationBrainstormQuestion, CreationBrainstormState } from '../store/useAppStore'

const question = (id: string): CreationBrainstormQuestion => ({
  id, dimension: id, type: 'multi_choice', prompt: `请选择${id}`, why_now: '确认创作方向',
  required: true, allow_custom: true, answer_template: '',
  options: [{ id: 'selected', label: `${id}的具体选项`, description: '具体安排', recommended: false }],
})

const createState = (overrides: Partial<CreationBrainstormState> = {}): CreationBrainstormState => ({
  session_id: 'summary-test', revision: 3, phase: 'ready', root_request: '写一份项目方案',
  current_question: null, brief_markdown: '', answered_count: 2, depth: 2,
  can_continue_brainstorm: true, open_flags: ['后续可进一步细化执行安排。'],
  readiness_reason: '已有足够信息', continuation_directions: [], invalidated_question_ids: [],
  decisions: [
    { question_id: 'audience', dimension: '目标读者', summary: '面向项目执行团队', source: 'user' },
    { question_id: 'scope', dimension: '内容范围', summary: '覆盖本季度已确认项目', source: 'user' },
  ],
  history: ['audience', 'scope'].map(id => ({
    question: question(id), answer: { selected_option_ids: ['selected'], custom_text: '', source: 'user' },
  })),
  ...overrides,
})

describe('脑暴决定摘要', () => {
  it('keeps a legacy refinement suggestion without claiming an answered question remains an open assumption', () => {
    const state = createState()
    const original = structuredClone(state)

    const view = render(<CreationBrainstormSummary state={state} />)

    expect(screen.getByText('已确认 2 项决定。')).toBeInTheDocument()
    const openItems = screen.getByRole('region', { name: '待决定与补充' })
    expect(within(openItems).getByText('待决定与补充（1 项）')).toBeInTheDocument()
    expect(within(openItems).getByRole('listitem')).toHaveTextContent(state.open_flags[0])
    expect(screen.queryByText(/开放假设|其余/)).not.toBeInTheDocument()
    expect(state).toEqual(original)
    view.unmount()
    expect(state).toEqual(original)
  })

  it('shows every actual unresolved item verbatim alongside the confirmed decisions', () => {
    const flags = ['交付日期尚未决定，请在排期后补充。', '预算上限尚未明确；需要负责人确认。']
    render(<CreationBrainstormSummary state={createState({ open_flags: flags })} />)

    expect(screen.getByText('已确认 2 项决定。')).toBeInTheDocument()
    const openItems = screen.getByRole('region', { name: '待决定与补充' })
    expect(within(openItems).getByText('待决定与补充（2 项）')).toBeInTheDocument()
    expect(within(openItems).getAllByRole('listitem').map(item => item.textContent)).toEqual(flags)
  })

  it('counts skipped assumptions and exclusions separately from user confirmations', () => {
    const state = createState()
    state.answered_count = 4
    state.decisions.push(
      { question_id: 'schedule', dimension: '排期', summary: '暂按下月交付', source: 'agent_assumption' },
      { question_id: 'channel', dimension: '分发渠道', summary: '不使用公开渠道', source: 'user_excluded' },
    )
    state.history!.push(
      { question: question('schedule'), answer: { selected_option_ids: [], custom_text: '', source: 'agent_assumption' } },
      { question: question('channel'), answer: { selected_option_ids: [], custom_text: '', source: 'user_excluded' } },
    )

    render(<CreationBrainstormSummary state={state} />)

    expect(screen.getByText('已确认 2 项决定，1 项合理假设，1 项排除约束。')).toBeInTheDocument()
    expect(screen.queryByText(/已确认 4 项决定/)).not.toBeInTheDocument()
  })

  it('uses the effective decision source after a user manually revises an assumption', () => {
    const state = createState()
    state.decisions[1].source = 'agent_assumption'
    state.history![1].answer.source = 'agent_assumption'
    state.history![1].answer.selected_option_ids = []
    const view = render(<CreationBrainstormSummary state={state} />)
    expect(screen.getByText('已确认 1 项决定，1 项合理假设。')).toBeInTheDocument()

    const revised = {
      ...state,
      brief_edits: { scope: '仅覆盖已经立项的项目' },
      decisions: state.decisions.map(decision => decision.question_id === 'scope'
        ? { ...decision, summary: '仅覆盖已经立项的项目', source: 'user' }
        : decision),
    }
    view.rerender(<CreationBrainstormSummary state={revised} />)

    expect(screen.getByText('已确认 2 项决定。')).toBeInTheDocument()
    expect(screen.queryByText(/合理假设/)).not.toBeInTheDocument()
    expect(revised.history![1].answer.source).toBe('agent_assumption')
  })

  it('shows the empty pending-items state without inventing remaining assumptions', () => {
    render(<CreationBrainstormSummary state={createState({ open_flags: [] })} />)

    expect(screen.getByText('已确认 2 项决定。')).toBeInTheDocument()
    expect(screen.getByText('当前没有待决定与补充事项。')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '待决定与补充' })).not.toBeInTheDocument()
    expect(screen.queryByText(/开放假设/)).not.toBeInTheDocument()
  })

  it('stops counting a confirmed decision when the user clears its summary', () => {
    const state = createState()
    state.decisions[1].summary = '  \n  '
    state.brief_edits = { scope: '' }

    render(<CreationBrainstormSummary state={state} />)

    expect(screen.getByText('已确认 1 项决定。')).toBeInTheDocument()
    expect(screen.queryByText(/已确认 2 项决定/)).not.toBeInTheDocument()
  })

  it('summarizes discussed depth without claiming the solution has been implemented or validated', () => {
    const stages = ['solutions', 'implementation', 'validation'] as const
    const depthState = createState({
      history: stages.map(stage => ({ question: { ...question(stage), exploration_stage: stage }, answer: { selected_option_ids: ['selected'], custom_text: '', source: 'user' } })),
      decisions: stages.map(stage => ({ question_id: stage, dimension: stage, summary: `${stage} 的用户选择`, source: 'user' })),
    })
    render(<CreationBrainstormSummary state={depthState} />)
    expect(screen.getByText('已讨论到：探索解法、细化落地、验证效果。')).toBeInTheDocument()
    expect(screen.queryByText(/已落实|验证通过|深挖完成率/)).not.toBeInTheDocument()
  })

  it('does not mark a stage discussed from an unanswered, excluded, archived or cleared question', () => {
    render(<CreationBrainstormSummary state={createState({
      current_question: { ...question('current'), exploration_stage: 'validation' },
      history: [
        { question: { ...question('excluded'), exploration_stage: 'solutions' }, answer: { selected_option_ids: [], custom_text: '', source: 'user_excluded' } },
        { question: { ...question('cleared'), exploration_stage: 'implementation' }, answer: { selected_option_ids: ['selected'], custom_text: '', source: 'user' } },
      ],
      decisions: [
        { question_id: 'excluded', dimension: '探索解法', summary: '用户排除该方向', source: 'user_excluded' },
        { question_id: 'cleared', dimension: '细化落地', summary: '  ', source: 'user' },
      ],
      archived_questions: [{ question: { ...question('archived'), exploration_stage: 'validation' }, answer: { selected_option_ids: ['selected'], custom_text: '', source: 'user' } }],
    })} />)
    expect(screen.queryByText(/已讨论到/)).not.toBeInTheDocument()
  })
})
