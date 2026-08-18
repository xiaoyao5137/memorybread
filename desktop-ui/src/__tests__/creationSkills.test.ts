import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  analyzeCreationSkill,
  agentSkillZipBytes,
  buildCreationSkillInstruction,
  categoryPathFor,
  codexSkillPackageFiles,
  creationSkillCategoryOptions,
  DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
  DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
  DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
  fetchCreationSkillCategories,
  importCodexSkillPackage,
  importAgentSkillZip,
  listLocalCreationSkills,
  marketCreationSkillToLocalInput,
  matchCreationSkills,
  matchCreationSkillsForExecution,
  normalizeCreationSkillTitle,
  parseCodexSkillMarkdown,
  publishCreationSkill,
  resolveCreationSkillDependencies,
  resolveExecutionSkills,
  searchCreationSkillMarket,
  skillFileText,
  suggestCreationSkillCategory,
  type CreationSkillCategory,
  type LocalCreationSkill,
  type MatchedCreationSkill,
} from '../utils/creationSkills'
import { OFFLINE_CREATION_SKILL_CATEGORIES } from '../data/creationSkillCategories'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Codex 技能包兼容', () => {
  it('解析标准 SKILL.md 元数据', () => {
    const metadata = parseCodexSkillMarkdown(`---
name: meeting-actions
description: >
  Extract decisions and action items.
  Use when reviewing meeting notes.
---

# Workflow

Read the notes, then list owners and due dates.`)

    expect(metadata).toEqual({
      name: 'meeting-actions',
      description: 'Extract decisions and action items. Use when reviewing meeting notes.',
      instructions: '# Workflow\n\nRead the notes, then list owners and due dates.',
    })
  })

  it('导入完整目录并保留脚本与引用文件', async () => {
    const skillMarkdown = new File([
      '---\nname: meeting-actions\ndescription: Extract decisions and action items from meeting notes.\n---\n\n# Workflow\n\nRead references/checklist.md.',
    ], 'SKILL.md', { type: 'text/markdown' })
    const reference = new File(['# Checklist\n\n- Decisions\n- Owners'], 'checklist.md', { type: 'text/markdown' })
    Object.defineProperty(skillMarkdown, 'webkitRelativePath', { value: 'meeting-actions/SKILL.md' })
    Object.defineProperty(reference, 'webkitRelativePath', { value: 'meeting-actions/references/checklist.md' })

    const imported = await importCodexSkillPackage([skillMarkdown, reference])

    expect(imported).toMatchObject({
      sourceKind: 'imported',
      sourceId: 'meeting-actions',
      title: 'meeting-actions',
      installed: true,
      published: false,
    })
    expect(imported.packageFiles?.map(file => file.path)).toEqual([
      'SKILL.md',
      'references/checklist.md',
    ])
    expect(skillFileText(imported.packageFiles![1])).toContain('Decisions')

    const instruction = buildCreationSkillInstruction([{
      skill: { ...imported, id: 10, createdAt: 1, updatedAt: 1 },
      reason: 'mentioned',
      score: 1_000,
    }])
    expect(instruction).toContain('[技能文件：SKILL.md]')
    expect(instruction).toContain('[技能文件：references/checklist.md]')
    expect(instruction).toContain('- Decisions')
  })

  it('为旧技能生成可复用的 SKILL.md', () => {
    const files = codexSkillPackageFiles({
      ...localSkill,
      id: 9,
      title: '技术架构设计文档写作法',
    })
    const markdown = skillFileText(files[0])

    expect(files.map(file => file.path)).toEqual([
      'SKILL.md',
      'references/memorybread-creation.json',
      'references/example.md',
    ])
    expect(markdown).toContain('name: local-skill-1')
    expect(markdown).toContain('description:')
    expect(markdown).toContain('帮助架构师把目标、约束和证据组织成可评审的架构设计文档')
    expect(markdown).toContain('用于创作：技术架构设计文档')
    expect(markdown).toContain('## 能力描述')
    expect(markdown).toContain('## 执行工作流')
    expect(markdown).toContain('Agent：solution_design_agent')
    expect(markdown).toContain('Tool：plantuml_diagram')
    expect(markdown).not.toContain('## 章节组织骨架')
    expect(markdown).toContain('## References')
    expect(markdown).toContain('references/memorybread-creation.json')
    expect(skillFileText(files[1])).toContain('"title": "定义先行的概念建立"')
  })

  it('用 ZIP 导出再导入时保留标准目录', async () => {
    const files = codexSkillPackageFiles({ ...localSkill, id: 9 })
    const archive = new File([agentSkillZipBytes(localSkill.title, files)], 'portable-skill.zip', {
      type: 'application/zip',
    })

    const imported = await importAgentSkillZip(archive)

    expect(imported.sourceKind).toBe('imported')
    expect(imported.packageFiles?.map(file => file.path).sort()).toEqual(files.map(file => file.path).sort())
    expect(skillFileText(imported.packageFiles![0])).toContain('name: local-skill-1')
    expect(imported.skillDescription).toEqual(localSkill.skillDescription)
    expect(imported.textStyle).toBe(localSkill.textStyle)
    expect(imported.exampleDocument).toBe(localSkill.exampleDocument)
  })

  it('拒绝目录名与 Skill name 不一致的包', async () => {
    const file = new File([
      '---\nname: expected-name\ndescription: A complete description for the reusable workflow.\n---\n',
    ], 'SKILL.md', { type: 'text/markdown' })
    Object.defineProperty(file, 'webkitRelativePath', { value: 'different-name/SKILL.md' })

    await expect(importCodexSkillPackage([file])).rejects.toThrow('name 必须与文件夹名称一致')
  })
})

