import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationSkillEditor from '../components/CreationSkillEditor'
import { OFFLINE_CREATION_SKILL_CATEGORIES } from '../data/creationSkillCategories'
import { useAppStore } from '../store/useAppStore'
import {
  DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
  DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
  DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
} from '../utils/creationSkills'

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://127.0.0.1:7070')
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const installedSkillRow = (id: number, title: string) => ({
  id,
  client_skill_key: `skill-${id}`,
  cloud_skill_id: null,
  source_kind: 'manual',
  source_id: `manual-${id}`,
  title,
  summary: `${title} 摘要`,
  category_id: null,
  skill_description: {
    purpose: '测试用途',
    document_types: ['测试文档'],
    problems: ['测试问题'],
    domains: [],
    deliverables: ['测试产物'],
  },
  execution_steps: [],
  common_titles: ['标题'],
  title_style: '风格',
  text_style: '行文',
  diagram_style: '图示',
  writing_guidelines: ['规则'],
  distinctive_sections: [],
  section_headings: DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
  field_examples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
  example_document: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
  status: 'saved',
  installed: true,
  published: false,
  created_at: 1,
  updated_at: 2,
})

const renderEditor = () => render(<CreationSkillEditor
  initialSkill={{
    id: 21,
    clientSkillKey: 'skill-21',
    cloudSkillId: null,
    sourceKind: 'manual',
    sourceId: 'manual-21',
    title: '技术架构创作方法',
    summary: '用于技术架构设计。',
    categoryId: null,
    skillDescription: {
      purpose: '用于把目标、约束和证据组织成技术架构设计文档。',
      documentTypes: ['技术架构设计文档'],
      problems: ['澄清系统边界与关键取舍'],
      domains: ['软件架构'],
      deliverables: ['可评审的架构设计文档'],
    },
    executionSteps: [{
      id: 'design-solution',
      title: '设计总体方案',
      objective: '把约束和证据转化为架构方案。',
      output: '总体方案与关键设计',
      agents: ['solution_design_agent'],
      skills: ['技术架构创作方法'],
      tools: ['plantuml_diagram', 'memory_search'],
    }],
    commonTitles: ['总体架构设计'],
    titleStyle: '结论先行。',
    textStyle: '清晰正式。',
    diagramStyle: '分层架构图。',
    writingGuidelines: [],
    distinctiveSections: [],
    sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
    fieldExamples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
    exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
    status: 'saved',
    installed: true,
    published: false,
    createdAt: 1,
    updatedAt: 2,
  }}
  onClose={vi.fn()}
  onSaved={vi.fn()}
/>)

