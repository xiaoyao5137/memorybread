import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentExecutionTrace } from '../components/CreationPanel'
import type { CreationAgentEvent } from '../store/useAppStore'

const event = (run: string, sequence: number, type: string, title: string) => ({
  schema_version: 'creation.agent.v1', event_id: `${run}-${sequence}`, session_id: 'session',
  run_id: run, sequence, timestamp: sequence * 1000, type,
  status: type.endsWith('completed') ? 'completed' : type.endsWith('failed') ? 'failed' : 'running',
  summary: title, actor: { kind: 'agent', id: 'writer', name: '文档撰写 Agent' },
  environment_patch: {}, data: { phase_id: 'write', phase_title: title, stage: 'generation' },
} as CreationAgentEvent)

const unfinished = (run: string) => [
  event(run, 1, 'phase.started', `${run}阶段`),
  event(run, 2, 'agent.started', `${run}动作`),
  event(run, 3, 'thinking.started', '深度思考中：生成文档内容'),
]

const show = (events: CreationAgentEvent[]) => render(
  <AgentExecutionTrace events={events} onOpenReferences={() => {}} apiBaseUrl="http://localhost:7070" />,
)

describe('创作轨迹轮次隔离', () => {
  it.each(['run.failed', 'run.cancelled', 'run.completed'])('%s 后重试只显示新一轮正在运行', (terminal) => {
    show([...unfinished('旧轮'), event('旧轮', 4, terminal, '旧轮结束'), ...unfinished('新轮')])
    const oldPhase = screen.getByText('1. 旧轮阶段').closest('.creation-trace-phase') as HTMLElement
    const newPhase = screen.getByText('2. 新轮阶段').closest('.creation-trace-phase') as HTMLElement
    expect(oldPhase).not.toHaveClass('is-running')
    expect(newPhase).toHaveClass('is-running')
    expect(within(oldPhase).getByText(terminal === 'run.failed' ? '未完成' : terminal === 'run.cancelled' ? '已结束' : '已完成')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    expect(oldPhase.querySelector('.creation-trace-thinking.is-running')).toBeNull()
    expect(oldPhase.querySelector('.creation-agent-event.is-running')).toBeNull()
    expect(newPhase.querySelector('.creation-trace-thinking.is-running')).toBeTruthy()
    expect(newPhase.querySelector('.creation-agent-event.is-running')).toBeTruthy()
  })

  it('后续失败不能把之前完成的阶段标成未完成', () => {
    show([
      ...unfinished('成功轮'), event('成功轮', 4, 'phase.completed', '成功轮阶段'),
      event('成功轮', 5, 'run.completed', '完成'),
      ...unfinished('失败轮'), event('失败轮', 4, 'run.failed', '失败'),
    ])
    const phase = screen.getByText('1. 成功轮阶段').closest('.creation-trace-phase') as HTMLElement
    expect(phase).toHaveClass('is-completed')
    expect(within(phase).queryByText('未完成')).toBeNull()
  })

  it('新一轮同名 Agent 完成不能覆盖旧一轮失败动作', () => {
    show([
      ...unfinished('旧轮'), event('旧轮', 4, 'run.failed', '失败'),
      ...unfinished('新轮'), event('新轮', 4, 'agent.completed', '新轮动作完成'),
    ])
    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    expect(screen.getByText('旧轮动作').closest('.creation-agent-event')).toHaveClass('is-failed')
  })
})