const localSkill: Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'> = {
  clientSkillKey: 'local-skill-1',
  cloudSkillId: null,
  sourceKind: 'creation_history',
  sourceId: 'history-42',
  title: '技术架构设计文档写作法',
  summary: '帮助架构师稳定产出可评审的架构设计文档。',
  categoryId: 'leaf-category',
  skillDescription: {
    purpose: '帮助架构师把目标、约束和证据组织成可评审的架构设计文档。',
    documentTypes: ['技术架构设计文档'],
    problems: ['澄清系统边界、关键取舍和实施路径'],
    domains: ['软件架构'],
    deliverables: ['包含架构、链路、风险和验证方式的完整文档'],
  },
  executionSteps: [{
    id: 'design-solution',
    title: '设计总体方案',
    objective: '把目标、约束和证据转化为结构化架构方案。',
    output: '总体方案与关键设计',
    agents: ['solution_design_agent'],
    skills: [],
    tools: ['plantuml_diagram'],
  }],
  commonTitles: ['总体架构设计', '关键链路设计'],
  titleStyle: '结论先行，标题带明确对象。',
  textStyle: '短段落配合约束、方案和取舍。',
  diagramStyle: '统一配色并标注边界与数据流向。',
  writingGuidelines: ['每个决策写明原因', '敏感数据使用占位符'],
  distinctiveSections: [{
    title: '定义先行的概念建立',
    description: '先解释核心对象，再展开方案。',
    guidance: '对象首次出现时先给通俗解释，再补职责边界。',
    examples: ['协作工作台可以理解为连接任务、角色与结果证据的统一入口。'],
  }],
  sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
  fieldExamples: {
    commonTitles: [...DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.commonTitles],
    titleStyle: [...DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.titleStyle],
    textStyle: [...DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.textStyle],
    diagramStyle: [...DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.diagramStyle],
    writingGuidelines: [...DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.writingGuidelines],
  },
  exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
  status: 'saved',
  installed: false,
  published: false,
}

describe('技能云端发布边界', () => {
  it('只上传结构化 Skill，不上传来源标识或原文', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body))
      expect(body).toMatchObject({
        client_skill_key: 'local-skill-1',
        title: localSkill.title,
        category_id: 'leaf-category',
        published: true,
      })
      expect(body.content.common_titles).toEqual(localSkill.commonTitles)
      expect(body.content.distinctive_sections).toEqual(localSkill.distinctiveSections)
      expect(body.content.skill_description).toEqual({
        purpose: localSkill.skillDescription.purpose,
        document_types: localSkill.skillDescription.documentTypes,
        problems: localSkill.skillDescription.problems,
        domains: localSkill.skillDescription.domains,
        deliverables: localSkill.skillDescription.deliverables,
      })
      expect(body.content.execution_steps).toEqual(localSkill.executionSteps)
      expect(body.content).not.toHaveProperty('structure_pattern')
      expect(body.content.section_headings).not.toHaveProperty('structure_pattern')
      expect(body.content.field_examples).not.toHaveProperty('structure_pattern')
      expect(body).not.toHaveProperty('source_id')
      expect(body).not.toHaveProperty('source_kind')
      expect(body).not.toHaveProperty('document_content')
      expect(JSON.stringify(body)).not.toContain('history-42')
      return new Response(JSON.stringify({ data: { id: 'cloud-1', published: true } }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await publishCreationSkill('https://api.example.test', 'token', localSkill, true)

    expect(result).toEqual({ id: 'cloud-1', published: true })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('把第四级类目还原为完整行业到文档类型路径', () => {
    const categories: CreationSkillCategory[] = [
      { id: '1', key: 'internet', name: '互联网', level: 1, sortOrder: 10 },
      { id: '2', key: 'retail', name: '电商零售', level: 2, parentId: '1', sortOrder: 10 },
      { id: '3', key: 'architect', name: '架构师', level: 3, parentId: '2', sortOrder: 10 },
      { id: '4', key: 'architecture', name: '技术架构设计文档', level: 4, parentId: '3', sortOrder: 10 },
    ]

    expect(categoryPathFor(categories, '4').map(item => item.name)).toEqual([
      '互联网',
      '电商零售',
      '架构师',
      '技术架构设计文档',
    ])
  })

  it('把接口的分级列表还原为父子相邻的下拉选项', () => {
    const options = creationSkillCategoryOptions([
      { id: 'leaf-b', key: 'leaf-b', name: '接口设计文档', level: 4, parentId: 'role-b', sortOrder: 20 },
      { id: 'industry-b', key: 'industry-b', name: '金融', level: 1, sortOrder: 20 },
      { id: 'segment-a', key: 'segment-a', name: '企业服务', level: 2, parentId: 'industry-a', sortOrder: 20 },
      { id: 'industry-a', key: 'industry-a', name: '互联网', level: 1, sortOrder: 10 },
      { id: 'role-b', key: 'role-b', name: '软件工程师', level: 3, parentId: 'segment-a', sortOrder: 20 },
      { id: 'leaf-a', key: 'leaf-a', name: '技术设计文档', level: 4, parentId: 'role-b', sortOrder: 10 },
    ])

    expect(options.map(({ id, depth }) => [id, depth])).toEqual([
      ['industry-a', 0],
      ['segment-a', 1],
      ['role-b', 2],
      ['leaf-a', 3],
      ['leaf-b', 3],
      ['industry-b', 0],
    ])
  })

  it('不会把未发布且没有云端记录的草稿提交到服务端', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      publishCreationSkill('https://api.example.test', 'token', localSkill, false),
    ).rejects.toThrow('未发布的本地技能草稿不会上传')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('按关键词搜索市场并映射为可安装的本地只读副本', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      expect(url.pathname).toBe('/v1/creation-skills')
      expect(url.searchParams.get('q')).toBe('架构')
      return Response.json({
        data: {
          items: [{
            id: '01900000-0000-7000-8000-000000000021',
            title: '通用架构评审文档',
            summary: '适合架构评审场景。',
            category_id: 'leaf-category',
            category_path: [
              { id: '1', key: 'internet', name: '互联网', level: 1 },
              { id: '4', key: 'architecture', name: '技术架构设计文档', level: 4 },
            ],
            content: {
              common_titles: localSkill.commonTitles,
              title_style: localSkill.titleStyle,
              text_style: localSkill.textStyle,
              diagram_style: localSkill.diagramStyle,
              structure_pattern: ['旧章节结构不应继续进入客户端模型'],
              writing_guidelines: localSkill.writingGuidelines,
              section_headings: {
                common_titles: localSkill.sectionHeadings.commonTitles,
                title_style: localSkill.sectionHeadings.titleStyle,
                text_style: localSkill.sectionHeadings.textStyle,
                diagram_style: localSkill.sectionHeadings.diagramStyle,
                structure_pattern: '旧章节结构标题',
                writing_guidelines: localSkill.sectionHeadings.writingGuidelines,
              },
              field_examples: {
                common_titles: localSkill.fieldExamples.commonTitles,
                title_style: localSkill.fieldExamples.titleStyle,
                text_style: localSkill.fieldExamples.textStyle,
                diagram_style: localSkill.fieldExamples.diagramStyle,
                structure_pattern: ['旧章节结构示例'],
                writing_guidelines: localSkill.fieldExamples.writingGuidelines,
              },
              example_document: localSkill.exampleDocument,
            },
            author: { id: 'author-1', nickname: '面包师小麦' },
            published: true,
            published_at: '2026-07-23T08:00:00Z',
            updated_at: '2026-07-23T08:00:00Z',
          }],
          total: 1,
          limit: 18,
          offset: 0,
        },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const page = await searchCreationSkillMarket('https://api.example.test', {
      query: '架构',
      limit: 18,
    })
    const local = marketCreationSkillToLocalInput(page.items[0])

    expect(page.items[0]).toMatchObject({
      title: '通用架构评审文档',
      author: { nickname: '面包师小麦' },
    })
    expect(local).toMatchObject({
      sourceKind: 'market',
      sourceId: '01900000-0000-7000-8000-000000000021',
      cloudSkillId: '01900000-0000-7000-8000-000000000021',
      installed: true,
      published: false,
    })
  })

  it('市场请求悬挂时会超时退出加载状态', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const abort = () => reject(new DOMException('The operation was aborted.', 'AbortError'))
        if (init?.signal?.aborted) abort()
        else init?.signal?.addEventListener('abort', abort, { once: true })
      }))
    vi.stubGlobal('fetch', fetchMock)

    const pending = searchCreationSkillMarket('https://api.example.test')
    const rejection = expect(pending).rejects.toThrow('技能市场请求超时，请稍后重试')
    await vi.advanceTimersByTimeAsync(8_000)
    await rejection
    vi.useRealTimers()

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal)
  })
})

