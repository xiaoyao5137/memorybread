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

const analysis = {
  title: '跨部门技术沟通会文档',
  summary: '适合架构师组织跨部门技术沟通，目标是统一系统边界、技术取舍和行动项。',
  common_titles: ['技术方案沟通会材料'],
  title_style: '标题明确场景与交付目标。',
  text_style: '结论先行，取舍有据。',
  diagram_style: '使用分层架构图和关键链路图。',
  structure_pattern: ['背景与目标', '方案边界', '关键取舍', '行动项'],
  writing_guidelines: ['每个结论写明负责人和下一步。'],
  suggested_category_keywords: ['互联网', '企业服务', '架构师', '技术架构设计文档'],
  analysis_mode: 'local_model',
}

beforeEach(() => {
  useAppStore.getState().reset()
  useAppStore.getState().setApiBaseUrl('http://127.0.0.1:7070')
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('沉淀技能', () => {
  it('模型输出格式异常时不误报为模型服务不可用', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/v1/creation-skill-categories') {
        return new Response('', { status: 404 })
      }
      if (url.pathname === '/api/creation/skills/analyze') {
        return Response.json({
          ...analysis,
          section_headings: DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
          field_examples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
          example_document: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
          analysis_mode: 'heuristic_fallback',
          fallback_reason: 'invalid_model_output',
        })
      }
      if (url.pathname === '/api/creation/skills' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        return Response.json(
          { ...body, id: 29, created_at: 1, updated_at: 2 },
          { status: 201 },
        )
      }
      return new Response('', { status: 404 })
    }))

    render(<CreationSkillEditor
      source={{
        kind: 'bake_document',
        id: 'doc-invalid-json',
        title: '协作流程说明',
        content: '# 背景与目标\n说明协作问题。\n## 方案设计\n明确角色、动作与验收证据。',
        docType: '实施方案',
      }}
      onClose={vi.fn()}
      onSaved={vi.fn()}
    />)

    expect(await screen.findByText(/本地模型已完成推理，但返回格式未通过校验/)).toBeInTheDocument()
    expect(screen.queryByText(/本地模型服务暂时不可用/)).not.toBeInTheDocument()
  })

  it('展示百分比分析进度，并自动保存为未安装草稿后由用户完成保存', async () => {
    const savedBodies: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
      if (url.pathname === '/api/creation/skills/analyze') {
        await new Promise(resolve => window.setTimeout(resolve, 40))
        return new Response(JSON.stringify(analysis), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url.pathname === '/api/creation/skills' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        savedBodies.push(body)
        return new Response(JSON.stringify({ ...body, id: 17, created_at: 1, updated_at: savedBodies.length + 1 }), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.pathname === '/api/creation/skills/17' && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body))
        savedBodies.push(body)
        return new Response(JSON.stringify({ ...body, id: 17, created_at: 1, updated_at: savedBodies.length + 1 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('', { status: 404 })
    }))
    const onSaved = vi.fn()

    render(<CreationSkillEditor
      source={{
        kind: 'bake_document',
        id: 'doc-17',
        title: '研发中心跨部门技术沟通会纪要',
        content: '# 目标\n统一系统边界。\n## 方案\n讨论关键取舍与行动项。',
        docType: '技术架构设计文档',
      }}
      onClose={vi.fn()}
      onSaved={onSaved}
    />)

    expect(screen.getByRole('progressbar', { name: '本机分析进度' })).toHaveAttribute('aria-valuenow', '6')

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ status: 'draft', installed: false }))
    }, { timeout: 2500 })
    expect(screen.queryByText('把这份文档的写法提炼成可复用的创作配方；所有分析先在本机完成。')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    // 创作类目默认折叠且默认值为“私有”，不再自动建议类目，也不再展示私有提示文案
    expect(screen.queryByText('私有（仅本机可用）')).not.toBeInTheDocument()
    expect(screen.queryByText('默认私有，发布到市场需选择具体类目')).not.toBeInTheDocument()
    expect(savedBodies[0]).toMatchObject({ status: 'draft', installed: false, category_id: null })
    expect(screen.getByRole('heading', { name: '标题设计风格' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '行文设计思路' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '图片生成方式' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '话术表达风格' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '章节组织骨架' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('常用结构提炼结果')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('标题风格提炼结果')).not.toBeInTheDocument()
    const exampleDocument = (screen.getByLabelText('完整示例文档') as HTMLTextAreaElement).value
    expect(exampleDocument).toContain('共享评审空间预约流程优化方案')
    expect(exampleDocument.length).toBeGreaterThanOrEqual(1000)
    expect(exampleDocument.match(/^##\s+/gm)?.length).toBeGreaterThanOrEqual(6)
    expect(screen.getByText(/草稿已自动保存在本机/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '保存技能' }))
    await waitFor(() => {
      expect(savedBodies.some(body => body.status === 'saved' && body.installed === false)).toBe(true)
    })
  })

  it('编辑已有内容时保存修改后的技能简介，并使用级联选项卡', async () => {
    let savedBody: Record<string, any> | null = null
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
      if (url.pathname === '/api/creation/skills/21' && init?.method === 'PUT') {
        savedBody = JSON.parse(String(init.body))
        return Response.json({ ...savedBody, id: 21, created_at: 1, updated_at: 3 })
      }
      return new Response('', { status: 404 })
    }))
    const categoryId = OFFLINE_CREATION_SKILL_CATEGORIES.find(item => item.key === 'enterprise-architecture-design-doc')!.id
    const onSaved = vi.fn()

    render(<CreationSkillEditor
      initialSkill={{
        id: 21,
        clientSkillKey: 'skill-21',
        cloudSkillId: 'cloud-skill-21',
        sourceKind: 'bake_document',
        sourceId: 'doc-21',
        title: '技术架构创作方法',
        summary: '用于技术架构设计。',
        categoryId,
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
          skills: [],
          tools: ['data_search'],
        }],
        commonTitles: ['总体架构设计'],
        titleStyle: '结论先行。',
        textStyle: '清晰正式。',
        diagramStyle: '分层架构图。',
        writingGuidelines: [],
        distinctiveSections: [{
          title: '定义先行',
          description: '先说明核心对象是什么。',
          guidance: '对象首次出现时先给通俗解释，再补边界。',
          examples: ['协作工作台可以理解为任务流转的统一入口。'],
        }],
        sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
        fieldExamples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
        exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
        status: 'saved',
        installed: false,
        published: true,
        createdAt: 1,
        updatedAt: 2,
      }}
      onClose={vi.fn()}
      onSaved={onSaved}
    />)

    expect(screen.getByRole('heading', { name: '技能编辑' })).toBeInTheDocument()
    expect(screen.queryByText('沉淀自：技术架构创作方法')).not.toBeInTheDocument()
    const summaryInput = screen.getByLabelText(/技能简介/) as HTMLTextAreaElement
    expect(summaryInput).toHaveValue('用于技术架构设计。')
    expect(screen.queryByText('把这份文档的写法提炼成可复用的创作配方；所有分析先在本机完成。')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('option', { name: '企业服务', hidden: true })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /发布|开放到市场|更新市场版本|下架市场/ })).not.toBeInTheDocument()
    expect(screen.queryByText('发布边界')).not.toBeInTheDocument()
    expect(screen.getByDisplayValue('定义先行')).toBeInTheDocument()
    const screenshotOption = screen.getByRole('checkbox', { name: '创作时保留网页证据截图到文档上' })
    expect(screenshotOption).toBeChecked()
    fireEvent.click(screenshotOption)
    expect(screenshotOption).not.toBeChecked()
    expect(screen.queryByText(/默认开启；关闭后仍优先通过 AX\/DOM 精确读取/)).not.toBeInTheDocument()
    fireEvent.change(summaryInput, { target: { value: '把算力数据整理成可复核的分析文档。' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => {
      expect(savedBody).toMatchObject({
        summary: '把算力数据整理成可复核的分析文档。',
        skill_description: {
          purpose: '把算力数据整理成可复核的分析文档。',
          problems: ['把算力数据整理成可复核的分析文档。'],
        },
      })
      expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({
        summary: '把算力数据整理成可复核的分析文档。',
      }))
    })
    // 特色亮点区块默认折叠，按钮在折叠状态下仍存在于 DOM 中
    expect(screen.getByRole('button', { name: '删除特色亮点 1', hidden: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '添加特色章节', hidden: true })).toBeInTheDocument()
  })

  it('生成示例时把当前配方作为 Skill 交给创作 Agent 即时生成', async () => {
    let agentBody: Record<string, unknown> | null = null
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/v1/creation-skill-categories') {
        return new Response('', { status: 404 })
      }
      if (url.pathname === '/api/creation/agent/run') {
        agentBody = JSON.parse(String(init?.body))
        const events = [
          { type: 'run.queued', summary: '已接收本轮指令' },
          { type: 'document.delta', actor: { id: 'document_writer_agent' }, data: { content: '# 虚构主题示例\n\n' } },
          { type: 'document.delta', actor: { id: 'document_writer_agent' }, data: { content: '创作 Agent 生成的正文。' } },
          { type: 'run.completed', status: 'completed', data: { document: '# 虚构主题示例\n\n创作 Agent 生成的正文。' } },
        ]
        const sseText = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')
        return new Response(sseText, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
      }
      return new Response('', { status: 404 })
    }))

    render(<CreationSkillEditor
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
          domains: [],
          deliverables: ['可评审的架构设计文档'],
        },
        executionSteps: [{
          id: 'design-solution',
          title: '设计总体方案',
          objective: '把约束和证据转化为架构方案。',
          output: '',
          agents: [],
          skills: [],
          tools: [],
        }],
        commonTitles: ['总体架构设计'],
        titleStyle: '结论先行。',
        textStyle: '清晰正式。',
        diagramStyle: '',
        writingGuidelines: [],
        distinctiveSections: [],
        sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
        fieldExamples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
        exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
        status: 'saved',
        installed: false,
        published: false,
        createdAt: 1,
        updatedAt: 2,
      }}
      onClose={vi.fn()}
      onSaved={vi.fn()}
    />)

    fireEvent.click(screen.getByRole('button', { name: '生成示例', hidden: true }))
    await waitFor(() => {
      expect((screen.getByLabelText('完整示例文档') as HTMLTextAreaElement).value).toContain('创作 Agent 生成的正文')
    })

    expect(agentBody).not.toBeNull()
    const selectedSkills = agentBody!.selected_skills as Array<Record<string, unknown>>
    expect(selectedSkills).toHaveLength(1)
    expect(selectedSkills[0].title).toBe('技术架构创作方法')
    expect(selectedSkills[0].writingDesign).toBe('清晰正式。')
    // 旧示例不应回传给 Agent，避免照抄而不是生成新主题
    expect(selectedSkills[0].exampleDocument).toBe('')
    expect(agentBody!.enable_rag).toBe(false)
    expect(agentBody!.enable_web_search).toBe(false)
    expect(agentBody!.model_mode).toBe('local')
    expect(String(agentBody!.user_prompt)).toContain('技术架构创作方法')
    expect(screen.getByText(/示例已由创作 Agent 按当前配方生成/)).toBeInTheDocument()
  })
})
