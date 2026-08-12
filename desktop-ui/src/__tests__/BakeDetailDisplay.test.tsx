import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import BakeKnowledgeTab from '../components/bake/BakeKnowledgeTab'
import BakeTemplatesTab from '../components/bake/BakeTemplatesTab'
import BakeSopTab from '../components/bake/BakeSopTab'
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

    expect(screen.getByRole('link', { name: longSourceUrl })).toHaveClass('bake-source-url-link')
  })

  it('模板详情使用更明确的结构/风格说明文案', () => {
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

    expect(screen.getByText('结构骨架（决定输出结构）')).toBeInTheDocument()
    expect(screen.getByText('表达风格（决定措辞）')).toBeInTheDocument()
    expect(screen.getByText('常用短语：整体看、先结论后展开')).toBeInTheDocument()
    expect(screen.queryByText('已启用')).not.toBeInTheDocument()
    expect(screen.queryByText('草稿')).not.toBeInTheDocument()
    expect(screen.queryByText(/使用 \d+ 次/)).not.toBeInTheDocument()
    expect(screen.queryByText('high')).not.toBeInTheDocument()
    expect(screen.queryByText(/匹配分|匹配等级|来源记忆|提炼状态/)).not.toBeInTheDocument()
    expect(screen.getByText('关联技能')).toBeInTheDocument()
    expect(screen.getByText(/新增时间.*2026.*8.*11.*00:35:45.*最近更新.*2026.*8.*11.*14:18:36/)).toBeInTheDocument()
    expect(screen.getByText('跨部门技术沟通会文档')).toBeInTheDocument()
    expect(screen.getByText('已安装')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /跨部门技术沟通会文档/ }))
    expect(onOpenSkill).toHaveBeenCalledWith(relatedSkill)
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
        onOpenCapture={noop}
        onViewSourceTimeline={noop}
      />,
    )

    expect(screen.getAllByText('本地优先知识').length).toBeGreaterThan(0)
    expect(screen.queryByText(/bake_knowledge/)).not.toBeInTheDocument()
    expect(screen.queryByText(/重复观察/)).not.toBeInTheDocument()
    expect(screen.queryByText('high')).not.toBeInTheDocument()
    expect(screen.queryByText(/匹配分|匹配等级|提炼状态/)).not.toBeInTheDocument()
    expect(screen.queryByText('实体 / 标签')).not.toBeInTheDocument()
    expect(screen.queryByText('MemoryBread')).not.toBeInTheDocument()
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
      />,
    )

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

    fireEvent.click(screen.getByRole('button', { name: '新建' }))
    expect(screen.queryByText(/置信度/)).not.toBeInTheDocument()
  })
})