describe('技能本地生成与类目容错', () => {
  it('离线类目覆盖主要行业且每个节点都保持完整四级关系', () => {
    const counts = [1, 2, 3, 4].map(level => OFFLINE_CREATION_SKILL_CATEGORIES.filter(item => item.level === level).length)
    const ids = new Set(OFFLINE_CREATION_SKILL_CATEGORIES.map(item => item.id))
    const keys = new Set(OFFLINE_CREATION_SKILL_CATEGORIES.map(item => item.key))
    const clinicalPath = categoryPathFor(
      OFFLINE_CREATION_SKILL_CATEGORIES,
      OFFLINE_CREATION_SKILL_CATEGORIES.find(item => item.key === 'healthcare-hospital-clinic-clinician-clinical-pathway')?.id,
    )

    expect(counts).toEqual([20, 94, 199, 375])
    expect(ids.size).toBe(OFFLINE_CREATION_SKILL_CATEGORIES.length)
    expect(keys.size).toBe(OFFLINE_CREATION_SKILL_CATEGORIES.length)
    expect(clinicalPath.map(item => item.name)).toEqual([
      '医疗健康与生命科学',
      '医院与基层医疗',
      '临床医师',
      '临床诊疗路径',
    ])
  })

  it('分析接口未升级时仍自动生成完整可编辑草稿', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })))

    const analysis = await analyzeCreationSkill('http://127.0.0.1:7070', {
      kind: 'bake_document',
      id: 'doc-1',
      title: '订单中心总体架构设计',
      docType: '技术架构设计文档',
      content: '# 背景与目标\n建设订单中心。\n## 总体架构\n说明系统边界和关键链路。\n## 演进计划\n分阶段实施。',
    })

    expect(analysis.analysisMode).toBe('client_heuristic_fallback')
    expect(analysis.fallbackReason).toBe('analysis_request_failed')
    expect(analysis.title).toContain('技术架构设计')
    expect(analysis).not.toHaveProperty('structurePattern')
    expect(analysis.titleStyle).not.toBe('')
    expect(analysis.diagramStyle).not.toBe('')
    expect(analysis.commonTitles.join('')).not.toContain('订单中心')
    expect(analysis.exampleDocument).not.toContain('订单中心')
    expect(analysis.exampleDocument.length).toBeGreaterThanOrEqual(1000)
    expect(analysis.exampleDocument.match(/^##\s+/gm)?.length).toBeGreaterThanOrEqual(6)
    expect(analysis.textStyle.length).toBeGreaterThanOrEqual(400)
    expect(analysis.diagramStyle.length).toBeGreaterThanOrEqual(400)
    expect(analysis.writingGuidelines.join('').length).toBeGreaterThanOrEqual(400)
  })

  it('分析服务短暂不可用时自动重试并使用服务端结果', async () => {
    vi.useFakeTimers()
    const successfulResult = {
        title: '技术架构设计文档',
        summary: '适合梳理系统边界、关键取舍和验证路径。',
        common_titles: ['总体架构与关键取舍'],
        title_style: '使用短名词结构。',
        text_style: '先界定边界，再给出方案和验证方式。',
        diagram_style: '使用 PlantUML 表达已在正文定义的组件关系。',
        structure_pattern: ['背景与目标', '总体方案', '风险与验证'],
        writing_guidelines: ['结论后紧接依据和适用边界。'],
        section_headings: DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
        field_examples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
        example_document: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
        analysis_mode: 'local_model',
    }
    let createCount = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills/analyze/jobs' && init?.method === 'POST') {
        createCount += 1
        return Response.json({ job_id: `skill-job-${createCount}`, status: 'pending' })
      }
      if (url.pathname.endsWith('/skill-job-1')) {
        return Response.json({
          id: 'skill-job-1',
          status: 'failed',
          error_code: 'CREATION_SKILL_ANALYZER_UNAVAILABLE',
          error: '本地技能分析服务不可用',
        })
      }
      return Response.json({ id: 'skill-job-2', status: 'succeeded', result: successfulResult })
    })
    vi.stubGlobal('fetch', fetchMock)

    const pending = analyzeCreationSkill('http://127.0.0.1:7070', {
      kind: 'bake_document',
      id: 'doc-retry',
      title: '订单中心总体架构设计',
      docType: '技术架构设计文档',
      content: '# 背景与目标\n建设订单中心。\n## 总体架构\n说明系统边界和关键链路。\n## 演进计划\n分阶段实施。',
    })
    await vi.runAllTimersAsync()
    const analysis = await pending
    vi.useRealTimers()

    expect(fetchMock).toHaveBeenCalledTimes(4)
    expect(createCount).toBe(2)
    expect(analysis.analysisMode).toBe('local_model')
    expect(analysis.fallbackReason).toBeUndefined()
  })

  it('修复旧分析结果中的短示例、重复占位标题和过短写作指引', async () => {
    vi.useFakeTimers()
    const result = {
      title: '技术方案文档',
      summary: '适合需要梳理技术方案的协作场景。',
      common_titles: [{ level: '一级标题', pattern: '# [核心主题] + OS/整体技术方案' }],
      title_style: '标题采用中等长度短句',
      text_style: '先写目标，再写方案。',
      diagram_style: '默认不生成图片。',
      structure_pattern: [{ role: '先定义核心对象，再说明核心目标' }],
      writing_guidelines: ['使用正式语气。'],
      section_headings: {
        common_titles: '标题设计风格',
        title_style: '标题设计风格',
        text_style: '行文设计思路',
        diagram_style: '图片生成方式',
        structure_pattern: '章节组织骨架',
        writing_guidelines: '话术表达风格',
      },
      field_examples: {
        common_titles: ['目标对象 目标对象 目标对象', '从业务视角看，目标对象', '核心目标'],
        title_style: ['目标对象 目标对象 目标对象'],
        text_style: ['先写目标，再写方案。'],
        diagram_style: ['默认不生成图片。'],
        structure_pattern: ['目标与范围 → 总体方案 → 验证与验收'],
        writing_guidelines: ['需要明确适用边界。'],
      },
      example_document: '# 示例方案\n\n## 摘要\n\n这是一份过短示例。\n\n## 方案\n\n内容不足。',
      distinctive_sections: [{
        title: '定义先行',
        description: '先解释核心对象的角色与边界，再进入方案。',
        guidance: '对象首次出现时先给通俗解释，再补职责边界。',
        examples: ['协作工作台可以理解为任务流转的统一入口。'],
      }],
      suggested_category_keywords: ['技术设计文档'],
      analysis_mode: 'local_model',
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      return url.pathname === '/api/creation/skills/analyze/jobs'
        ? Response.json({ job_id: 'skill-job-old-analysis', status: 'pending' })
        : Response.json({ id: 'skill-job-old-analysis', status: 'succeeded', result })
    }))

    const pending = analyzeCreationSkill('http://127.0.0.1:7070', {
      kind: 'bake_document',
      id: 'doc-old-analysis',
      title: 'TieAgent OS 整体技术方案',
      docType: '技术文档',
      content: [
        '# TieAgent OS 整体技术方案',
        '## 从业务视角看，TieAgent',
        '### TieAgent OS 定义',
        '### 核心目标',
        '## runtime: TieAgent',
      ].join('\n\n'),
    })
    await vi.runAllTimersAsync()
    const analysis = await pending
    vi.useRealTimers()

    expect(analysis.analysisMode).toBe('local_model')
    expect(analysis.fieldExamples.commonTitles.join(' ')).not.toContain('目标对象 目标对象')
    expect(analysis.fieldExamples.commonTitles).toContain('从业务视角看，协作工作台的角色与边界')
    expect(analysis.commonTitles[0]).toContain('一级标题：采用“')
    expect(analysis).not.toHaveProperty('structurePattern')
    expect(analysis.sectionHeadings).not.toHaveProperty('structurePattern')
    expect(analysis.fieldExamples).not.toHaveProperty('structurePattern')
    expect(JSON.stringify(analysis)).not.toContain('[object Object]')
    expect(analysis.distinctiveSections?.[0].title).toBe('定义先行')
    expect(analysis.commonTitles.length).toBeGreaterThanOrEqual(4)
    expect(analysis.textStyle.length).toBeGreaterThanOrEqual(400)
    expect(analysis.diagramStyle.length).toBeGreaterThanOrEqual(400)
    expect(analysis.writingGuidelines.join('').length).toBeGreaterThanOrEqual(400)
    expect(analysis.exampleDocument.length).toBeGreaterThanOrEqual(1000)
    expect(analysis.exampleDocument.match(/^##\s+/gm)?.length).toBeGreaterThanOrEqual(6)
  })

  it('合并同一来源的并发分析，避免开发模式重复占用本地模型', async () => {
    vi.useFakeTimers()
    let resolveFetch: ((response: Response) => void) | undefined
    const result = {
      title: '知识交接方案文档',
      summary: '适合需要规范知识交接流程的协作场景。',
      common_titles: ['知识交接优化方案'],
      title_style: '标题明确场景和交付目标。',
      text_style: '先说明边界，再按阶段说明动作。',
      diagram_style: '使用泳道图展示角色和交接节点。',
      structure_pattern: ['背景与目标', '方案设计', '验证与验收'],
      writing_guidelines: ['每个阶段写明输入、输出和完成标准。'],
      section_headings: {
        common_titles: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.commonTitles,
        title_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.titleStyle,
        text_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.textStyle,
        diagram_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.diagramStyle,
        structure_pattern: '旧章节结构标题',
        writing_guidelines: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.writingGuidelines,
      },
      field_examples: {
        common_titles: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.commonTitles,
        title_style: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.titleStyle,
        text_style: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.textStyle,
        diagram_style: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.diagramStyle,
        structure_pattern: ['旧章节结构示例'],
        writing_guidelines: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.writingGuidelines,
      },
      example_document: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
      suggested_category_keywords: ['互联网', '企业服务', '软件工程师', '技术设计文档'],
      analysis_mode: 'local_model',
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills/analyze/jobs') {
        return new Promise<Response>(resolve => {
          resolveFetch = resolve
        })
      }
      return Promise.resolve(Response.json({
        id: 'skill-job-concurrent',
        status: 'succeeded',
        result,
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const source = {
      kind: 'bake_document' as const,
      id: 'doc-concurrent',
      title: '知识交接优化方案',
      docType: '技术方案',
      content: '# 背景与目标\n统一知识交接方式，明确责任边界。\n## 方案设计\n按阶段说明输入、输出和验收标准。',
    }

    const first = analyzeCreationSkill('http://127.0.0.1:7070', source)
    const second = analyzeCreationSkill('http://127.0.0.1:7070', source)

    expect(fetchMock).toHaveBeenCalledOnce()
    resolveFetch?.(Response.json({
      job_id: 'skill-job-concurrent',
      status: 'pending',
    }))
    await vi.runAllTimersAsync()

    const [firstResult, secondResult] = await Promise.all([first, second])
    vi.useRealTimers()
    expect(firstResult.analysisMode).toBe('local_model')
    expect(secondResult).toEqual(firstResult)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls.filter(([input]) => (
      new URL(String(input)).pathname === '/api/creation/skills/analyze/jobs'
    ))).toHaveLength(1)
  })

  it('云端类目接口失败时使用与服务端同 ID 的四级内置类目', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })))

    const categories = await fetchCreationSkillCategories('http://127.0.0.1:8080')

    expect(categories).toHaveLength(OFFLINE_CREATION_SKILL_CATEGORIES.length)
    expect(new Set(categories.map(item => item.level))).toEqual(new Set([1, 2, 3, 4]))
    const architecture = categories.find(item => item.key === 'enterprise-architecture-design-doc')
    expect(categoryPathFor(categories, architecture?.id).map(item => item.name)).toEqual([
      '互联网',
      '企业服务',
      '架构师',
      '技术架构设计文档',
    ])
  })

  it('根据总结、正文和文档类型自动选中完整四级类目', () => {
    const analysis = {
      title: '订单中心架构技能',
      summary: '用于企业系统的关键链路架构评审。',
      skillDescription: localSkill.skillDescription,
      executionSteps: localSkill.executionSteps,
      commonTitles: ['订单中心总体架构设计'],
      titleStyle: '结论先行。',
      textStyle: '正式。',
      diagramStyle: '架构图。',
      writingGuidelines: [],
      sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
      fieldExamples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
      exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
      suggestedCategoryKeywords: ['互联网', '企业服务', '架构师', '技术架构设计文档'],
      analysisMode: 'local_model',
    }
    const leaf = suggestCreationSkillCategory(OFFLINE_CREATION_SKILL_CATEGORIES, analysis, {
      kind: 'creation_history',
      id: '42',
      title: '订单中心架构设计',
      docType: '技术架构设计文档',
      content: '企业服务平台的系统边界与关键链路。',
    })

    expect(leaf?.key).toBe('enterprise-architecture-design-doc')
  })

  it('能为新增行业内容推荐对应的四级类目', () => {
    const leaf = suggestCreationSkillCategory(OFFLINE_CREATION_SKILL_CATEGORIES, {
      title: '临床诊疗路径技能',
      summary: '用于医院临床医师整理标准化诊疗流程。',
      skillDescription: {
        ...localSkill.skillDescription,
        documentTypes: ['临床诊疗路径'],
        domains: ['医疗健康'],
      },
      executionSteps: localSkill.executionSteps,
      commonTitles: ['呼吸科临床诊疗路径'],
      titleStyle: '规范、明确。',
      textStyle: '按临床阶段说明。',
      diagramStyle: '使用诊疗流程图。',
      writingGuidelines: [],
      sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
      fieldExamples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
      exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
      suggestedCategoryKeywords: ['医疗健康与生命科学', '医院与基层医疗', '临床医师', '临床诊疗路径'],
      analysisMode: 'local_model',
    })

    expect(leaf?.key).toBe('healthcare-hospital-clinic-clinician-clinical-pathway')
  })

  it('把具体部门名称归纳为可复用的场景标题', () => {
    const title = normalizeCreationSkillTitle('电商与商业化技术协作会议纪要撰写指南', {
      kind: 'bake_document',
      id: 'doc-2',
      title: '研发中心跨部门技术沟通会纪要',
      docType: '技术设计文档',
      content: '架构师与产品、研发和运维共同确认技术方案与系统边界。',
    })

    expect(title).toBe('跨部门技术沟通会文档')
    expect(title).not.toContain('研发中心')
  })

  it('不会被正文中偶然出现的复盘案例误导技能用途', () => {
    const title = normalizeCreationSkillTitle('技术架构设计文档', {
      kind: 'bake_document',
      id: 'doc-with-review-case',
      title: '通用运行平台整体技术方案',
      docType: '技术文档',
      content: '# 背景\n正文引用了一段案例复盘和年度总结，但文档本身仍是技术方案。',
    })

    expect(title).toBe('运行平台整体技术方案')
    expect(title).not.toBe('项目复盘总结文档')
  })

  it('只匹配已保存且已安装的 Skill，并把简介和类目写入创作指令', () => {
    const installedSkill: LocalCreationSkill = {
      ...localSkill,
      id: 7,
      categoryId: OFFLINE_CREATION_SKILL_CATEGORIES.find(item => item.key === 'enterprise-architecture-design-doc')!.id,
      title: '跨部门技术沟通会文档',
      summary: '适合架构师组织跨部门技术评审，目标是统一方案边界、关键取舍和行动项。',
      installed: true,
      createdAt: 1,
      updatedAt: 2,
    }
    const matches = matchCreationSkills('请使用@跨部门技术沟通会文档 帮我写一份跨部门架构评审会材料', [
      installedSkill,
      { ...installedSkill, id: 8, title: '未安装版本', installed: false },
      { ...installedSkill, id: 9, title: '草稿版本', status: 'draft' },
    ])
    const instruction = buildCreationSkillInstruction(matches)

    expect(matches.map(item => item.skill.id)).toEqual([7])
    expect(matches[0].reason).toBe('mentioned')
    expect(instruction).toContain('适用场景与目标：')
    expect(instruction).toContain('互联网 / 企业服务 / 架构师 / 技术架构设计文档')
    expect(instruction).toContain('Agent Skills 目录内容')
    expect(instruction).not.toContain('[技能文件：references/example.md]')
    expect(instruction).toContain('references/memorybread-creation.json')
    expect(instruction).toContain('execution_steps 是唯一的执行流程和一级章节白名单')
    expect(instruction).toContain('不得跳步、调换步骤或合并不同步骤的数据与表格')
    expect(instruction).toContain('不得添加 Skill 未声明的结论、重点进展、风险阻塞、后续计划')
    expect(instruction).toContain('标题设计风格：总体架构设计；关键链路设计')
    expect(instruction).toContain(`行文设计思路：${installedSkill.textStyle}`)
    expect(instruction).toContain('"diagram_style"')
    expect(instruction).toContain('"writing_guidelines"')
    expect(instruction).toContain('"title": "定义先行的概念建立"')
    expect(instruction).toContain('"guidance": "对象首次出现时先给通俗解释')
    expect(instruction).not.toContain('章节组织骨架：')
    expect(instruction).not.toContain('章节推进示例：')
    expect(instruction).not.toContain('标题如何传递重点')
  })

  it('显式 @ 是引入技能的唯一方式，名称相近的模板不会被带入', () => {
    const primary: LocalCreationSkill = {
      ...localSkill,
      id: 81,
      title: 'GPU成本优化周报模板',
      summary: '按四个指定步骤生成 GPU 成本优化周报。',
      installed: true,
      createdAt: 1,
      updatedAt: 3,
    }
    const genericWeekly: LocalCreationSkill = {
      ...localSkill,
      id: 82,
      title: '通用工作周报模板',
      summary: '生成包含进展、风险和下周计划的工作周报。',
      installed: true,
      createdAt: 1,
      updatedAt: 2,
    }
    const stageUpdate: LocalCreationSkill = {
      ...localSkill,
      id: 83,
      title: '项目阶段汇报模板',
      summary: '生成项目阶段进展汇报。',
      installed: true,
      createdAt: 1,
      updatedAt: 1,
    }

    const matches = matchCreationSkills(
      '请使用@GPU成本优化周报模板 创作下本周的周报',
      [primary, genericWeekly, stageUpdate],
    )

    expect(matches.map(match => match.skill.id)).toEqual([81])
    expect(matches[0].reason).toBe('mentioned')
  })

  it('自动推荐已下线：输入含标题重合和汇报意图也不自动召回', () => {
    const weeklyTemplate: LocalCreationSkill = {
      ...localSkill,
      id: 86,
      title: 'GPU成本优化周报模板',
      summary: '用于每周更新大模型性能成本优化周报。',
      skillDescription: {
        purpose: '用于每周更新大模型性能成本优化周报。',
        documentTypes: ['周报', '进度总结报告'],
        problems: ['GPU指标数据分散，需要按统一口径整理为关键指标表'],
        domains: ['算力运营', '成本优化'],
        deliverables: ['包含本周进度总结与关键指标表的结构化周报'],
      },
      installed: true,
      createdAt: 1,
      updatedAt: 2,
    }

    // 即使输入与技能标题高度重合、且带明确汇报意图，没有 @ 也不召回。
    const withoutMention = matchCreationSkills('写一份本周的GPU成本优化周报', [weeklyTemplate])
    expect(withoutMention).toHaveLength(0)

    // 显式 @ 仍然是唯一入口。
    const withMention = matchCreationSkills('请使用@GPU成本优化周报模板 写本周周报', [weeklyTemplate])
    expect(withMention).toHaveLength(1)
    expect(withMention[0]).toMatchObject({ reason: 'mentioned', skill: { id: 86 } })
  })

  it('把执行步骤引用的已安装 Skill 一并加入本轮调用', () => {
    const primary: LocalCreationSkill = {
      ...localSkill,
      id: 72,
      installed: true,
      createdAt: 1,
      updatedAt: 2,
      executionSteps: [{
        ...localSkill.executionSteps[0],
        skills: ['evidence-brief'],
      }],
    }
    const dependency: LocalCreationSkill = {
      ...localSkill,
      id: 73,
      clientSkillKey: 'evidence-brief',
      title: '证据简报 Skill',
      installed: true,
      createdAt: 1,
      updatedAt: 2,
    }

    const resolved = resolveCreationSkillDependencies(
      [primary],
      [primary, dependency, { ...dependency, id: 74, clientSkillKey: 'not-installed', installed: false }],
    )

    expect(resolved.map(skill => skill.id)).toEqual([72, 73])
  })

  it('支持按来源文档查询关联 Skill 并读取安装状态', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toContain('source_kind=bake_document')
      expect(String(input)).toContain('source_id=doc-42')
      return new Response(JSON.stringify([{
        id: 3,
        client_skill_key: 'doc-skill',
        source_kind: 'bake_document',
        source_id: 'doc-42',
        title: '项目复盘总结文档',
        summary: '适合项目结束后的跨团队复盘。',
        common_titles: ['项目复盘'],
        title_style: '结论先行',
        text_style: '事实清晰',
        diagram_style: '时间线',
        structure_pattern: ['目标', '结果', '行动项'],
        writing_guidelines: [],
        section_headings: {
          common_titles: '这类文档标题通常怎么命名',
          title_style: '标题如何传递重点',
          text_style: '正文怎样组织和表达',
          diagram_style: '图示怎样服务于内容',
          structure_pattern: '从开篇到结论的章节骨架',
          writing_guidelines: '保持这份风格的关键约束',
        },
        field_examples: {
          common_titles: ['项目复盘'],
          title_style: ['项目复盘：结论与行动'],
          text_style: ['先陈述结果，再解释原因。'],
          diagram_style: ['用时间线展示阶段变化。'],
          structure_pattern: ['目标 → 结果 → 行动项'],
          writing_guidelines: ['需要说明的是，结论只覆盖已确认范围。'],
        },
        example_document: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
        status: 'saved',
        installed: true,
        published: false,
        created_at: 1,
        updated_at: 2,
      }]), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchMock)

    const skills = await listLocalCreationSkills('http://127.0.0.1:7070', {
      sourceKind: 'bake_document',
      sourceId: 'doc-42',
    })

    expect(skills).toHaveLength(1)
    expect(skills[0]).toMatchObject({ status: 'saved', installed: true })
    expect(skills[0].commonTitles).toEqual(['结论先行'])
    expect(skills[0].fieldExamples.commonTitles).toContain('项目复盘')
    expect(skills[0].sectionHeadings.commonTitles).toBe('标题设计风格')
    expect(skills[0].packageFiles?.map(file => file.path)).toEqual([
      'SKILL.md',
      'references/memorybread-creation.json',
      'references/example.md',
    ])
  })

  it('读取旧本地记录时把 Python 字典字符串还原为可读规则', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json([{
      id: 16,
      client_skill_key: 'doc-skill-object-array',
      source_kind: 'bake_document',
      source_id: '20',
      title: '项目复盘总结文档',
      summary: '适合技术方案写作。',
      common_titles: ["{'level': '一级标题', 'pattern': '# [核心主题] + OS/整体技术方案'}"],
      title_style: '标题规则',
      text_style: '行文规则',
      diagram_style: '默认不生成图片。',
      structure_pattern: ["{'role': '先定义核心对象，再说明核心目标'}"],
      writing_guidelines: [],
      distinctive_sections: [],
      section_headings: { ...Object.fromEntries(Object.entries(DEFAULT_CREATION_SKILL_SECTION_HEADINGS).map(([key, value]) => [
        key.replace(/[A-Z]/g, match => `_${match.toLowerCase()}`),
        value,
      ])) },
      field_examples: {
        common_titles: ["{'level': '一级标题', 'pattern': '# [核心主题] + OS/整体技术方案'}"],
        title_style: ["{'level': '一级标题', 'pattern': '# [核心主题] + OS/整体技术方案'}"],
        text_style: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.textStyle,
        diagram_style: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.diagramStyle,
        structure_pattern: ['旧章节结构示例'],
        writing_guidelines: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.writingGuidelines,
      },
      example_document: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
      status: 'saved',
      installed: false,
      published: false,
      created_at: 1,
      updated_at: 2,
    }])))

    const [skill] = await listLocalCreationSkills('http://127.0.0.1:7070')

    expect(skill.commonTitles[0]).toBe('一级标题：采用“# [核心主题] + OS/整体技术方案”的标题骨架')
    expect(skill).not.toHaveProperty('structurePattern')
    expect(skill.sectionHeadings).not.toHaveProperty('structurePattern')
    expect(skill.fieldExamples).not.toHaveProperty('structurePattern')
    expect(skill.fieldExamples.commonTitles[0]).not.toContain("{'level':")
    expect(skill.title).toBe('运行平台整体技术方案')
    expect(skill.textStyle.length).toBeGreaterThanOrEqual(400)
    expect(skill.textStyle).toContain('交付前逐节检查')
    expect(skill.diagramStyle.length).toBeGreaterThanOrEqual(400)
    expect(skill.writingGuidelines.join('').length).toBeGreaterThanOrEqual(400)
    expect(skill.skillDescription.purpose).toBe('适合技术方案写作。')
    expect(skill.skillDescription.documentTypes).toEqual(['运行平台整体技术方案'])
    expect(skill.executionSteps.map(step => step.id)).toEqual([
      'collect-context',
      'design-solution',
      'design-chapters',
      'draft-document',
      'review-delivery',
    ])
    expect(skill.executionSteps[0].tools).toEqual(['memory_search'])
    expect(skill.executionSteps.every(step => step.retainWebpageScreenshot === false)).toBe(true)
  })

  it('读取新版保存结果时保留空示例文档和用户修改的仿写示例', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json([{
      id: 17,
      client_skill_key: 'manual-skill-17',
      cloud_skill_id: null,
      source_kind: 'manual',
      source_id: 'manual-17',
      title: '方案写作技能',
      summary: '用于整理方案文档。',
      category_id: null,
      common_titles: [],
      title_style: '',
      text_style: '',
      diagram_style: '',
      writing_guidelines: [],
      distinctive_sections: [],
      section_headings: {
        common_titles: '标题设计风格',
        title_style: '标题设计风格',
        text_style: '行文设计思路',
        diagram_style: '图片生成方式',
        writing_guidelines: '话术表达风格',
      },
      field_examples: {
        common_titles: ['用户修改后的标题仿写示例'],
        title_style: ['用户修改后的标题仿写示例'],
        text_style: ['用户修改后的行文仿写示例'],
        diagram_style: [],
        writing_guidelines: [],
      },
      example_document: '',
      skill_description: {
        purpose: '用于整理方案文档。',
        document_types: ['方案文档'],
        problems: ['整理方案'],
        domains: [],
        deliverables: ['方案文档'],
      },
      execution_steps: [{
        id: 'draft',
        title: '起草方案',
        objective: '按要求完成方案。',
        output: '',
        agents: [],
        skills: [],
        tools: [],
      }],
      status: 'saved',
      installed: false,
      published: false,
      created_at: 1,
      updated_at: 2,
    }])))

    const [skill] = await listLocalCreationSkills('http://127.0.0.1:7070')

    expect(skill.fieldExamples.commonTitles).toEqual(['用户修改后的标题仿写示例'])
    expect(skill.fieldExamples.textStyle).toEqual(['用户修改后的行文仿写示例'])
    expect(skill.fieldExamples.diagramStyle).toEqual([])
    expect(skill.fieldExamples.writingGuidelines).toEqual([])
    expect(skill.exampleDocument).toBe('')
  })
})

