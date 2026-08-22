import { describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import BakeKnowledgeTab from '../components/bake/BakeKnowledgeTab'
import BakeTemplatesTab from '../components/bake/BakeTemplatesTab'
import BakeSopTab from '../components/bake/BakeSopTab'
import BakeRichTextEditor from '../components/bake/BakeRichTextEditor'
import type { ArticleTemplate, BakeKnowledgeItem, SopCandidate } from '../types'
import {
  DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
  DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
  DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
  type LocalCreationSkill,
} from '../utils/creationSkills'

const noop = vi.fn()

const template: ArticleTemplate = {
  id: 'tpl-1',
  title: '周报模板',
  docType: 'weekly_report',
  status: 'enabled',
  tags: ['周报'],
  applicableTasks: ['creation'],
  sourceMemoryIds: ['m-1'],
  sourceCaptureIds: ['c-1'],
  sourceEpisodeIds: ['m-1'],
  linkedKnowledgeIds: ['k-1'],
  sections: [
    { title: '背景', keywords: ['背景'] },
    { title: '进展', keywords: ['进展'] },
  ],
  stylePhrases: ['整体看', '先结论后展开'],
  replacementRules: [{ from: '综上所述', to: '整体看' }],
  promptHint: '先总结再细化',
  usageCount: 3,
  reviewStatus: 'confirmed',
  matchScore: 0.98,
  matchLevel: 'high',
  createdAt: '2026-08-11 00:35:45',
  createdAtMs: new Date(2026, 7, 11, 0, 35, 45).getTime(),
  updatedAt: '2026-08-11 14:18:36',
  updatedAtMs: new Date(2026, 7, 11, 14, 18, 36).getTime(),
}

const draftTemplate: ArticleTemplate = {
  ...template,
  id: 'tpl-2',
  title: '月报模板',
  status: 'pending_review',
  usageCount: 5,
}

const knowledge: BakeKnowledgeItem = {
  id: 'knowledge-1',
  captureId: 'c-1',
  sourceCaptureIds: ['c-1'],
  sourceTimelineId: 'm-1',
  summary: '本地优先知识',
  overview: '用户数据优先在本地处理',
  details: '',
  detailedContent: '这是一条详细知识。',
  entities: ['MemoryBread', '本地优先'],
  category: 'bake_knowledge',
  importance: 8,
  occurrenceCount: 4,
  status: 'active',
  reviewStatus: 'confirmed',
  matchScore: 0.96,
  matchLevel: 'high',
  createdAt: '2026-07-23 10:00',
  createdAtMs: 0,
  updatedAt: '2026-07-23 10:00',
  updatedAtMs: 0,
}

const sop: SopCandidate = {
  id: 'sop-1',
  sourceCaptureId: 'c-1',
  sourceTitle: '启动失败排查',
  triggerKeywords: ['启动失败', 'health'],
  confidence: 'high',
  extractedProblem: '服务无法启动',
  steps: ['检查 /health', '检查端口', '查看日志'],
  linkedKnowledgeIds: ['101', '202'],
  linkedKnowledgeSummaries: [
    { id: '101', summary: '排查服务健康检查失败' },
    { id: '202', summary: '启动端口冲突的处理步骤' },
  ],
  status: 'confirmed',
}

const relatedSkill: LocalCreationSkill = {
  id: 2,
  clientSkillKey: 'skill-cross-team-tech-meeting',
  cloudSkillId: null,
  sourceKind: 'bake_document',
  sourceId: template.id,
  title: '跨部门技术沟通会文档',
  summary: '用于跨部门技术沟通、阶段复盘与规划。',
  categoryId: '11401',
  skillDescription: {
    purpose: '用于整理跨部门技术沟通中的事实、判断和行动项。',
    documentTypes: ['跨部门技术沟通会文档'],
    problems: ['统一技术背景、阶段结论和后续责任'],
    domains: ['技术协作'],
    deliverables: ['包含结论与行动项的会议文档'],
  },
  executionSteps: [{
    id: 'draft-document',
    title: '撰写会议文档',
    objective: '把事实与结论组织成完整文档。',
    output: '可继续编辑的 Markdown 文档',
    agents: ['document_writer_agent'],
    skills: [],
    tools: [],
  }],
  commonTitles: ['跨部门技术沟通会'],
  titleStyle: '标题概括目标',
  textStyle: '结论先行',
  diagramStyle: '简洁流程图',
  writingGuidelines: ['明确负责人'],
  sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
  fieldExamples: DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
  exampleDocument: DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
  status: 'saved',
  installed: true,
  published: false,
  createdAt: 1_720_000_000_000,
  updatedAt: 1_720_000_000_000,
}

describe('Bake 详情展示优化', () => {
  it('富文本编辑框保留标题、粗体和列表格式为 Markdown', () => {
    const onChange = vi.fn()
    render(<BakeRichTextEditor value="" onChange={onChange} ariaLabel="测试文档内容" />)
    const editor = screen.getByRole('textbox', { name: '测试文档内容' })
    editor.innerHTML = '<h2>结论</h2><p><strong>订单</strong>保持增长</p><ul><li>继续观察</li></ul>'
    fireEvent.input(editor)

    expect(onChange).toHaveBeenLastCalledWith('## 结论\n\n**订单**保持增长\n\n- 继续观察')
  })

  it('富文本工具栏可切换正文、标题、粗体、斜体和两种列表', async () => {
    const user = userEvent.setup()
    const runAction = async (value: string, action: string, expected: string) => {
      const onChange = vi.fn()
      const { unmount } = render(
        <BakeRichTextEditor value={value} onChange={onChange} ariaLabel={`测试${action}`} />,
      )
      const editor = screen.getByRole('textbox', { name: `测试${action}` })
      editor.focus()
      const block = editor.querySelector('p, h2, li')
      const text = block
        ? document.createTreeWalker(block, NodeFilter.SHOW_TEXT).nextNode()
        : null
      expect(text).toBeTruthy()
      const range = document.createRange()
      range.selectNodeContents(text!)
      const selection = window.getSelection()!
      selection.removeAllRanges()
      selection.addRange(range)
      await user.click(screen.getByRole('button', { name: action }))
      expect(onChange).toHaveBeenLastCalledWith(expected)
      unmount()
    }

    await runAction('第一项', '加粗', '**第一项**')
    await runAction('第一项', '斜体', '*第一项*')
    await runAction('第一项', '标题', '## 第一项')
    await runAction('## 第一项', '正文', '第一项')
    await runAction('第一项', '项目列表', '- 第一项')
    await runAction('- 第一项', '编号列表', '1. 第一项')
  })

  it('超长来源网址使用可换行的受限宽度样式', () => {
    const longSourceUrl = 'https://docs.example.com/d/home/a-very-long-document-identifier-without-natural-breaks?section=another-very-long-section-identifier'

    render(
      <BakeTemplatesTab
        templates={[{ ...template, sourceUrl: longSourceUrl }]}
        total={1}
        limit={20}
        offset={0}
        query=""
        from=""
        to=""
        draftQuery=""
        draftFrom=""
        draftTo=""
        selectedTemplateId={template.id}
        onSelectTemplate={noop}
        onCreateTemplate={noop}
        onUpdateTemplate={noop}
        onToggleTemplateStatus={noop}
        onDeleteTemplate={noop}
        onViewSourceMemory={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '查看文档「周报模板」详情' }))
    expect(screen.getByRole('link', { name: longSourceUrl })).toHaveClass('bake-source-url-link')
  })

  it('文档列表释放摘要空间，并从行操作和详情打开记忆图谱', () => {
    const onOpenGraph = vi.fn()
    const multiCategoryTemplate = {
      ...template,
      docType: 'weekly_report,project_plan',
      sourceUrl: 'https://docs.example.com/projects/weekly',
    }
    render(
      <BakeTemplatesTab
        templates={[multiCategoryTemplate]}
        total={1}
        limit={20}
        offset={0}
        query=""
        from=""
        to=""
        draftQuery=""
        draftFrom=""
        draftTo=""
        selectedTemplateId={template.id}
        onSelectTemplate={noop}
        onCreateTemplate={noop}
        onUpdateTemplate={noop}
        onToggleTemplateStatus={noop}
        onDeleteTemplate={noop}
        onViewSourceMemory={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        onOpenGraph={onOpenGraph}
      />,
    )

    const table = screen.getByRole('table', { name: '文档表格' })
    expect(within(table).queryByRole('columnheader', { name: '状态' })).not.toBeInTheDocument()
    expect(within(table).getByText('周报')).toBeInTheDocument()
    expect(within(table).getByText('项目方案')).toBeInTheDocument()
    // 关键词独占首行；筛选内容与按钮区由整行分隔线隔开，清空、搜索和新建统一位于底部右侧。
    const clearButton = screen.getByRole('button', { name: '清空' })
    const searchButton = screen.getByRole('button', { name: '搜索' })
    const createDocumentButton = screen.getByRole('button', { name: '新建' }) as HTMLButtonElement
    const primaryActions = createDocumentButton.closest('.bake-list-toolbar__repository-primary-actions')
    const separatedActionRow = primaryActions?.closest('.bake-list-toolbar__repository-actions--secondary')
    expect(createDocumentButton.type).toBe('button')
    expect(primaryActions).not.toBeNull()
    expect(separatedActionRow).not.toBeNull()
    expect(primaryActions).toContainElement(clearButton)
    expect(primaryActions).toContainElement(searchButton)
    expect(screen.getByText('关键词').closest('.bake-list-toolbar__repository-row--search')).not.toContainElement(searchButton)
    fireEvent.click(screen.getByRole('button', { name: /在记忆图谱中查看文档/ }))
    expect(onOpenGraph).toHaveBeenCalledWith(multiCategoryTemplate)

    fireEvent.click(screen.getByRole('button', { name: /查看文档.*详情/ }))
    const drawer = screen.getByRole('dialog', { name: '周报模板' })
    expect(within(drawer).getByRole('link', { name: multiCategoryTemplate.sourceUrl })).toBeInTheDocument()
    fireEvent.click(within(drawer).getByRole('button', { name: '记忆图谱' }))
    expect(onOpenGraph).toHaveBeenCalledTimes(2)
  })

  it('文档详情隐藏模板技术字段，并在标题行进入编辑', () => {
    const onOpenSkill = vi.fn()
    render(
      <BakeTemplatesTab
        templates={[template, draftTemplate]}
        total={2}
        limit={20}
        offset={0}
        query=""
        from=""
        to=""
        draftQuery=""
        draftFrom=""
        draftTo=""
        selectedTemplateId={template.id}
        onSelectTemplate={noop}
        onCreateTemplate={noop}
        onUpdateTemplate={noop}
        onToggleTemplateStatus={noop}
        onDeleteTemplate={noop}
        relatedSkills={[relatedSkill]}
        onOpenSkill={onOpenSkill}
        onViewSourceMemory={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
      />,
    )

    expect(screen.getByRole('table', { name: '文档表格' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看文档「周报模板」详情' }))
    const drawer = screen.getByRole('dialog', { name: '周报模板' })
    expect(within(drawer).getByText('文档内容')).toBeInTheDocument()
    expect(screen.getAllByText('周报').length).toBeGreaterThan(0)
    expect(within(drawer).queryByText('结构骨架（决定输出结构）')).not.toBeInTheDocument()
    expect(within(drawer).queryByText('表达风格（决定措辞）')).not.toBeInTheDocument()
    expect(within(drawer).queryByText(/替换规则|风格短语|结构字段/)).not.toBeInTheDocument()
    expect(within(drawer).getByText('已启用')).toBeInTheDocument()
    expect(within(drawer).queryByText('草稿')).not.toBeInTheDocument()
    expect(within(drawer).queryByText(/使用 \d+ 次/)).not.toBeInTheDocument()
    expect(within(drawer).queryByText('high')).not.toBeInTheDocument()
    expect(within(drawer).queryByText(/匹配分|匹配等级|来源记忆|提炼状态/)).not.toBeInTheDocument()
    expect(within(drawer).getByText('关联技能')).toBeInTheDocument()
    expect(within(drawer).getByText(/创建时间.*2026.*8.*11.*00:35:45.*最近更新.*2026.*8.*11.*14:18:36/)).toBeInTheDocument()
    expect(within(drawer).getByText('跨部门技术沟通会文档')).toBeInTheDocument()
    expect(within(drawer).getByText('已安装')).toBeInTheDocument()
    fireEvent.click(within(drawer).getByRole('button', { name: /跨部门技术沟通会文档/ }))
    expect(onOpenSkill).toHaveBeenCalledWith(relatedSkill)

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    expect(screen.getByRole('textbox', { name: '文档名称' })).toHaveValue('周报模板')
    expect(screen.getByRole('combobox', { name: '文档分类' })).toHaveTextContent('周报')
    expect(screen.getByRole('textbox', { name: '文档内容' })).toBeInTheDocument()
    expect(screen.queryByText('关联技能')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument()
  })

  it('新建文档使用弹窗草稿，取消不入库且保存后才创建', async () => {
    const onCreateTemplate = vi.fn().mockResolvedValue(true)
    const commonProps = {
      total: 1,
      limit: 20,
      offset: 0,
      query: '',
      from: '',
      to: '',
      draftQuery: '',
      draftFrom: '',
      draftTo: '',
      onSelectTemplate: noop,
      onCreateTemplate,
      onUpdateTemplate: noop,
      onToggleTemplateStatus: noop,
      onDeleteTemplate: noop,
      onViewSourceMemory: noop,
      onPageChange: noop,
      onLimitChange: noop,
      onDraftQueryChange: noop,
      onDraftFromChange: noop,
      onDraftToChange: noop,
      onSearch: noop,
      onClearFilters: noop,
    }
    render(
      <BakeTemplatesTab
        {...commonProps}
        templates={[template]}
        selectedTemplateId={template.id}
      />,
    )

    expect(screen.getByRole('button', { name: '新建' })).toBeInTheDocument()
    expect(screen.queryByText('新建模板')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    expect(screen.getByRole('dialog', { name: '新建文档' })).toBeInTheDocument()
    expect(onCreateTemplate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog', { name: '新建文档' })).not.toBeInTheDocument()
    expect(onCreateTemplate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    fireEvent.change(screen.getByRole('textbox', { name: '新文档名称' }), { target: { value: '供应商尽调报告' } })
    fireEvent.click(screen.getByRole('combobox', { name: '新文档分类' }))
    fireEvent.change(screen.getByRole('textbox', { name: '搜索或自定义文档分类' }), { target: { value: '法律尽调' } })
    fireEvent.keyDown(screen.getByRole('textbox', { name: '搜索或自定义文档分类' }), { key: 'Enter' })
    expect(screen.getByRole('combobox', { name: '新文档分类' })).toHaveTextContent('法律尽调')
    const contentEditor = screen.getByRole('textbox', { name: '新文档内容' })
    contentEditor.innerHTML = '<p>核对主体资质与关键合同。</p>'
    fireEvent.input(contentEditor)
    expect(onCreateTemplate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(onCreateTemplate).toHaveBeenCalledWith({
      title: '供应商尽调报告',
      docType: '法律尽调',
      fullContent: '核对主体资质与关键合同。',
    }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建文档' })).not.toBeInTheDocument())
  })

  it('知识列表和详情不展示内部提炼字段', () => {
    render(
      <BakeKnowledgeTab
        items={[knowledge]}
        total={1}
        limit={20}
        offset={0}
        query=""
        draftQuery=""
        from=""
        to=""
        draftFrom=""
        draftTo=""
        selectedKnowledgeId={knowledge.id}
        onSelectKnowledge={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        onDeleteKnowledge={noop}
        onViewSourceTimeline={noop}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '查看知识「本地优先知识」详情' }))
    expect(within(screen.getByRole('dialog', { name: '本地优先知识' })).getByRole('button', { name: '删除' })).toBeInTheDocument()
    expect(screen.getAllByText('本地优先知识').length).toBeGreaterThan(0)
    expect(screen.queryByText(/bake_knowledge/)).not.toBeInTheDocument()
    expect(screen.queryByText(/重复观察/)).not.toBeInTheDocument()
    expect(screen.queryByText('high')).not.toBeInTheDocument()
    expect(screen.queryByText(/匹配分|匹配等级|提炼状态/)).not.toBeInTheDocument()
    expect(screen.queryByText('实体 / 标签')).not.toBeInTheDocument()
    expect(screen.queryByText('MemoryBread')).not.toBeInTheDocument()
  })

  it('知识详情支持新建入口和标题行编辑保存', async () => {
    const onUpdateKnowledge = vi.fn().mockResolvedValue(true)
    render(
      <BakeKnowledgeTab
        items={[knowledge]}
        total={1}
        limit={20}
        offset={0}
        query=""
        draftQuery=""
        from=""
        to=""
        draftFrom=""
        draftTo=""
        selectedKnowledgeId={knowledge.id}
        onSelectKnowledge={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        onDeleteKnowledge={noop}
        onViewSourceTimeline={noop}
        onCreateKnowledge={noop}
        onUpdateKnowledge={onUpdateKnowledge}
      />,
    )

    expect(screen.getByRole('button', { name: '新建' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑知识「本地优先知识」' }))
    fireEvent.change(screen.getByRole('textbox', { name: '知识标题' }), { target: { value: '更新后的知识' } })
    expect(screen.getByRole('textbox', { name: '知识详细内容' })).toHaveAttribute('contenteditable', 'true')
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(onUpdateKnowledge).toHaveBeenCalledWith(expect.objectContaining({ summary: '更新后的知识' })))
  })

  it('SOP详情不展示原始关联ID与工作提示预览', () => {
    render(
      <BakeSopTab
        candidates={[sop]}
        total={1}
        limit={20}
        offset={0}
        query=""
        from=""
        to=""
        draftQuery=""
        draftFrom=""
        draftTo=""
        selectedSopId={sop.id}
        onSelectSop={noop}
        onDeleteSop={noop}
        onViewSourceTimeline={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        onCreateSop={noop}
        onUpdateSop={noop}
      />,
    )

    const operationTable = screen.getByRole('table', { name: '操作表格' })
    expect(within(operationTable).getByRole('columnheader', { name: '适用场景' })).toBeInTheDocument()
    expect(within(operationTable).getByRole('columnheader', { name: '操作环节概述' })).toBeInTheDocument()
    expect(within(operationTable).queryByRole('columnheader', { name: '步骤' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '查看操作：服务无法启动' }))
    expect(within(screen.getByRole('dialog', { name: '服务无法启动' })).getByRole('button', { name: '删除' })).toBeInTheDocument()
    expect(screen.queryByText('关联知识')).not.toBeInTheDocument()
    expect(screen.queryByText('已关联 2 条知识（用于补充背景和术语）')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '排查服务健康检查失败' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '启动端口冲突的处理步骤' })).not.toBeInTheDocument()
    expect(screen.queryByText('101、202')).not.toBeInTheDocument()
    expect(screen.queryByText('工作提示预览')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '复制工作提示' })).not.toBeInTheDocument()
    expect(screen.queryByText(/置信度/)).not.toBeInTheDocument()
    expect(screen.queryByText(/来源：/)).not.toBeInTheDocument()
    expect(screen.queryByText('启动失败排查')).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/来源/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    expect(screen.queryByText(/置信度/)).not.toBeInTheDocument()
  })

  it('知识详情可收藏，列表可切换收藏筛选', async () => {
    const onFavoriteFilterChange = vi.fn()
    const onToggleFavorite = vi.fn().mockResolvedValue(true)
    render(
      <BakeKnowledgeTab
        items={[knowledge]}
        total={1}
        limit={20}
        offset={0}
        query=""
        draftQuery=""
        from=""
        to=""
        draftFrom=""
        draftTo=""
        selectedKnowledgeId={knowledge.id}
        onSelectKnowledge={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        onDeleteKnowledge={noop}
        onViewSourceTimeline={noop}
        favoriteFilter="all"
        onFavoriteFilterChange={onFavoriteFilterChange}
        onToggleFavorite={onToggleFavorite}
      />,
    )

    fireEvent.change(screen.getByRole('combobox', { name: '收藏状态' }), {
      target: { value: 'favorite' },
    })
    expect(onFavoriteFilterChange).toHaveBeenCalledWith('favorite')
    fireEvent.click(screen.getByRole('button', { name: '查看知识「本地优先知识」详情' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: '本地优先知识' })).getByRole('button', { name: '收藏' }))
    await waitFor(() => expect(onToggleFavorite).toHaveBeenCalledWith(knowledge, true))
  })

  it('操作详情可收藏，列表可切换收藏筛选', async () => {
    const onFavoriteFilterChange = vi.fn()
    const onToggleFavorite = vi.fn().mockResolvedValue(true)
    render(
      <BakeSopTab
        candidates={[sop]}
        total={1}
        limit={20}
        offset={0}
        query=""
        from=""
        to=""
        draftQuery=""
        draftFrom=""
        draftTo=""
        selectedSopId={sop.id}
        onSelectSop={noop}
        onDeleteSop={noop}
        onViewSourceTimeline={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        favoriteFilter="all"
        onFavoriteFilterChange={onFavoriteFilterChange}
        onToggleFavorite={onToggleFavorite}
      />,
    )

    fireEvent.change(screen.getByRole('combobox', { name: '收藏状态' }), {
      target: { value: 'not_favorite' },
    })
    expect(onFavoriteFilterChange).toHaveBeenCalledWith('not_favorite')
    fireEvent.click(screen.getByRole('button', { name: '查看操作：服务无法启动' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: '服务无法启动' })).getByRole('button', { name: '收藏' }))
    await waitFor(() => expect(onToggleFavorite).toHaveBeenCalledWith(sop, true))
  })

  it('文档详情可收藏，列表可切换收藏筛选', async () => {
    const onFavoriteFilterChange = vi.fn()
    const onToggleFavorite = vi.fn().mockResolvedValue(true)
    render(
      <BakeTemplatesTab
        templates={[template]}
        total={1}
        limit={20}
        offset={0}
        query=""
        from=""
        to=""
        draftQuery=""
        draftFrom=""
        draftTo=""
        selectedTemplateId={template.id}
        onSelectTemplate={noop}
        onCreateTemplate={noop}
        onUpdateTemplate={noop}
        onToggleTemplateStatus={noop}
        onDeleteTemplate={noop}
        onViewSourceMemory={noop}
        onPageChange={noop}
        onLimitChange={noop}
        onDraftQueryChange={noop}
        onDraftFromChange={noop}
        onDraftToChange={noop}
        onSearch={noop}
        onClearFilters={noop}
        favoriteFilter="all"
        onFavoriteFilterChange={onFavoriteFilterChange}
        onToggleFavorite={onToggleFavorite}
      />,
    )

    fireEvent.change(screen.getByRole('combobox', { name: '收藏状态' }), {
      target: { value: 'favorite' },
    })
    expect(onFavoriteFilterChange).toHaveBeenCalledWith('favorite')
    fireEvent.click(screen.getByRole('button', { name: '查看文档「周报模板」详情' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: '周报模板' })).getByRole('button', { name: '收藏' }))
    await waitFor(() => expect(onToggleFavorite).toHaveBeenCalledWith(template, true))
  })

  const renderTemplatesTab = (overrides: Partial<ComponentProps<typeof BakeTemplatesTab>> = {}) => render(
    <BakeTemplatesTab
      templates={[template]}
      total={1}
      limit={20}
      offset={0}
      query=""
      from=""
      to=""
      draftQuery=""
      draftFrom=""
      draftTo=""
      selectedTemplateId={template.id}
      onSelectTemplate={noop}
      onCreateTemplate={noop}
      onUpdateTemplate={noop}
      onToggleTemplateStatus={noop}
      onDeleteTemplate={noop}
      onViewSourceMemory={noop}
      onPageChange={noop}
      onLimitChange={noop}
      onDraftQueryChange={noop}
      onDraftFromChange={noop}
      onDraftToChange={noop}
      onSearch={noop}
      onClearFilters={noop}
      {...overrides}
    />,
  )

  it('文档详情展示即时刷新字段，点击立即刷新调用刷新回调', async () => {
    const onRefreshTemplate = vi.fn().mockResolvedValue(undefined)
    const refreshTemplate = {
      ...template,
      sourceUrl: 'https://docs.example.com/weekly',
      refreshPolicy: 'auto' as const,
      lastRefreshCheckedAtMs: new Date(2026, 7, 20, 4, 21, 24).getTime(),
      lastRefreshError: 'FOCUS_POLICY_BLOCKED',
    }
    renderTemplatesTab({ templates: [refreshTemplate], onRefreshTemplate })

    fireEvent.click(screen.getByRole('button', { name: '查看文档「周报模板」详情' }))
    const drawer = screen.getByRole('dialog', { name: '周报模板' })
    expect(within(drawer).getByText('即时刷新')).toBeInTheDocument()
    expect(within(drawer).getByText('自动判断')).toBeInTheDocument()
    expect(within(drawer).getByText('FOCUS_POLICY_BLOCKED')).toBeInTheDocument()
    expect(within(drawer).getByText(/2026.*8.*20.*04:21:24/)).toBeInTheDocument()

    fireEvent.click(within(drawer).getByRole('button', { name: '立即刷新' }))
    await waitFor(() => expect(onRefreshTemplate).toHaveBeenCalledWith(template.id))
  })

  it('无来源网址的文档不展示立即刷新按钮并提示无法刷新', () => {
    const onRefreshTemplate = vi.fn()
    renderTemplatesTab({ templates: [{ ...template, sourceUrl: undefined }], onRefreshTemplate })

    fireEvent.click(screen.getByRole('button', { name: '查看文档「周报模板」详情' }))
    const drawer = screen.getByRole('dialog', { name: '周报模板' })
    expect(within(drawer).queryByRole('button', { name: '立即刷新' })).not.toBeInTheDocument()
    expect(within(drawer).getByText('没有来源网址，无法即时刷新')).toBeInTheDocument()
  })

  it('编辑模式可调整即时刷新策略，保存时调用策略回调', async () => {
    const onUpdateTemplate = vi.fn().mockResolvedValue(true)
    const onSetTemplateRefreshPolicy = vi.fn().mockResolvedValue(true)
    renderTemplatesTab({
      templates: [{ ...template, sourceUrl: 'https://docs.example.com/weekly', refreshPolicy: 'auto' as const }],
      onUpdateTemplate,
      onSetTemplateRefreshPolicy,
    })

    fireEvent.click(screen.getByRole('button', { name: '编辑文档「周报模板」' }))
    const policySelect = screen.getByRole('combobox', { name: '即时刷新策略' })
    expect(policySelect).toHaveValue('auto')
    fireEvent.change(policySelect, { target: { value: 'never' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(onSetTemplateRefreshPolicy).toHaveBeenCalledWith(template.id, 'never'))
  })

  it('策略未变更时保存不调用策略回调', async () => {
    const onUpdateTemplate = vi.fn().mockResolvedValue(true)
    const onSetTemplateRefreshPolicy = vi.fn().mockResolvedValue(true)
    renderTemplatesTab({
      templates: [{ ...template, refreshPolicy: 'always' as const }],
      onUpdateTemplate,
      onSetTemplateRefreshPolicy,
    })

    fireEvent.click(screen.getByRole('button', { name: '编辑文档「周报模板」' }))
    expect(screen.getByRole('combobox', { name: '即时刷新策略' })).toHaveValue('always')
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(onUpdateTemplate).toHaveBeenCalled())
    expect(onSetTemplateRefreshPolicy).not.toHaveBeenCalled()
  })
})
