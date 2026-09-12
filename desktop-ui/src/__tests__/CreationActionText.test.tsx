import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentExecutionTrace } from '../components/CreationPanel'
import { creationActionText, CREATION_AGENT_ACTIONS } from '../utils/creationActionText'
import { CREATION_SKILL_AGENT_OPTIONS } from '../utils/creationSkills'
import type { CreationAgentEvent } from '../store/useAppStore'

describe('执行过程动作文案', () => {
  it.each(CREATION_SKILL_AGENT_OPTIONS)('$label 使用动作描述', ({ label }) => {
    expect(CREATION_AGENT_ACTIONS[label]).toBeTruthy()
    expect(creationActionText(`${label} 已完成`)).toBe(`${CREATION_AGENT_ACTIONS[label]} 已完成`)
  })

  it.each(Object.entries(CREATION_AGENT_ACTIONS))('%s 的历史名称转换为动作', (name, action) => {
    expect(creationActionText(name)).toBe(action)
  })

  it('保留已有动作、Skill 标题和普通内容', () => {
    const title = '生成文档内容、质量审校、梳理 Agent 产品的市场机会'
    expect(creationActionText(title)).toBe(title)
    expect(creationActionText('方案设计Agent')).toBe('设计落地方案')
  })

  it('历史阶段与动作行展示动作，执行者及原始摘要留在明细', () => {
    const event = (sequence: number, type: string, summary: string, data = {}) => ({
      schema_version: 'creation.agent.v1', event_id: `event-${sequence}`, session_id: 'session',
      run_id: 'run', sequence, timestamp: sequence * 1000, type, status: 'completed', summary,
      actor: { kind: 'agent', id: 'solution_design_agent', name: '方案设计 Agent' },
      environment_patch: {}, data,
    } as CreationAgentEvent)
    render(<AgentExecutionTrace events={[
      event(1, 'phase.started', '方案设计 Agent', { phase_id: 'step:solution_design_agent', phase_title: '方案设计 Agent' }),
      event(2, 'agent.completed', '方案设计 Agent 已完成'),
      event(3, 'phase.completed', '方案设计 Agent', { phase_id: 'step:solution_design_agent' }),
      event(4, 'run.completed', '完成'),
    ]} onOpenReferences={() => {}} apiBaseUrl="http://localhost:7070" />)
    expect(screen.getByText('1. 设计落地方案')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开全部' }))
    expect(screen.getByText('设计落地方案 已完成')).toBeInTheDocument()
    expect(screen.getByText('方案设计 Agent 已完成')).toBeInTheDocument()
    expect(document.querySelector('.creation-agent-event__capability')).toHaveTextContent('方案设计 Agent')
    document.querySelectorAll('.creation-agent-event__headline, .creation-trace-phase__title').forEach(title => {
      expect(title).not.toHaveTextContent('Agent')
    })
  })
})
