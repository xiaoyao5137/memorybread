import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CreationBrainstormStages from '../components/CreationBrainstormStages'
import CreationBriefEditor from '../components/CreationBriefEditor'
import { stageFixture, stageQuestion } from '../../test/brainstorm-stage-fixture'

const handlers = () => ({ busy: false, onConfirmStage: vi.fn(), onConfirmChapter: vi.fn(), onRetry: vi.fn() })
afterEach(() => { vi.useRealTimers() })

describe('三阶段脑暴进度与完整思路', () => {
  it('renders persisted batch progress and an estimated duration without timer-based fabrication', () => {
    const state = stageFixture()
    const view = render(<CreationBrainstormStages workflow={state.stage_workflow!} {...handlers()} />)
    expect(screen.getByText('创作方向阶段模型思考中')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
    expect(screen.getByText('已完成 2 / 5 批')).toBeInTheDocument()
    state.stage_workflow!.stages[0].progress.total_batches = 0
    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...handlers()} />)
    expect(screen.getByText('正在规划本阶段批次')).toBeInTheDocument()
    expect(screen.queryByText(/0 \/ 0 批/)).not.toBeInTheDocument()
    expect(screen.getByText('预计剩余约 2 分钟 · 按已完成批次估算')).toBeInTheDocument()
    state.stage_workflow!.stages[0].progress.estimated_remaining_seconds = null
    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...handlers()} />)
    expect(screen.getByText('预计耗时：正在估算')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
  })

  it('marks an overdue estimate without counting down or advancing model progress, and accepts a new batch estimate', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-26T10:00:00Z'))
    const state = stageFixture()
    const progress = state.stage_workflow!.stages[0].progress
    progress.estimated_remaining_seconds = 2
    const view = render(<CreationBrainstormStages workflow={state.stage_workflow!} {...handlers()} />)
    expect(screen.getByText('预计剩余约 2 秒 · 按已完成批次估算')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.getByText('预计剩余约 2 秒 · 按已完成批次估算')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1001) })
    expect(screen.getByText('耗时超出预估，正在继续')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
    progress.updated_at_ms = Date.now()
    progress.estimated_remaining_seconds = 5
    progress.completed_batches = 3
    progress.percent = 60
    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...handlers()} />)
    expect(screen.getByText('预计剩余约 5 秒 · 按已完成批次估算')).toBeInTheDocument()
    expect(screen.queryByText('耗时超出预估，正在继续')).not.toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '60')
  })

  it('recognizes an expired persisted estimate immediately when returning to the page', () => {
    const state = stageFixture()
    state.stage_workflow!.stages[0].progress.updated_at_ms = Date.now() - 10000
    state.stage_workflow!.stages[0].progress.estimated_remaining_seconds = 2
    render(<CreationBrainstormStages workflow={state.stage_workflow!} {...handlers()} />)
    expect(screen.getByText('耗时超出预估，正在继续')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
  })

  it('keeps pending stages unavailable and explicitly confirms a complete stage', () => {
    const actions = handlers()
    render(<CreationBrainstormStages workflow={stageFixture('ready').stage_workflow!} {...actions} />)
    fireEvent.click(screen.getByRole('button', { name: '确认创作方向，进入创作细节' }))
    expect(actions.onConfirmStage).toHaveBeenCalledOnce()
    expect(screen.getAllByText('待开始')).toHaveLength(2)
  })

  it('shows the saved reason for a v17 stage with no content questions, including after stage completion', () => {
    const state = stageFixture('ready')
    const workflow = state.stage_workflow!
    const stage = workflow.stages[0]
    stage.preparation_contract_version = 17
    stage.questions = []
    stage.content_no_new_decision_reason = '本次创作方向已由用户原始需求明确。'
    const view = render(<CreationBrainstormStages workflow={workflow} {...handlers()} />)
    expect(screen.getByText('创作方向未新增内容题：本次创作方向已由用户原始需求明确。')).toBeInTheDocument()
    expect(screen.getByText('创作方向没有新增待确认的内容题。确认后开始下一阶段思考。')).toBeInTheDocument()
    expect(screen.queryByText('创作方向已逐项确认。确认阶段结果后，开始下一阶段思考。')).not.toBeInTheDocument()

    stage.status = 'completed'
    workflow.active_stage = 'detail'
    workflow.stages[1].status = 'thinking'
    view.rerender(<CreationBrainstormStages workflow={workflow} {...handlers()} />)
    expect(screen.getByText('创作方向未新增内容题：本次创作方向已由用户原始需求明确。')).toBeInTheDocument()
  })

  it('uses flat question wording for v17 and explains a zero-question writing stage', () => {
    const state = stageFixture('answering')
    const workflow = state.stage_workflow!
    workflow.stages[0].preparation_contract_version = 17
    const view = render(<CreationBrainstormStages workflow={workflow} {...handlers()} />)
    expect(screen.getByText('本阶段已准备 6 题，请逐题确认。可在创作简报中查看全部候选问题与选项。')).toBeInTheDocument()
    expect(screen.queryByText(/按选择展开/)).not.toBeInTheDocument()

    workflow.active_stage = 'writing'
    workflow.stages[0].status = 'completed'
    workflow.stages[2].status = 'ready'
    workflow.stages[2].preparation_contract_version = 17
    workflow.stages[2].questions = []
    workflow.stages[2].content_no_new_decision_reason = '正文结构已由当前有效决定确定。'
    workflow.chapters = [{ id: 'body', title: '创作正文', goal: '完成正文', status: 'ready', question_ids: [] }]
    workflow.active_chapter_id = 'body'
    view.rerender(<CreationBrainstormStages workflow={workflow} {...handlers()} />)
    expect(screen.getByText('创作行文未新增内容题：正文结构已由当前有效决定确定。')).toBeInTheDocument()
    expect(screen.getByText('“创作正文”没有新增待确认的内容题，确认后开始创作。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认本章设计并创作' })).toBeEnabled()
  })

  it('explains a saved writing question that repeated an already confirmed detail question', () => {
    const state = stageFixture('ready', 'writing')
    const workflow = state.stage_workflow!
    workflow.stages[1].status = 'completed'
    const repeated = stageQuestion('repeated-writing', '推理加速引擎')
    repeated.prompt = '推理加速引擎与调度器的协同机制如何描述？'
    workflow.stages[1].questions = [stageQuestion('answered-detail', '推理加速引擎')]
    workflow.stages[1].questions[0].prompt = repeated.prompt
    workflow.stages[2].questions = [repeated]
    render(<CreationBrainstormStages workflow={workflow} {...handlers()} />)
    expect(screen.getByText(/本章行文题与前面已确认的问题重复/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认本章设计并创作' })).toBeEnabled()
  })

  it('shows a zero-content-question reason even when detail retains an answered input clarification', () => {
    const state = stageFixture('ready', 'detail')
    const workflow = state.stage_workflow!
    const stage = workflow.stages[1]
    stage.preparation_contract_version = 17
    stage.questions = [{ ...stage.questions[0], input_clarification: { kind: 'report_period', need_ids: [] } }]
    stage.content_no_new_decision_reason = '本阶段内容取舍已由用户确认；报告周期题已单独回答。'
    render(<CreationBrainstormStages workflow={workflow} {...handlers()} />)
    expect(screen.getByText('创作细节未新增内容题：本阶段内容取舍已由用户确认；报告周期题已单独回答。')).toBeInTheDocument()
    expect(screen.getByText('创作细节没有新增待确认的内容题。确认后开始下一阶段思考。')).toBeInTheDocument()
  })

  it('shows retryable failure and preserves the stage outline', () => {
    const state = stageFixture('failed')
    state.stage_workflow!.stages[0].error = { code: 'FAILED', message: '连接中断，已保存前两批', retryable: true }
    const actions = handlers()
    render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.getByRole('alert')).toHaveTextContent('连接中断，已保存前两批')
    fireEvent.click(screen.getByRole('button', { name: '继续本阶段思考' }))
    expect(actions.onRetry).toHaveBeenCalledOnce()
  })

  it('shows synchronization for an unverified restored failure without changing its saved state', () => {
    const state = stageFixture('failed', 'writing')
    state.stage_workflow!.stages[2].error = { code: 'BRAINSTORM_MODEL_OUTPUT_INVALID', message: '旧快照生成失败', retryable: true }
    const original = structuredClone(state)
    const actions = handlers()
    const view = render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} synchronizing />)
    expect(screen.getByText('正在同步创作行文进度')).toBeInTheDocument()
    expect(screen.getByText('进度同步中')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续本阶段思考' })).not.toBeInTheDocument()
    expect(screen.queryByText(/生成失败|思考暂停/)).not.toBeInTheDocument()
    expect(state).toEqual(original)
    expect(actions.onRetry).not.toHaveBeenCalled()

    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.getByRole('alert')).toHaveTextContent('旧快照生成失败')
    expect(screen.getByRole('button', { name: '继续本阶段思考' })).toBeEnabled()
  })

  it('shows saved questions during preparation and retry without advancing the completed-topic percentage', () => {
    const state = stageFixture()
    const stage = state.stage_workflow!.stages[0]
    stage.preparation_progress = { topic_title: '目标与读者', saved_nodes: 2, pending_nodes: 3 }
    const actions = handlers()
    const view = render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.getByText('目标与读者：已保存 2 道题，待准备 3 项思路。')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
    stage.status = 'failed'
    stage.error = { code: 'BRAINSTORM_MODEL_TIMEOUT', message: '本题思考超时，已保留当前进度', retryable: true }
    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.getByRole('alert')).toHaveTextContent('目标与读者：已保存 2 道题，待准备 3 项思路。继续后从未完成处接续。')
    fireEvent.click(screen.getByRole('button', { name: '继续本阶段思考' }))
    expect(actions.onRetry).toHaveBeenCalledOnce()
    expect(stage.progress.percent).toBe(40)
    expect(stage.questions).toHaveLength(6)
  })

  it('keeps bounded background recovery in the thinking state and preserves saved progress', () => {
    const state = stageFixture('thinking', 'writing')
    const stage = state.stage_workflow!.stages[2]
    stage.recovery_progress = { attempt: 1, max_attempts: 2, retry_at_ms: Date.now() + 2000 }
    stage.preparation_progress = { topic_title: '核心数据概览与周期界定', saved_nodes: 1, pending_nodes: 5 }
    // A stale error from an older snapshot must not override Core's active state.
    stage.error = { code: 'BRAINSTORM_MODEL_OUTPUT_INVALID', message: '脑暴问题未生成有效结果，请重试', retryable: true }
    const original = structuredClone(state)
    const actions = handlers()
    const view = render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.getByText('创作行文阶段模型思考中')).toBeInTheDocument()
    expect(screen.getByText('正在结合已确认内容完善本阶段思路。')).toBeInTheDocument()
    expect(screen.getByText('预计耗时：正在估算')).toBeInTheDocument()
    expect(screen.getByText('核心数据概览与周期界定：已保存 1 道题，待准备 5 项思路。')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40')
    expect(screen.getByText('0 / 2 章已完成')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续本阶段思考' })).not.toBeInTheDocument()
    expect(screen.queryByText(/失败|暂停|请重试/)).not.toBeInTheDocument()
    expect(actions.onRetry).not.toHaveBeenCalled()
    expect(state).toEqual(original)

    delete stage.recovery_progress
    delete stage.error
    stage.status = 'answering'
    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.queryByText('正在结合已确认内容完善本阶段思路。')).not.toBeInTheDocument()
    expect(screen.getByText(/本阶段已准备 6 题，按选择展开/)).toBeInTheDocument()
    expect(actions.onRetry).not.toHaveBeenCalled()
  })

  it('shows the real exhausted state after background recovery stops', () => {
    const state = stageFixture('failed', 'writing')
    state.stage_workflow!.stages[2].error = {
      code: 'BRAINSTORM_PREPARATION_EXHAUSTED', message: '本阶段准备暂未完成，已保留当前进度', retryable: true,
    }
    const actions = handlers()
    render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    expect(screen.getByRole('alert')).toHaveTextContent('本阶段准备暂未完成，已保留当前进度')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '继续本阶段思考' })).toBeEnabled()
    expect(actions.onRetry).not.toHaveBeenCalled()
  })

  it.each([
    ['BRAINSTORM_MODEL_OUTPUT_INVALID', true],
    ['MODEL_ACCESS_DENIED', false],
    ['BRAINSTORM_MODEL_OUTPUT_INVALID', false],
  ] as const)('honors the server retry permission for %s (retryable: %s)', (code, retryable) => {
    const state = stageFixture('failed')
    state.stage_workflow!.stages[0].error = { code, message: '本批思考未完成，之前的批次已保存', retryable }
    const actions = handlers()
    render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    const retry = screen.getByRole('button', { name: '继续本阶段思考' })
    if (retryable) expect(retry).toBeEnabled()
    else expect(retry).toBeDisabled()
    fireEvent.click(retry)
    expect(actions.onRetry).toHaveBeenCalledTimes(retryable ? 1 : 0)
  })

  it('requires chapter design confirmation and disables it during the Agent Loop', () => {
    const state = stageFixture('ready', 'writing')
    const actions = handlers()
    const view = render(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} />)
    fireEvent.click(screen.getByRole('button', { name: '确认本章设计并创作' }))
    expect(actions.onConfirmChapter).toHaveBeenCalledOnce()
    state.stage_workflow!.chapters[0].status = 'writing'
    view.rerender(<CreationBrainstormStages workflow={state.stage_workflow!} {...actions} busy />)
    expect(screen.getByRole('button', { name: '本章创作中' })).toBeDisabled()
    expect(screen.getByText('0 / 2 章已完成')).toBeInTheDocument()
  })

  it('shows every option and unselected descendant without counting them as decisions', () => {
    const state = stageFixture('answering')
    const original = structuredClone(state)
    render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    const tree = screen.getByRole('region', { name: '脑暴思路树' })
    expect(within(tree).getByText('创作方向 · 6 道问题')).toBeInTheDocument()
    expect(within(tree).getAllByText('候选选项 · 未确认')).toHaveLength(12)
    expect(within(tree).getAllByText('候选问题 · 未确认')).toHaveLength(5)
    fireEvent.click(within(tree).getByRole('button', { name: '目标与读者：覆盖完整场景的展开 候选问题 · 未确认' }))
    expect(screen.getByRole('region', { name: '思路节点题目' })).toHaveTextContent('所属分支目标与读者：覆盖完整场景')
    expect(screen.getByText('已确认 0 项决定')).toBeInTheDocument()
    expect(screen.getAllByRole('textbox')).toHaveLength(2)
    expect(state).toEqual(original)
  })

  it('marks an explicitly cleared answer without restoring the old selected candidates', () => {
    const state = stageFixture('answering')
    const question = state.current_question!
    state.current_question = null
    state.history = [{ question, answer: { selected_option_ids: [], custom_text: '', source: 'user_cleared' } }]
    state.brief_edits = { [question.id]: '' }
    render(<CreationBriefEditor state={state} rootRequest="" disabled={false} onSave={vi.fn()} onDraftChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: '目标与读者 已清空' })).toBeInTheDocument()
    expect(screen.queryByText('已选择')).not.toBeInTheDocument()
    expect(screen.getByText('已确认 0 项决定')).toBeInTheDocument()
  })
})