describe('提交后的执行时技能解析', () => {
  const gpuWeeklyTemplate: LocalCreationSkill = {
    ...localSkill,
    id: 91,
    title: 'GPU成本优化周报模板',
    summary: '用于每周更新大模型性能成本优化周报。',
    skillDescription: {
      purpose: '用于每周更新大模型性能成本优化周报。',
      documentTypes: ['周报', '进度总结报告'],
      problems: ['GPU指标数据分散，需要按统一口径整理为关键指标表'],
      domains: ['算力运营', '成本优化'],
      deliverables: ['包含本周进度总结与关键指标表的结构化周报'],
    },
    commonTitles: ['GPU成本优化周报'],
    installed: true,
    createdAt: 1,
    updatedAt: 2,
  }

  it('用户原句没有 @ 时，执行时确定性召回仍命中周报模板', () => {
    const matches = matchCreationSkillsForExecution(
      '请生成下本周GPU成本优化的周报',
      [gpuWeeklyTemplate],
    )

    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({ reason: 'automatic', skill: { id: 91 } })
  })

  it('没有汇报意图的技术问答由模型决策不召回总结类模板', async () => {
    // 召回与否由模型依据 Skill 自描述披露决策，不做枚举式意图门控。
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      skill_ids: [],
      source: 'model',
      reasoning: '纯问答，没有写周报的创作诉求',
    })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '帮我分析GPU成本优化的关键指标应该怎么定',
      skills: [gpuWeeklyTemplate],
    })

    expect(resolution.source).toBe('model')
    expect(resolution.matches).toHaveLength(0)
  })

  const archPlanTemplate: LocalCreationSkill = {
    ...localSkill,
    id: 92,
    title: '架构方案模板',
    summary: '用于撰写系统架构方案文档。',
    skillDescription: {
      purpose: '把系统架构设计整理成可评审的方案文档。',
      documentTypes: ['架构方案'],
      problems: ['架构取舍需要结构化表达'],
      domains: ['软件架构'],
      deliverables: ['完整的架构方案文档'],
    },
    commonTitles: ['架构方案文档'],
    installed: true,
    createdAt: 1,
    updatedAt: 2,
  }

  it('纯画图请求由模型判定不在模板声明用途内，不召回方案类模板', async () => {
    // 模板自述用途是写方案文档、未声明支持画图，模型据此返回空数组。
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      skill_ids: [],
      source: 'model',
      reasoning: '画架构图不在模板声明的用途内',
    })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '参考架构方案模板画一张我们系统的架构图',
      skills: [archPlanTemplate],
    })

    expect(resolution.source).toBe('model')
    expect(resolution.matches).toHaveLength(0)
  })

  it('明确的方案创作意图下模型正常召回方案类模板', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      skill_ids: [92],
      source: 'model',
      reasoning: '明确要写技术方案，与模板用途一致',
    })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '用架构方案模板写一份推理服务的技术方案',
      skills: [archPlanTemplate],
    })

    expect(resolution.source).toBe('model')
    expect(resolution.matches).toHaveLength(1)
    expect(resolution.matches[0]).toMatchObject({ skill: { id: 92 } })
  })

  it('显式 @ 优先于模型路由，且不请求模型路由接口', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '请使用@GPU成本优化周报模板 写本周周报',
      skills: [gpuWeeklyTemplate],
    })

    expect(resolution.source).toBe('mentioned')
    expect(resolution.matches.map(match => match.skill.id)).toEqual([91])
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('模型的明确决策（含选中具体技能）覆写确定性结果', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      skill_ids: [91],
      source: 'model',
      reasoning: '输入与周报模板的产物一致',
    })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '请生成下本周GPU成本优化的周报',
      skills: [gpuWeeklyTemplate],
    })

    expect(resolution.source).toBe('model')
    expect(resolution.matches.map(match => match.skill.id)).toEqual([91])
    expect(resolution.reasoning).toBe('输入与周报模板的产物一致')
  })

  it('模型主动判定无合适技能时尊重空召回', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      skill_ids: [],
      source: 'model',
      reasoning: '输入与现有技能都不相关',
    })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '请生成下本周GPU成本优化的周报',
      skills: [gpuWeeklyTemplate],
    })

    expect(resolution.source).toBe('model')
    expect(resolution.matches).toHaveLength(0)
  })

  it('模型降级空召回（source=fallback）不能丢掉确定性命中的技能', async () => {
    // sidecar 推理失败时返回 HTTP 200 + 空召回，这是原始缺陷的触发路径。
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({
      skill_ids: [],
      source: 'fallback',
      reasoning: '',
    })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '请生成下本周GPU成本优化的周报',
      skills: [gpuWeeklyTemplate],
    })

    expect(resolution.source).toBe('deterministic')
    expect(resolution.matches.map(match => match.skill.id)).toEqual([91])
  })

  it('模型路由接口报错时回退确定性匹配，不阻断创作', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 502 })))

    const resolution = await resolveExecutionSkills({
      apiBaseUrl: 'http://127.0.0.1:7070',
      prompt: '请生成下本周GPU成本优化的周报',
      skills: [gpuWeeklyTemplate],
    })

    expect(resolution.source).toBe('deterministic')
    expect(resolution.matches.map(match => match.skill.id)).toEqual([91])
  })
})
