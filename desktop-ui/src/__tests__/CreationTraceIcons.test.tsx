import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentExecutionTrace } from '../components/CreationPanel'
import { CreationThinkingIcon, CreationTraceIcon } from '../components/CreationTraceIcon'
import type { CreationAgentEvent } from '../store/useAppStore'
import { CREATION_SKILL_AGENT_OPTIONS } from '../utils/creationSkills'
import { CREATION_TOOL_DEFINITIONS } from '../utils/creationTools'

const toolEvent = (
  sequence: number,
  id: string,
  summary: string,
  status: 'running' | 'completed' | 'warning' | 'failed' = 'completed',
): CreationAgentEvent => ({
  schema_version: 'creation.agent.v1',
  event_id: `tool-${sequence}`,
  session_id: 'trace-icons-session',
  run_id: 'trace-icons-run',
  sequence,
  timestamp: sequence * 1000,
  type: status === 'failed' ? 'tool.failed' : status === 'running' ? 'tool.started' : 'tool.completed',
  status,
  actor: { kind: 'tool', id, name: `${id} Tool` },
  summary,
  environment_patch: {},
  data: {},
})

const thinkingEvent = (
  sequence: number,
  type: 'thinking.started' | 'thinking.completed',
): CreationAgentEvent => ({
  schema_version: 'creation.agent.v1',
  event_id: `thinking-${sequence}`,
  session_id: 'trace-icons-session',
  run_id: 'trace-icons-run',
  sequence,
  timestamp: sequence * 1000,
  type,
  status: type === 'thinking.started' ? 'running' : 'completed',
  actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
  summary: type === 'thinking.started' ? '深度思考中：理解本轮要求' : '深度思考完成：理解本轮要求',
  environment_patch: {},
  data: { stage: 'intent', reasoning: '已确定执行路径' },
})

const mainEvent = (
  sequence: number,
  type: string,
  summary: string,
  extras: Partial<CreationAgentEvent> = {},
): CreationAgentEvent => ({
  schema_version: 'creation.agent.v1',
  event_id: `main-${sequence}`,
  session_id: 'trace-icons-session',
  run_id: 'trace-icons-run',
  sequence,
  timestamp: sequence * 1000,
  type,
  status: 'completed',
  actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
  summary,
  environment_patch: {},
  data: {},
  ...extras,
})

const show = (events: CreationAgentEvent[]) => render(
  <AgentExecutionTrace events={events} onOpenReferences={() => {}} apiBaseUrl="http://localhost:7070" />,
)

const stepFor = (summary: string): HTMLElement => {
  const step = screen.getByText(summary).closest('.creation-agent-event')
  expect(step).not.toBeNull()
  return step as HTMLElement
}