const mockApis = () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input))
    if (url.pathname === '/v1/creation-skill-categories') {
      return new Response(JSON.stringify({
        data: OFFLINE_CREATION_SKILL_CATEGORIES.map(item => ({
          id: item.id,
          key: item.key,
          name: item.name,
          level: item.level,
          parent_id: item.parentId,
          sort_order: item.sortOrder,
        })),
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/api/creation/skills' && url.searchParams.get('installed') === 'true') {
      return new Response(JSON.stringify([
        installedSkillRow(21, '技术架构创作方法'),
        installedSkillRow(31, '周报写作技能'),
      ]), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response('', { status: 404 })
  }))
}

const pickerOptionLabels = () => Array.from(
  screen.getByRole('listbox', { name: '执行步骤 1 选择能力' }).querySelectorAll('[role="option"]'),
).map(node => node.textContent || '')

describe('创作技能工作流 @ 提及', () => {
  it('已提及的官方 Tool 仍出现在选择器中，且不会重复插入', async () => {
    mockApis()
    const { container } = renderEditor()

    await waitFor(() => {
      expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).toContain('@记忆搜索')
    })
    expect(Array.from(container.querySelectorAll('.mention-highlight-field__mention')).map(node => node.textContent)).toEqual(
      expect.arrayContaining(['@方案设计 Agent', '@PlantUML 画图', '@记忆搜索']),
    )
    const objective = screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement
    fireEvent.change(objective, { target: { value: `${objective.value}@` } })

    const options = pickerOptionLabels()
    expect(options.some(label => label.includes('@记忆搜索'))).toBe(true)
    expect(options.some(label => label.includes('Tool · 已提及'))).toBe(true)

    // 重复选择已提及的官方 Tool 不产生重复文本
    const mentionedOption = Array.from(
      screen.getByRole('listbox', { name: '执行步骤 1 选择能力' }).querySelectorAll('[role="option"]'),
    ).find(node => (node.textContent || '').includes('@记忆搜索')) as HTMLButtonElement
    fireEvent.click(mentionedOption)
    const after = (screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value
    expect(after.match(/@记忆搜索/g)?.length).toBe(1)
    expect(after).not.toContain('@记忆搜索@')
    expect(after).not.toContain('@记忆搜索 @记忆搜索')
  })

  it('选择器不能提及技能自身名称', async () => {
    mockApis()
    renderEditor()

    await waitFor(() => {
      expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).toContain('@')
    })
    // 自身名称不应回写为 @ 提及
    expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).not.toContain('@技术架构创作方法')

    const objective = screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement
    fireEvent.change(objective, { target: { value: `${objective.value}@` } })
    expect(pickerOptionLabels().some(label => label.includes('技术架构创作方法'))).toBe(false)

    // 直接输入自身名称也不应提供“作为 Skill 名称引用”的自定义项
    const current = screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement
    const base = current.value.replace(/@[^@]*$/, '')
    fireEvent.change(current, { target: { value: `${base}@技术架构创作方法` } })
    expect(pickerOptionLabels().some(label => label.includes('技术架构创作方法'))).toBe(false)

    // 其它已安装 Skill 仍可提及
    const other = screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement
    const otherBase = other.value.replace(/@[^@]*$/, '')
    fireEvent.change(other, { target: { value: `${otherBase}@周报` } })
    expect(pickerOptionLabels().some(label => label.includes('@周报写作技能'))).toBe(true)
  })

  it('插入提及后继续打字不会重新弹出选择器，回车也不会误选能力', async () => {
    mockApis()
    renderEditor()

    await waitFor(() => {
      expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).toContain('@记忆搜索')
    })
    const objective = screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement
    fireEvent.change(objective, { target: { value: `${objective.value}@行业调研` } })

    const agentOption = Array.from(
      screen.getByRole('listbox', { name: '执行步骤 1 选择能力' }).querySelectorAll('[role="option"]'),
    ).find(node => (node.textContent || '').includes('@行业调研 Agent')) as HTMLButtonElement
    fireEvent.click(agentOption)

    const inserted = (screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value
    expect(inserted).toContain('@行业调研 Agent ')

    // 提及之后继续写正文，选择器不应重新弹出
    fireEvent.change(objective, { target: { value: `${inserted}收集行业背景` } })
    expect(screen.queryByRole('listbox', { name: '执行步骤 1 选择能力' })).toBeNull()

    // 此时回车只应正常换行，不会插入任何能力文本
    fireEvent.keyDown(objective, { key: 'Enter', code: 'Enter' })
    expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).toBe(`${inserted}收集行业背景`)
  })

  it('输入法组合期间按回车只确认候选字，不会误插入选择器中的能力', async () => {
    mockApis()
    renderEditor()

    await waitFor(() => {
      expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).toContain('@记忆搜索')
    })
    const objective = screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement
    const queryValue = `${objective.value}@记忆`
    fireEvent.change(objective, { target: { value: queryValue } })
    expect(screen.getByRole('listbox', { name: '执行步骤 1 选择能力' })).toBeTruthy()

    fireEvent.compositionStart(objective)
    const defaultAllowed = fireEvent.keyDown(objective, {
      key: 'Enter',
      code: 'Enter',
      isComposing: true,
    })

    // 不拦截默认行为，也不插入能力文本
    expect(defaultAllowed).toBe(true)
    expect((screen.getByLabelText('执行步骤 1 执行动作') as HTMLTextAreaElement).value).toBe(queryValue)
  })
})