describe('创作 Agent 执行轨迹图标', () => {
  it('按工具 ID 区分本地记忆、数据、网页，并为未知工具保留通用图标', () => {
    show([
      toolEvent(1, 'memory_search', '检索本地记忆'),
      toolEvent(2, 'data_search', '查询业务数据'),
      toolEvent(3, 'webpage_scrape', '读取网页内容'),
      toolEvent(4, 'custom_tool', '运行自定义工具'),
    ])

    expect(stepFor('检索本地记忆').querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'memory_search')
    expect(stepFor('查询业务数据').querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'data_search')
    expect(stepFor('读取网页内容').querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'webpage_scrape')
    expect(stepFor('运行自定义工具').querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'tool')

    const images = ['检索本地记忆', '查询业务数据', '读取网页内容', '运行自定义工具']
      .map(summary => stepFor(summary).querySelector<HTMLImageElement>('.creation-agent-event__icon img'))
    expect(images.every(image => image?.getAttribute('src')?.includes('.png'))).toBe(true)
    expect(new Set(images.map(image => image?.getAttribute('src'))).size).toBe(4)
    expect(stepFor('检索本地记忆').querySelector('.creation-agent-event__icon svg')).toBeNull()
  })

  it('每个已知工具和专用 Agent 都使用独立生成的图片', () => {
    const actors = [
      ...CREATION_TOOL_DEFINITIONS.map(({ id }) => ({ kind: 'tool' as const, id })),
      ...CREATION_SKILL_AGENT_OPTIONS.map(({ id }) => ({ kind: 'agent' as const, id })),
    ]
    const { container } = render(
      <div>{actors.map(actor => (
        <CreationTraceIcon
          key={actor.id}
          event={{ ...toolEvent(1, actor.id, actor.id), actor: { ...actor, name: actor.id } }}
          status="completed"
        />
      ))}</div>,
    )
    const icons = [...container.querySelectorAll('.creation-agent-event__icon')]
    const sources = icons.map(icon => icon.querySelector('img')?.getAttribute('src'))
    expect(icons.map(icon => icon.getAttribute('data-trace-icon'))).toEqual(actors.map(actor => actor.id))
    expect(sources.every(source => source?.includes('.png'))).toBe(true)
    expect(new Set(sources).size).toBe(actors.length)
    expect(container.querySelector('.creation-agent-event__icon svg')).toBeNull()
  })

  it('每个已注册工具跨事件、运行与状态始终使用自己的同一张图片', () => {
    const statuses = ['running', 'completed', 'warning', 'failed', 'cancelled'] as const
    const { container } = render(<div>{CREATION_TOOL_DEFINITIONS.flatMap(({ id }, toolIndex) => (
      statuses.map((status, index) => (
        <CreationTraceIcon
          key={`${id}-${status}`}
          event={{
            ...toolEvent(toolIndex * statuses.length + index + 1, id, `${id} ${status}`),
            status,
            run_id: `run-${index}`,
          }}
          status={status}
        />
      ))
    ))}</div>)
    const icons = [...container.querySelectorAll('.creation-agent-event__icon')]
    CREATION_TOOL_DEFINITIONS.forEach(({ id }, toolIndex) => {
      const matching = icons.slice(toolIndex * statuses.length, (toolIndex + 1) * statuses.length)
      expect(matching.map(icon => icon.getAttribute('data-trace-icon'))).toEqual(statuses.map(() => id))
      expect(new Set(matching.map(icon => icon.querySelector('img')?.getAttribute('src'))).size).toBe(1)
    })
  })

  it.each([
    ['running', '正在检索'],
    ['completed', '已完成检索'],
    ['warning', '检索有警告'],
    ['failed', '检索失败'],
  ] as const)('%s 状态仍保留工具类别，并单独显示状态标记', (status, summary) => {
    show([toolEvent(1, 'memory_search', summary, status)])

    const step = stepFor(summary)
    expect(step.querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'memory_search')
    if (status === 'running') {
      expect(step.querySelector('.creation-agent-event__activity')).not.toBeNull()
    } else {
      expect(step.querySelector('.creation-agent-event__status'))
        .toHaveAttribute('data-status', status)
    }
  })

  it('思考进行中和完成后都保留独立的思考图标', () => {
    const { rerender } = show([thinkingEvent(1, 'thinking.started')])
    const running = screen.getByText('深度思考中：理解本轮要求').closest('.creation-trace-thinking')
    expect(running).toHaveClass('is-running')
    expect(running?.querySelector('.creation-trace-thinking__icon'))
      .toHaveAttribute('data-trace-icon', 'thinking')
    const runningImage = running?.querySelector('.creation-trace-thinking__icon img')?.getAttribute('src')
    expect(runningImage).toContain('.png')

    rerender(
      <AgentExecutionTrace
        events={[thinkingEvent(1, 'thinking.started'), thinkingEvent(2, 'thinking.completed')]}
        onOpenReferences={() => {}}
        apiBaseUrl="http://localhost:7070"
      />,
    )
    const completed = screen.getByText('深度思考').closest('.creation-trace-thinking')
    expect(completed).not.toHaveClass('is-running')
    expect(completed?.querySelector('.creation-trace-thinking__icon'))
      .toHaveAttribute('data-trace-icon', 'thinking')
    expect(completed?.querySelector('.creation-trace-thinking__icon img')?.getAttribute('src'))
      .toBe(runningImage)
  })

  it('所有思考阶段及未知阶段共用同一图标，阶段只影响文字', () => {
    const stages = ['intent', 'routing', 'generation', 'planning', 'future_stage'] as const
    const events = stages.flatMap((stage, index) => [
      { ...thinkingEvent(index * 2 + 1, 'thinking.started'), summary: '深度思考中', data: { stage } },
      { ...thinkingEvent(index * 2 + 2, 'thinking.completed'), summary: '深度思考完成', data: { stage } },
    ])
    const { container } = show(events)
    const icons = [...container.querySelectorAll('.creation-trace-thinking__icon')]
    expect(icons.map(icon => icon.getAttribute('data-trace-icon')))
      .toEqual(stages.map(() => 'thinking'))
    expect(new Set(icons.map(icon => icon.querySelector('img')?.getAttribute('src'))).size).toBe(1)
  })

  it('主 Agent 连续事件增加新动作或保存进度后图标不变', () => {
    const goal = mainEvent(3, 'goal.updated', '已建立创作目标与验收条件')
    const checkpoint = mainEvent(2, 'operation.checkpoint', '已保存操作进度')
    const { container, rerender } = show([
      mainEvent(1, 'run.started', '创作 Agent 已接管目标'),
      checkpoint,
      goal,
    ])
    expect(container.querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'agent')
    const image = container.querySelector('.creation-agent-event__icon img')?.getAttribute('src')

    rerender(
      <AgentExecutionTrace
        events={[mainEvent(1, 'run.resumed', '从未完成操作恢复'), checkpoint]}
        onOpenReferences={() => {}}
        apiBaseUrl="http://localhost:7070"
      />,
    )
    expect(container.querySelector('.creation-agent-event__icon'))
      .toHaveAttribute('data-trace-icon', 'agent')
    expect(container.querySelector('.creation-agent-event__icon img')?.getAttribute('src')).toBe(image)
  })

  it('主 Agent 的目标、恢复、路由、规划和交付共用固定图标', () => {
    const actors = [
      mainEvent(0, 'goal.updated', '已设定目标'),
      mainEvent(1, 'run.resumed', '已恢复执行'),
      mainEvent(2, 'agent.completed', '模型已选择能力', { data: { routing_decision: { tools: [] } } }),
      mainEvent(3, 'agent.completed', '已规划步骤', { environment_patch: { plan: ['步骤'] } }),
      mainEvent(4, 'agent.completed', '已规划步骤', { data: { action: 'plan' } }),
      mainEvent(5, 'agent.completed', '任意历史事件'),
      mainEvent(6, 'delivery.checked', '已核对交付'),
    ]
    const { container } = render(<div>{actors.map(event => (
      <CreationTraceIcon key={event.event_id} event={event} status="completed" />
    ))}</div>)
    const icons = [...container.querySelectorAll('.creation-agent-event__icon')]
    expect(icons.map(icon => icon.getAttribute('data-trace-icon')))
      .toEqual(actors.map(() => 'agent'))
    expect(new Set(icons.map(icon => icon.querySelector('img')?.getAttribute('src'))).size).toBe(1)
  })

  it('思考、Agent、Skill 与 Tool 各有固定图片，不因动作变化', () => {
    const stages = ['intent', 'routing', 'planning', 'generation'] as const
    const mainActions = [
      mainEvent(1, 'goal.updated', '目标'),
      mainEvent(2, 'run.resumed', '恢复'),
      mainEvent(3, 'agent.completed', '规划', { data: { action: 'plan' } }),
      mainEvent(4, 'delivery.checked', '交付'),
      mainEvent(8, 'intent.pending', '理解'),
      mainEvent(9, 'harness.decision', '决定'),
      mainEvent(10, 'agent.completed', '路由', { data: { routing_decision: { tools: [] } } }),
    ]
    const fallbacks = [
      mainEvent(5, 'custom.event', '自定义 Agent', {
        actor: { kind: 'agent', id: 'custom_agent', name: '自定义 Agent' },
      }),
      mainEvent(6, 'skill.completed', '自定义 Skill', {
        actor: { kind: 'skill', id: 'custom_skill', name: '自定义 Skill' },
      }),
      toolEvent(7, 'custom_tool', '自定义 Tool'),
    ]
    const { container } = render(<div>
      {stages.map(stage => <CreationThinkingIcon key={stage} />)}
      {[...mainActions, ...fallbacks].map(event => (
        <CreationTraceIcon key={event.event_id} event={event} status="completed" />
      ))}
    </div>)
    const icons = [...container.querySelectorAll<HTMLElement>('[data-trace-icon]')]
    const sources = icons.map(icon => icon.querySelector('img')?.getAttribute('src'))
    expect(icons).toHaveLength(14)
    expect(sources.every(source => source?.includes('.png'))).toBe(true)
    expect(new Set(sources).size).toBe(4)
  })

  it('思考图标始终使用同一张透明图片', () => {
    const { container } = render(<CreationThinkingIcon />)
    expect(container.querySelector('.creation-trace-thinking__icon'))
      .toHaveAttribute('data-trace-icon', 'thinking')
  })
})
