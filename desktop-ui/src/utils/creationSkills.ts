import { fetchWithLocalhostFallback } from '../hooks/useApi'
import { serviceEnvironmentHeaders } from '../store/useAppStore'
import { OFFLINE_CREATION_SKILL_CATEGORIES } from '../data/creationSkillCategories'
import { unzipSync, zipSync } from 'fflate'

export type CreationSkillSourceKind = 'creation_history' | 'bake_document' | 'market' | 'imported' | 'manual'

export interface CreationSkillSource {
  kind: CreationSkillSourceKind
  id: string
  title: string
  content: string
  docType: string
}

export interface CreationSkillContent {
  skillDescription: CreationSkillDescription
  executionSteps: CreationSkillExecutionStep[]
  commonTitles: string[]
  titleStyle: string
  textStyle: string
  diagramStyle: string
  writingGuidelines: string[]
  distinctiveSections?: CreationSkillDistinctiveSection[]
  sectionHeadings: CreationSkillSectionHeadings
  fieldExamples: CreationSkillFieldExamples
  exampleDocument: string
}

export interface CreationSkillDescription {
  purpose: string
  documentTypes: string[]
  problems: string[]
  domains: string[]
  deliverables: string[]
}

export interface CreationSkillExecutionStep {
  id: string
  title: string
  objective: string
  output: string
  agents: string[]
  skills: string[]
  tools: string[]
  /** 默认 false；只控制是否保留证据图片，不控制静默优先的即时 DOM/AX 取数。 */
  retainWebpageScreenshot?: boolean
}

export interface CreationSkillDistinctiveSection {
  title: string
  description: string
  guidance: string
  examples: string[]
}

export interface CreationSkillSectionHeadings {
  commonTitles: string
  titleStyle: string
  textStyle: string
  diagramStyle: string
  writingGuidelines: string
}

export interface CreationSkillFieldExamples {
  commonTitles: string[]
  titleStyle: string[]
  textStyle: string[]
  diagramStyle: string[]
  writingGuidelines: string[]
}

export interface CreationSkillPackageFile {
  path: string
  mediaType: string
  contentBase64: string
  sizeBytes: number
}

export interface CodexSkillMetadata {
  name: string
  description: string
  instructions: string
}

export interface CreationSkillAnalysis extends CreationSkillContent {
  title: string
  summary: string
  suggestedCategoryKeywords: string[]
  analysisMode: 'local_model' | 'heuristic_fallback' | string
  fallbackReason?: string
}

export interface LocalCreationSkill extends CreationSkillContent {
  id: number
  clientSkillKey: string
  cloudSkillId?: string | null
  sourceKind: CreationSkillSourceKind
  sourceId: string
  title: string
  summary: string
  categoryId?: string | null
  status: 'draft' | 'saved'
  installed: boolean
  published: boolean
  packageFiles?: CreationSkillPackageFile[]
  createdAt: number
  updatedAt: number
}

export interface LocalCreationSkillQuery {
  sourceKind?: CreationSkillSourceKind
  sourceId?: string
  installed?: boolean
}

export interface MatchedCreationSkill {
  skill: LocalCreationSkill
  reason: 'mentioned' | 'automatic'
  score: number
}

export interface ExecutionSkillResolution {
  matches: MatchedCreationSkill[]
  source: 'mentioned' | 'model' | 'unavailable'
  reasoning: string
}

export interface CreationSkillCategory {
  id: string
  key: string
  name: string
  level: 1 | 2 | 3 | 4
  parentId?: string | null
  sortOrder: number
}

export interface CreationSkillCategoryOption extends CreationSkillCategory {
  depth: number
}

export interface CreationSkillMarketAuthor {
  id: string
  nickname: string
}

export interface CreationSkillMarketItem extends CreationSkillContent {
  id: string
  title: string
  summary: string
  isOfficial?: boolean
  categoryId: string
  categoryPath: CreationSkillCategory[]
  author: CreationSkillMarketAuthor
  packageFiles: CreationSkillPackageFile[]
  packageName: string
  packageFileCount: number
  packageSizeBytes: number
  packageSha256: string
  publishedAt?: string | null
  updatedAt: string
}

export interface CreationSkillMarketPage {
  items: CreationSkillMarketItem[]
  total: number
  limit: number
  offset: number
}

export interface CreationSkillMarketQuery {
  query?: string
  categoryId?: string
  limit?: number
  offset?: number
}

const parseError = async (response: Response, fallback: string) => {
  const text = await response.text().catch(() => '')
  const trimmed = text.trim()
  if (trimmed) {
    try {
      const payload = JSON.parse(trimmed)
      const message = payload?.error?.message
        || payload?.message
        || (typeof payload?.error === 'string' ? payload.error : '')
      if (message) return String(message)
    } catch {
      const missingField = trimmed.match(/missing field `([^`]+)`/)
      if (missingField) return `请求缺少必需字段 ${missingField[1]}，请刷新页面后重试`
      if (/invalid type/i.test(trimmed)) return '请求字段类型不正确，请刷新页面后重试'
      if (/failed to deserialize the json body/i.test(trimmed)) return '请求内容格式不正确，请刷新页面后重试'
      if (/expected request with `content-type/i.test(trimmed)) return '请求类型不正确，需要 application/json'
      if (/request body (limit|size)/i.test(trimmed)) return '请求内容过大，请精简 Skill 源文件或示例文档后重试'
      if (trimmed.length > 0 && trimmed.length <= 120) return trimmed
    }
  }
  return `${fallback}（服务返回 HTTP ${response.status}，请稍后重试）`
}

const CREATION_SKILL_ANALYSIS_RETRY_DELAY_MS = 750
const CREATION_SKILL_ANALYSIS_JOB_TIMEOUT_MS = 6 * 60 * 1000
const CREATION_SKILL_ANALYSIS_MAX_POLL_FAILURES = 3
const CREATION_SKILL_MARKET_TIMEOUT_MS = 8_000
const RETRYABLE_CREATION_SKILL_ANALYSIS_ERRORS = new Set([
  'CREATION_SKILL_ANALYZER_UNAVAILABLE',
  'CREATION_SKILL_ANALYSIS_FAILED',
])

const isTransientCreationSkillNetworkError = (error: unknown) => {
  const message = error instanceof Error ? error.message : String(error)
  return error instanceof TypeError
    || /failed to fetch|networkerror|load failed|connection refused|econnrefused/i.test(message)
}

const waitForCreationSkillAnalysisRetry = () => new Promise<void>((resolve) => {
  window.setTimeout(resolve, CREATION_SKILL_ANALYSIS_RETRY_DELAY_MS)
})

const readCreationSkillAnalysisError = async (response: Response) => {
  const payload = await response.json().catch(() => null)
  return {
    code: String(payload?.error || ''),
    message: String(payload?.message || '沉淀技能失败'),
  }
}

export const DEFAULT_CREATION_SKILL_SECTION_HEADINGS: CreationSkillSectionHeadings = {
  commonTitles: '标题设计风格',
  titleStyle: '标题设计风格',
  textStyle: '行文设计思路',
  diagramStyle: '图片生成方式',
  writingGuidelines: '话术表达风格',
}

export const DEFAULT_CREATION_SKILL_FIELD_EXAMPLES: CreationSkillFieldExamples = {
  commonTitles: ['现状与约束', '方案如何落到执行'],
  titleStyle: ['现状与约束', '方案如何落到执行'],
  textStyle: ['先界定适用范围，再沿“现状 → 判断 → 动作 → 验证”逐层收束。'],
  diagramStyle: ['PlantUML 活动图：主流程纵向排列，跨角色动作放入对应泳道。'],
  writingGuidelines: ['需要说明的是，目标对象只覆盖已经确认的适用范围。'],
}

export const CREATION_SKILL_AGENT_OPTIONS = [
  { id: 'industry_research_agent', label: '行业调研 Agent' },
  { id: 'data_analysis_agent', label: '数据分析 Agent' },
  { id: 'solution_design_agent', label: '方案设计 Agent' },
  { id: 'chapter_design_agent', label: '章节设计 Agent' },
  { id: 'document_writer_agent', label: '文档撰写 Agent' },
  { id: 'anti_ai_style_agent', label: '去 AI 味 Agent' },
  { id: 'detail_polish_agent', label: '细节润色 Agent' },
  { id: 'table_polish_agent', label: '表格润色 Agent' },
  { id: 'typography_polish_agent', label: '字体润色 Agent' },
  { id: 'image_polish_agent', label: '图片润色 Agent' },
  { id: 'quality_review_agent', label: '质量审校 Agent' },
] as const

export const CREATION_SKILL_TOOL_OPTIONS = [
  { id: 'memory_search', label: '记忆搜索' },
  { id: 'internet_search', label: '互联网检索' },
  { id: 'data_search', label: '数据检索' },
  { id: 'webpage_scrape', label: '网页爬取' },
  { id: 'github_search', label: 'GitHub 检索' },
  { id: 'plantuml_diagram', label: 'PlantUML 画图' },
  { id: 'mermaid_diagram', label: 'Mermaid 画图' },
] as const

export const DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT = `# 共享评审空间：预约流程与协作边界优化方案

## 摘要

本文围绕一个完全虚构的共享评审空间场景，讨论预约信息分散、资源状态不透明和异常处理依赖口头协调的问题。方案的重点不是增加审批，而是让每次申请都能回答三个问题：当前由谁使用、下一步由谁处理、完成后凭什么确认资源已经释放。

全文先界定问题和适用范围，再把目标拆成可观察状态，随后给出角色分工、核心流程、异常保障与验证方式。所有判断都落到动作和证据，不使用真实组织、项目或业务数据。

## 背景与问题：一次冲突暴露出的状态断点

共享评审空间同时服务准备材料、集中讨论和结果确认等活动。现有做法只记录“有人预约”，却没有说明准备是否完成、临时变更是否被接收、使用结束后资源是否已经恢复。信息看似存在，真正执行时仍要逐人询问。

问题的核心不是缺少一张登记表，而是状态、动作和责任没有对应关系。申请角色关心能否使用，维护角色关心是否满足开放条件，后续使用者关心资源何时重新可用；如果这些问题混在一个备注框里，任何变更都会重新触发人工确认。

## 目标与范围：先明确要解决什么

本次优化只处理预约发起、冲突确认、使用准备、完成释放和异常复核。目标是让相关角色不依赖额外询问，也能从同一处判断当前状态、待办动作和完成证据。界面样式、空间硬件和人员排班不在本次方案范围内。

需要明确的是，范围约束不是附注，而是后续取舍的依据。凡是不能改变状态判断、责任归属或验证结果的信息，都不进入主流程；确需保留的补充说明放在对应动作之后，避免重要条件被长段背景淹没。

## 方案设计：让状态、责任与动作相互对应

方案把一次预约拆成“申请、确认、准备、使用、释放、复核”几个连续状态。每个状态都绑定进入条件、责任角色、应执行动作和完成证据；只有证据满足要求，状态才向后流转。这样既能保持流程简洁，也能避免角色凭经验猜测。

角色分工遵循“谁产生信息，谁负责首次更新；谁消费结果，谁负责确认可用”的原则：

- 申请角色说明使用目的、期望范围和必要准备，并对变更及时更新。
- 维护角色检查冲突与开放条件，只对自己能够验证的状态作确认。
- 使用角色在开始前确认资源状态，在结束后提交释放结果和遗留事项。
- 复核角色只处理异常和争议，不重复参与每一次正常流转。

## 核心流程：从提出申请到完成释放

流程从申请角色提交用途和范围开始。系统先检查同一时段是否存在冲突；没有冲突时进入准备状态，有冲突时返回可调整的条件，而不是只给出“失败”结果。申请角色据此修改范围或撤回请求，避免维护角色在多个沟通渠道间转述。

随后，准备完成后由使用角色确认接手。确认动作意味着必要材料、访问边界和现场状态已经可用，而不是简单点击按钮。使用结束后，使用角色提交释放结果；若仍有遗留事项，则同时标明影响范围和下一位处理角色，流程不会把“已结束”误写成“已恢复”。

## 风险与保障：异常不能重新回到人工猜测

主要风险来自三类断点：状态被更新但相关角色没有接收、异常被记录却没有明确下一步、完成结果缺少可复核证据。对应保障也不应写成宽泛口号，而要直接嵌入流程。

- 关键状态变化只保留一个正式入口，其他渠道只发送提醒，不形成第二份事实。
- 异常记录必须同时包含影响范围、临时处理和下一位责任角色。
- 释放动作必须附带可观察结果；无法确认时回到复核状态，不直接标记完成。
- 长时间没有推进的事项进入待复核列表，由相关角色判断继续、调整或关闭。

## 验证与复盘：用可观察结果收束判断

验证分为流程可执行性和结果可判断性。前者关注相关角色能否只凭当前记录完成下一步，后者关注状态变化是否都有对应证据。试运行期间不追求覆盖所有例外，而是优先验证主流程是否连续、异常是否能回到明确责任人。

复盘时按“现象、判断、动作、结果”记录，不把意见数量当作效果。若某个节点仍需要反复口头确认，应先检查进入条件是否含糊；若不同角色对完成状态理解不一，应先修正证据定义，而不是继续增加提醒。

## 结论与后续：把临时协调变成稳定机制

这套方案把一次临时协调转化为可以被读取、执行和复核的状态链路。它保留必要的人为判断，但让判断发生在边界明确的位置；它减少重复询问，但不以隐藏异常为代价。

后续优化应继续围绕同一目标展开：让每位相关角色在进入流程时知道自己为什么接手、需要完成什么、完成后留下什么证据。只要这三个问题能够稳定回答，共享资源的协作就不再依赖某位熟悉情况的人持续兜底。`

function defaultCreationSkillDescription(
  title: string,
  summary: string,
  docType = '',
  domains: string[] = [],
): CreationSkillDescription {
  const documentType = docType.trim() || title.trim() || '专业文档'
  const evidence = `${title}\n${summary}\n${docType}`
  const problem = /研究|调研|分析|报告/.test(evidence)
    ? '把分散资料和证据转化为有依据、可比较、可形成结论的分析'
    : /方案|架构|设计|规划|建设/.test(evidence)
      ? '把目标、约束和关键取舍转化为可评审、可执行、可验证的方案'
      : /复盘|总结|纪要/.test(evidence)
        ? '从过程记录中提炼事实、判断、行动项和后续验证方式'
        : '把零散需求与事实组织成结构清晰、可直接使用的专业文档'
  return {
    purpose: summary.trim() || `用于在需要创作${documentType}时，复用这枚 Skill 的分析、组织和写作方法。`,
    documentTypes: [documentType],
    problems: [problem],
    domains: distinctCreationSkillItems(domains, 12),
    deliverables: [`一份结构完整、依据清楚并包含后续动作的${documentType}`],
  }
}

function defaultCreationSkillExecutionSteps(
  title: string,
  evidence = '',
): CreationSkillExecutionStep[] {
  const text = `${title}\n${evidence}`
  const steps: CreationSkillExecutionStep[] = [{
    id: 'collect-context',
    title: '收集需求与事实',
    objective: '明确创作目标、读者、范围、已有资料和不能推断的事实边界。',
    output: '需求清单、事实材料和待核验项',
    agents: [],
    skills: [],
    tools: ['memory_search'],
    retainWebpageScreenshot: false,
  }]
  if (/行业|市场|竞品|研究|调研|政策|趋势/.test(text)) {
    steps.push({
      id: 'research-industry',
      title: '开展行业调研',
      objective: '补充外部环境、通行做法和来源可追溯的行业证据。',
      output: '带来源的行业事实、趋势与可比较案例',
      agents: ['industry_research_agent'],
      skills: [],
      tools: ['internet_search'],
      retainWebpageScreenshot: false,
    })
  }
  if (/数据|指标|统计|趋势|成本|收益|测算|分析/.test(text)) {
    steps.push({
      id: 'analyze-data',
      title: '分析数据与证据',
      objective: '核对数据口径，识别关键关系、差异和支撑结论的证据。',
      output: '数据判断、口径说明和证据缺口',
      agents: ['data_analysis_agent'],
      skills: [],
      tools: ['data_search', 'webpage_scrape'],
      retainWebpageScreenshot: false,
    })
  }
  if (/方案|架构|设计|规划|建设|实施/.test(text)) {
    steps.push({
      id: 'design-solution',
      title: '设计方案',
      objective: '把目标、约束和证据转化为有边界、有取舍、有验证方式的方案。',
      output: '方案结构、关键设计、实施路径和风险控制',
      agents: ['solution_design_agent'],
      skills: [],
      tools: /架构|流程|链路|交互|模块/.test(text) ? ['plantuml_diagram'] : [],
      retainWebpageScreenshot: false,
    })
  }
  steps.push(
    {
      id: 'design-chapters',
      title: '设计章节蓝图',
      objective: '先确定章节顺序、每章要回答的问题、可用证据和完成标准。',
      output: '供初稿使用的章节蓝图',
      agents: ['chapter_design_agent'],
      skills: [],
      tools: [],
      retainWebpageScreenshot: false,
    },
    {
      id: 'draft-document',
      title: '撰写完整文档',
      objective: '依据前序产出和 Skill 的风格指纹完成全文，不补造事实。',
      output: '可继续编辑的完整 Markdown 文档',
      agents: ['document_writer_agent'],
      skills: [],
      tools: [],
      retainWebpageScreenshot: false,
    },
    {
      id: 'review-delivery',
      title: '审校并交付',
      objective: '检查目标回应、事实依据、结构完整、术语一致和行动可执行性。',
      output: '通过质量检查的最终文档与待核验项',
      agents: ['quality_review_agent'],
      skills: [],
      tools: [],
      retainWebpageScreenshot: false,
    },
  )
  return steps
}

function mapSkillDescription(
  value: any,
  title: string,
  summary: string,
  docType = '',
  domains: string[] = [],
): CreationSkillDescription {
  const fallback = defaultCreationSkillDescription(title, summary, docType, domains)
  const list = (camel: string, snake: string, defaultItems: string[]) => {
    const raw = value?.[camel] ?? value?.[snake]
    return Array.isArray(raw)
      ? distinctCreationSkillItems(raw, 12)
      : [...defaultItems]
  }
  return {
    purpose: String(value?.purpose || fallback.purpose).trim(),
    documentTypes: list('documentTypes', 'document_types', fallback.documentTypes),
    problems: list('problems', 'problems', fallback.problems),
    domains: list('domains', 'domains', fallback.domains),
    deliverables: list('deliverables', 'deliverables', fallback.deliverables),
  }
}

function mapExecutionSteps(
  value: unknown,
  title: string,
  evidence = '',
): CreationSkillExecutionStep[] {
  const allowedAgents = new Set(CREATION_SKILL_AGENT_OPTIONS.map(option => option.id))
  const allowedTools = new Set(CREATION_SKILL_TOOL_OPTIONS.map(option => option.id))
  const resources = (raw: unknown, allowed?: Set<string>) => (
    Array.isArray(raw)
      ? distinctCreationSkillItems(raw, 8).filter(item => !allowed || allowed.has(item))
      : []
  )
  const steps = Array.isArray(value)
    ? value.map((item: any, index) => {
      const id = String(item?.id || `step-${index + 1}`)
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, '-')
        .replace(/^-|-$/g, '')
        .slice(0, 80)
      const step: CreationSkillExecutionStep = {
        id: id || `step-${index + 1}`,
        title: String(item?.title || '').trim(),
        objective: String(item?.objective || '').trim(),
        output: String(item?.output || '').trim(),
        agents: resources(item?.agents, allowedAgents),
        skills: resources(item?.skills),
        tools: resources(item?.tools, allowedTools),
        retainWebpageScreenshot: item?.retainWebpageScreenshot
          ?? item?.retain_webpage_screenshot
          ?? false,
      }
      // 步骤目标与产出已合并为“执行动作”，产出允许为空。
      return step.title && step.objective ? step : null
    }).filter((step): step is CreationSkillExecutionStep => Boolean(step))
    : []
  return steps.length ? steps.slice(0, 12) : defaultCreationSkillExecutionSteps(title, evidence)
}

const MAX_SKILL_PACKAGE_FILES = 128
const MAX_SKILL_PACKAGE_FILE_BYTES = 5 * 1024 * 1024
const MAX_SKILL_PACKAGE_BYTES = 10 * 1024 * 1024
const MAX_SKILL_MARKDOWN_BYTES = 512 * 1024
const MAX_IMPORTED_SKILL_CONTEXT_CHARS = 120_000

export function parseCodexSkillMarkdown(content: string): CodexSkillMetadata {
  const normalized = content.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n')
  const match = normalized.match(/^---[ \t]*\n([\s\S]*?)\n---[ \t]*(?:\n|$)([\s\S]*)$/)
  if (!match) throw new Error('SKILL.md 必须以 YAML frontmatter 开头')
  const name = readFrontmatterValue(match[1], 'name')
  const description = readFrontmatterValue(match[1], 'description')
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name) || name.length > 64) {
    throw new Error('SKILL.md 的 name 必须是 1 到 64 位小写字母、数字或连字符')
  }
  if (!description || description.length > 1_024) {
    throw new Error('SKILL.md 的 description 需要在 1 到 1024 个字符之间')
  }
  return { name, description, instructions: match[2].trim() }
}

function readFrontmatterValue(frontmatter: string, key: string) {
  const lines = frontmatter.split('\n')
  const prefix = `${key}:`
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trimStart()
    if (!line.startsWith(prefix)) continue
    const raw = line.slice(prefix.length).trim()
    if (/^[|>][+-]?$/.test(raw)) {
      const values: string[] = []
      for (let next = index + 1; next < lines.length; next += 1) {
        if (!/^[ \t]/.test(lines[next])) break
        values.push(lines[next].trim())
      }
      return (raw.startsWith('>') ? values.join(' ') : values.join('\n')).trim()
    }
    if (
      (raw.startsWith('"') && raw.endsWith('"'))
      || (raw.startsWith('\'') && raw.endsWith('\''))
    ) {
      return raw.slice(1, -1).trim()
    }
    return raw.replace(/\s+#.*$/, '').trim()
  }
  return ''
}

export async function importAgentSkillPackage(
  files: File[] | FileList,
): Promise<Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'>> {
  const selectedFiles = Array.from(files).filter(file => file.name !== '.DS_Store')
  if (selectedFiles.length === 0) throw new Error('请选择一个包含 SKILL.md 的技能文件夹')
  if (selectedFiles.length > MAX_SKILL_PACKAGE_FILES) {
    throw new Error(`Skill 源文件数量不能超过 ${MAX_SKILL_PACKAGE_FILES} 个`)
  }
  const totalBytes = selectedFiles.reduce((sum, file) => sum + file.size, 0)
  if (totalBytes > MAX_SKILL_PACKAGE_BYTES) throw new Error('Skill 源文件总大小不能超过 10 MB')

  const rawPaths = selectedFiles.map(file => {
    const relativePath = String((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name)
      .replace(/\\/g, '/')
      .replace(/^\/+/, '')
    validateSkillFilePath(relativePath)
    return relativePath
  })
  const hasRootSkillMarkdown = rawPaths.includes('SKILL.md')
  const rootNames = hasRootSkillMarkdown
    ? new Set<string>()
    : new Set(rawPaths.filter(path => path.includes('/')).map(path => path.split('/')[0]))
  if (!hasRootSkillMarkdown && (rootNames.size > 1 || (rootNames.size === 1 && rawPaths.some(path => !path.includes('/'))))) {
    throw new Error('请一次只选择一个技能文件夹')
  }
  const rootName = rootNames.size === 1 ? [...rootNames][0] : ''
  const normalizedPaths = rawPaths.map(path => rootName ? path.slice(rootName.length + 1) : path)
  normalizedPaths.forEach(validateSkillFilePath)
  if (new Set(normalizedPaths).size !== normalizedPaths.length) {
    throw new Error('Skill 源文件包含重复文件路径')
  }

  const markdownIndex = normalizedPaths.findIndex(path => path === 'SKILL.md')
  if (markdownIndex < 0) throw new Error('Skill 源文件根目录缺少 SKILL.md')
  if (selectedFiles[markdownIndex].size > MAX_SKILL_MARKDOWN_BYTES) {
    throw new Error('SKILL.md 不能超过 512 KB')
  }
  let skillMarkdown: string
  try {
    skillMarkdown = new TextDecoder('utf-8', { fatal: true })
      .decode(await readBrowserFileBytes(selectedFiles[markdownIndex]))
  } catch {
    throw new Error('SKILL.md 必须是 UTF-8 文本文件')
  }
  const metadata = parseCodexSkillMarkdown(skillMarkdown)
  if (rootName && metadata.name !== rootName) {
    throw new Error(`SKILL.md 的 name 必须与文件夹名称一致（当前文件夹为 ${rootName}）`)
  }

  const packageFiles = await Promise.all(selectedFiles.map(async (file, index) => {
    if (file.size > MAX_SKILL_PACKAGE_FILE_BYTES) {
      throw new Error(`${normalizedPaths[index]} 不能超过 5 MB`)
    }
    const bytes = await readBrowserFileBytes(file)
    return {
      path: normalizedPaths[index],
      mediaType: file.type || inferSkillFileMediaType(normalizedPaths[index]),
      contentBase64: bytesToBase64(bytes),
      sizeBytes: bytes.byteLength,
    }
  }))
  packageFiles.sort((left, right) => (
    left.path === 'SKILL.md' ? -1 : right.path === 'SKILL.md' ? 1 : left.path.localeCompare(right.path)
  ))
  const memoryBreadProfile = readMemoryBreadPackageProfile(
    packageFiles,
    metadata.name,
    metadata.description,
  )

  const suffix = Date.now().toString(36)
  return {
    clientSkillKey: `imported-${metadata.name.slice(0, 48)}-${suffix}`,
    cloudSkillId: null,
    sourceKind: 'imported',
    sourceId: metadata.name,
    title: metadata.name,
    summary: metadata.description,
    categoryId: null,
    skillDescription: memoryBreadProfile?.skillDescription || {
      purpose: metadata.description,
      documentTypes: ['SKILL.md 定义的文档或交付物'],
      problems: ['按 Skill 源文件中的专业工作流和输出要求完成创作任务'],
      domains: [],
      deliverables: ['符合 SKILL.md 验收要求的完整交付物'],
    },
    executionSteps: memoryBreadProfile?.executionSteps || [
      {
        id: 'read-skill',
        title: '读取技能说明',
        objective: '读取 SKILL.md，并识别触发条件、输入、约束与输出要求。',
        output: '可执行的技能要求清单',
        agents: [],
        skills: [metadata.name],
        tools: [],
      },
      {
        id: 'load-resources',
        title: '按需加载资源',
        objective: '只读取当前任务需要的引用、资产和脚本说明。',
        output: '任务所需的技能上下文与可用资源',
        agents: [],
        skills: [metadata.name],
        tools: [],
      },
      {
        id: 'design-chapters',
        title: '设计章节蓝图',
        objective: '按照 SKILL.md 的交付要求先确定章节顺序、证据位置和完成标准。',
        output: '符合技能要求的章节蓝图',
        agents: ['chapter_design_agent'],
        skills: [metadata.name],
        tools: [],
      },
      {
        id: 'write-document',
        title: '执行技能工作流',
        objective: '按照章节蓝图和 SKILL.md 的先后顺序完成分析、创作和必要的工具调用。',
        output: '符合技能要求的完整草稿',
        agents: ['document_writer_agent'],
        skills: [metadata.name],
        tools: [],
      },
      {
        id: 'review-output',
        title: '核对输出要求',
        objective: '对照 SKILL.md 检查完整性、格式与使用边界。',
        output: '通过技能验收条件的最终交付物',
        agents: ['quality_review_agent'],
        skills: [metadata.name],
        tools: [],
      },
    ],
    commonTitles: memoryBreadProfile?.commonTitles || [metadata.name],
    titleStyle: memoryBreadProfile?.titleStyle || '遵循 SKILL.md 中的标题与输出要求。',
    textStyle: memoryBreadProfile?.textStyle || metadata.instructions || '严格遵循 SKILL.md 中定义的工作流与输出要求。',
    diagramStyle: memoryBreadProfile?.diagramStyle || '仅在 SKILL.md 或引用文件明确要求时生成图示。',
    writingGuidelines: memoryBreadProfile?.writingGuidelines || ['优先遵循 SKILL.md；引用其他文件时使用技能根目录相对路径。'],
    distinctiveSections: memoryBreadProfile?.distinctiveSections || [],
    sectionHeadings: memoryBreadProfile?.sectionHeadings || { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
    fieldExamples: memoryBreadProfile?.fieldExamples || cloneFieldExamples(DEFAULT_CREATION_SKILL_FIELD_EXAMPLES),
    exampleDocument: memoryBreadProfile?.exampleDocument || DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
    status: 'saved',
    installed: true,
    published: false,
    packageFiles,
  }
}

function readMemoryBreadPackageProfile(
  files: CreationSkillPackageFile[],
  title: string,
  summary: string,
): CreationSkillContent | null {
  const profileFile = files.find(file => file.path === 'references/memorybread-creation.json')
  if (!profileFile) return null
  try {
    const payload = JSON.parse(skillFileText(profileFile) || '')
    if (payload?.kind !== 'memorybread.creation-profile' || !payload?.content) return null
    const content = payload.content
    const exampleFile = files.find(file => file.path === 'references/example.md')
    return {
      skillDescription: mapSkillDescription(content.skill_description, title, summary),
      executionSteps: mapExecutionSteps(content.execution_steps, title, summary),
      commonTitles: Array.isArray(content.common_titles) ? content.common_titles.map(String) : [],
      titleStyle: String(content.title_style || ''),
      textStyle: String(content.text_style || ''),
      diagramStyle: String(content.diagram_style || ''),
      writingGuidelines: Array.isArray(content.writing_guidelines)
        ? content.writing_guidelines.map(String)
        : [],
      distinctiveSections: mapDistinctiveSections(content.distinctive_sections),
      sectionHeadings: mapSectionHeadings(content.section_headings),
      fieldExamples: mapFieldExamples(content.field_examples),
      exampleDocument: exampleFile
        ? skillFileText(exampleFile) || DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT
        : String(content.example_document || '') || DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
    }
  } catch {
    return null
  }
}

/** 保留旧名称作为兼容别名；实际读取的是通用 Agent Skills 目录。 */
export const importCodexSkillPackage = importAgentSkillPackage

export async function importAgentSkillZip(
  archive: File,
): Promise<Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'>> {
  if (!/\.zip$/i.test(archive.name)) throw new Error('请选择 Skill 源文件 ZIP')
  if (archive.size > MAX_SKILL_PACKAGE_BYTES) throw new Error('Skill 源文件总大小不能超过 10 MB')
  let entries: Record<string, Uint8Array>
  try {
    entries = unzipSync(await readBrowserFileBytes(archive))
  } catch {
    throw new Error('无法解压 Skill 源文件，请确认 ZIP 文件完整')
  }
  const files = Object.entries(entries)
    .filter(([path]) => !path.endsWith('/') && !path.endsWith('/.DS_Store') && path !== '.DS_Store')
    .map(([path, bytes]) => {
      const file = new File([arrayBufferFromBytes(bytes)], path.split('/').pop() || 'skill-file', {
        type: inferSkillFileMediaType(path),
      })
      Object.defineProperty(file, 'webkitRelativePath', { value: path })
      return file
    })
  return importAgentSkillPackage(files)
}

async function readBrowserFileBytes(file: File): Promise<Uint8Array> {
  const fileWithArrayBuffer = file as File & { arrayBuffer?: () => Promise<ArrayBuffer> }
  if (typeof fileWithArrayBuffer.arrayBuffer === 'function') {
    return new Uint8Array(await fileWithArrayBuffer.arrayBuffer())
  }

  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error(`读取 ${file.name} 失败`))
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(new Uint8Array(reader.result))
      } else {
        reject(new Error(`读取 ${file.name} 失败`))
      }
    }
    reader.readAsArrayBuffer(file)
  })
}

function validateSkillFilePath(path: string) {
  const parts = path.split('/')
  if (
    !path
    || path.length > 240
    || path.startsWith('/')
    || path.includes('\\')
    || /[\u0000-\u001f\u007f]/.test(path)
    || parts.some(part => !part || part === '.' || part === '..')
  ) {
    throw new Error(`Skill 源文件包含无效文件路径：${path || '空路径'}`)
  }
}

function inferSkillFileMediaType(path: string) {
  const extension = path.split('.').pop()?.toLowerCase()
  if (extension === 'md' || extension === 'mdx') return 'text/markdown'
  if (['txt', 'py', 'sh', 'js', 'ts', 'tsx', 'jsx', 'json', 'yaml', 'yml', 'toml', 'csv', 'svg', 'html', 'css'].includes(extension || '')) {
    return extension === 'json'
      ? 'application/json'
      : extension === 'svg'
        ? 'image/svg+xml'
        : 'text/plain'
  }
  if (extension === 'png') return 'image/png'
  if (extension === 'jpg' || extension === 'jpeg') return 'image/jpeg'
  if (extension === 'gif') return 'image/gif'
  if (extension === 'pdf') return 'application/pdf'
  return 'application/octet-stream'
}

function bytesToBase64(bytes: Uint8Array) {
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
  }
  return btoa(binary)
}

export function skillFileBytes(file: CreationSkillPackageFile) {
  const binary = atob(file.contentBase64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}

export function skillFileText(file: CreationSkillPackageFile) {
  if (!isTextSkillFile(file)) return null
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(skillFileBytes(file))
  } catch {
    return null
  }
}

export function isTextSkillFile(file: CreationSkillPackageFile) {
  return file.mediaType.startsWith('text/')
    || ['application/json', 'application/yaml', 'application/x-yaml', 'image/svg+xml'].includes(file.mediaType)
    || /\.(?:md|mdx|txt|py|sh|js|jsx|ts|tsx|json|ya?ml|toml|csv|svg|html|css)$/i.test(file.path)
}

export function agentSkillPackageFiles(
  skill: CreationSkillContent & {
    id: string | number
    clientSkillKey?: string
    title: string
    summary: string
    packageFiles?: CreationSkillPackageFile[]
  },
): CreationSkillPackageFile[] {
  if (skill.packageFiles?.length) return [...skill.packageFiles]
  const name = codexSkillName(skill.clientSkillKey || skill.title, skill.id)
  const description = ([
    skill.skillDescription.purpose,
    skill.skillDescription.documentTypes.length
      ? `用于创作：${skill.skillDescription.documentTypes.join('、')}。`
      : '',
    skill.skillDescription.problems.length
      ? `适合解决：${skill.skillDescription.problems.join('；')}。`
      : '',
  ].filter(Boolean).join(' ') || skill.summary).slice(0, 1_024)
  const markdown = `---
name: ${name}
description: ${JSON.stringify(description)}
---

# ${skill.title}

## 能力描述

${skill.skillDescription.purpose}

- 适用文档：${skill.skillDescription.documentTypes.join('；') || '由任务上下文确定'}
- 解决问题：${skill.skillDescription.problems.join('；') || '由任务上下文确定'}
- 涉及领域：${skill.skillDescription.domains.join('；') || '不限特定领域'}
- 目标产物：${skill.skillDescription.deliverables.join('；') || skill.summary}

## 执行工作流

${skill.executionSteps.map((step, index) => `### ${index + 1}. ${step.title}

${step.objective}

${step.output.trim() ? `- 产出：${step.output}\n` : ''}- Agent：${step.agents.join('、') || '无'}
- Skill：${step.skills.join('、') || '无'}
- Tool：${step.tools.join('、') || '无'}`).join('\n\n')}

## References

- Read \`references/memorybread-creation.json\` for the complete structured writing profile.
${skill.exampleDocument.trim() ? '- Read `references/example.md` when a complete output example is useful.' : ''}

Follow the user's facts and constraints. Treat reference examples as style guidance, never as facts for the new document.
`
  const profile = JSON.stringify({
    schema_version: 1,
    kind: 'memorybread.creation-profile',
    content: {
      skill_description: {
        purpose: skill.skillDescription.purpose,
        document_types: skill.skillDescription.documentTypes,
        problems: skill.skillDescription.problems,
        domains: skill.skillDescription.domains,
        deliverables: skill.skillDescription.deliverables,
      },
      execution_steps: skill.executionSteps.map(step => ({
        id: step.id,
        title: step.title,
        objective: step.objective,
        output: step.output,
        agents: step.agents,
        skills: step.skills,
        tools: step.tools,
        retain_webpage_screenshot: step.retainWebpageScreenshot === true,
      })),
      common_titles: skill.commonTitles,
      title_style: skill.titleStyle,
      text_style: skill.textStyle,
      diagram_style: skill.diagramStyle,
      writing_guidelines: skill.writingGuidelines,
      distinctive_sections: skill.distinctiveSections || [],
      section_headings: {
        common_titles: skill.sectionHeadings.commonTitles,
        title_style: skill.sectionHeadings.titleStyle,
        text_style: skill.sectionHeadings.textStyle,
        diagram_style: skill.sectionHeadings.diagramStyle,
        writing_guidelines: skill.sectionHeadings.writingGuidelines,
      },
      field_examples: {
        common_titles: skill.fieldExamples.commonTitles,
        title_style: skill.fieldExamples.titleStyle,
        text_style: skill.fieldExamples.textStyle,
        diagram_style: skill.fieldExamples.diagramStyle,
        writing_guidelines: skill.fieldExamples.writingGuidelines,
      },
      example_document: '',
    },
  }, null, 2)
  const files = [
    textSkillPackageFile('SKILL.md', 'text/markdown', markdown),
    textSkillPackageFile('references/memorybread-creation.json', 'application/json', profile),
  ]
  if (skill.exampleDocument.trim()) {
    files.push(textSkillPackageFile('references/example.md', 'text/markdown', skill.exampleDocument.trim()))
  }
  return files
}

/** 兼容旧调用方；生成物同时兼容 Codex 与 Claude Code。 */
export const codexSkillPackageFiles = agentSkillPackageFiles

function textSkillPackageFile(path: string, mediaType: string, content: string): CreationSkillPackageFile {
  const bytes = new TextEncoder().encode(content)
  return { path, mediaType, contentBase64: bytesToBase64(bytes), sizeBytes: bytes.byteLength }
}

export function agentSkillZipBytes(
  name: string,
  files: CreationSkillPackageFile[],
) {
  const skillMarkdown = files.find(file => file.path === 'SKILL.md')
  const root = skillMarkdown
    ? parseCodexSkillMarkdown(skillFileText(skillMarkdown) || '').name
    : codexSkillName(name, 'skill')
  const entries = Object.fromEntries(files.map(file => [`${root}/${file.path}`, skillFileBytes(file)]))
  return arrayBufferFromBytes(zipSync(entries, { level: 6 }))
}

function arrayBufferFromBytes(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer
}

function codexSkillName(value: string, id: string | number) {
  const normalized = value
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 64)
    .replace(/-$/g, '')
  if (normalized) return normalized
  const suffix = String(id)
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 44)
    .replace(/-$/g, '')
  return `memorybread-skill-${suffix || 'portable'}`
}

const inFlightCreationSkillAnalyses = new Map<string, Promise<CreationSkillAnalysis>>()

function distinctCreationSkillItems(values: unknown[], maximum: number) {
  const seen = new Set<string>()
  const result: string[] = []
  for (const value of values) {
    const text = coerceCreationSkillStringItem(value)
    const fingerprint = text.replace(/[^\p{L}\p{N}]+/gu, '')
    if (!fingerprint || seen.has(fingerprint)) continue
    seen.add(fingerprint)
    result.push(text)
    if (result.length >= maximum) break
  }
  return result
}

function coerceCreationSkillStringItem(value: unknown, key = '') {
  if (typeof value === 'string') {
    const text = value.trim()
    if (/^\{[\s\S]*\}$/.test(text)) {
      const read = (name: string) => text.match(
        new RegExp(`['"]${name}['"]\\s*:\\s*['"]([^'"]+)['"]`, 'i'),
      )?.[1]?.trim() || ''
      if (key === 'common_titles' || key === 'title_style') {
        const level = read('level')
        const pattern = read('pattern')
        if (pattern) return `${level ? `${level}：` : ''}采用“${pattern}”的标题骨架`
      }
    }
    return text
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  const item = value as Record<string, unknown>
  const first = (...keys: string[]) => {
    for (const candidateKey of keys) {
      const candidate = item[candidateKey]
      if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
    }
    return ''
  }
  if (key === 'common_titles' || key === 'title_style') {
    const level = first('level', '层级', 'position', '位置')
    const pattern = first('pattern', '骨架', 'rule', '规则', 'title', '标题')
    const boundary = first('boundary', 'usage', '适用位置', '说明')
    return pattern
      ? `${level ? `${level}：` : ''}采用“${pattern}”的标题骨架${boundary ? `；${boundary}` : ''}`
      : ''
  }
  if (key === 'writing_guidelines') {
    const phrase = first('phrase', 'term', 'wording', '短语', '话术')
    const usage = first('role', 'usage', 'effect', '作用', '说明')
    return phrase && usage ? `习惯用“${phrase}”${usage}` : phrase || usage
  }
  return first('example', 'text', 'content', 'value', '示例')
}

function compactCreationSkillPlaceholders(value: unknown) {
  return String(value || '')
    .replace(/(?:目标对象[\s·—_:：/\\-]*){2,}/gu, '目标对象')
    .replace(/(?:相关角色[\s·—_:：/\\-]*){2,}/gu, '相关角色')
    .replace(/([\u4e00-\u9fff])\s+([\u4e00-\u9fff])/gu, '$1$2')
    .replace(/\s{2,}/g, ' ')
    .replace(/^[\s：:·—_\/\\-]+|[\s：:·—_\/\\-]+$/g, '')
}

function repairCreationSkillHeadingExamples(values: unknown[], fallback: string[]) {
  const repaired = values
    .map(value => fictionalizeCreationSkillHeadingExample(
      coerceCreationSkillStringItem(value, 'common_titles'),
    ))
    .filter(isCompleteCreationSkillHeadingExample)
  return distinctCreationSkillItems([...fallback, ...repaired], 6)
}

function fictionalizeCreationSkillHeadingExample(value: unknown) {
  let text = compactCreationSkillPlaceholders(value)
    .replace(/目标对象/gu, '协作工作台')
    .replace(/相关团队/gu, '协作团队')
  if (/^从.{1,12}(?:视角|角度|层面)看[，,:：]?协作工作台$/u.test(text)) {
    text = `${text}的角色与边界`
  }
  if (/^[A-Za-z][A-Za-z0-9_-]*\s*[：:]\s*协作工作台$/u.test(text)) {
    text = `${text}的调度边界`
  }
  return text
}

function isCompleteCreationSkillHeadingExample(value: string) {
  const text = value.trim()
  if (text.replace(/[^\p{L}\p{N}]+/gu, '').length < 4) return false
  if (/(?:目标对象|相关角色|相关团队)[的之]?$/u.test(text)) return false
  if (/(?:看|关于|针对|面向)[，,:：]?$/u.test(text)) return false
  return !/^(?:目标对象|相关角色|相关团队|协作工作台)+$/u.test(text)
}

function mergeCreationSkillTextStyle(primary: unknown, fallback: string) {
  const text = String(primary || '').trim()
  if (!text) return fallback
  if (text.length >= 400 || text.includes(fallback)) return text
  return `${text}\n\n执行配方：${fallback}`
}

function mergeCreationSkillDiagramStyle(primary: unknown, fallback: string) {
  const text = String(primary || '').trim()
  if (!text) return fallback
  if (text.length >= 400 || text.includes(fallback)) return text
  return `${text}\n\n完整配图配方：${fallback}`
}

function mergeCreationSkillWritingGuidelines(primary: unknown, fallback: string[]) {
  const modelItems = Array.isArray(primary)
    ? primary
      .map(item => coerceCreationSkillStringItem(item, 'writing_guidelines'))
      .filter(Boolean)
      .slice(0, 5)
    : []
  return distinctCreationSkillItems([...modelItems, ...fallback], 8)
}

function isCompleteCreationSkillExampleDocument(value: unknown) {
  const text = String(value || '').trim()
  if (text.length < 1000) return false
  if ((text.match(/^#\s+\S/gm) || []).length !== 1) return false
  if ((text.match(/^##\s+\S/gm) || []).length < 6) return false
  const bodyBlocks = text
    .split(/\n\s*\n/)
    .map(block => block.trim())
    .filter(block => block && !block.startsWith('#') && block.length >= 40)
  return bodyBlocks.length >= 8
}

const creationSkillAnalysisRequest = (source: CreationSkillSource): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    source_kind: source.kind,
    source_id: source.id,
    document_title: source.title,
    document_content: source.content,
    doc_type: source.docType,
  }),
})

function mapCreationSkillAnalysis(data: any, source: CreationSkillSource): CreationSkillAnalysis {
  if (!data?.section_headings || !data?.field_examples || !String(data?.example_document || '').trim()) {
    throw new Error('技能分析结果格式不完整')
  }
  const fallback = buildClientCreationSkillFallback(source)
  const fieldExamples = mapFieldExamples(data.field_examples)
  const headingExamples = repairCreationSkillHeadingExamples(
    fieldExamples.commonTitles,
    fallback.fieldExamples.commonTitles,
  )
  fieldExamples.commonTitles = headingExamples
  fieldExamples.titleStyle = [...headingExamples]
  const commonTitles = distinctCreationSkillItems([
    ...(Array.isArray(data.common_titles)
      ? data.common_titles.map((item: unknown) => coerceCreationSkillStringItem(item, 'common_titles'))
      : []),
    ...fallback.commonTitles,
  ], 8)
  return {
    title: normalizeCreationSkillTitle(data.title, source),
    summary: data.summary,
    skillDescription: mapSkillDescription(
      data.skill_description,
      normalizeCreationSkillTitle(data.title, source),
      String(data.summary || fallback.summary),
      source.docType,
      fallback.skillDescription.domains,
    ),
    executionSteps: mapExecutionSteps(
      data.execution_steps,
      normalizeCreationSkillTitle(data.title, source),
      `${source.docType}\n${source.title}\n${source.content.slice(0, 8_000)}`,
    ),
    commonTitles,
    titleStyle: commonTitles.join('；'),
    textStyle: mergeCreationSkillTextStyle(data.text_style, fallback.textStyle),
    diagramStyle: mergeCreationSkillDiagramStyle(data.diagram_style, fallback.diagramStyle),
    writingGuidelines: mergeCreationSkillWritingGuidelines(
      data.writing_guidelines,
      fallback.writingGuidelines,
    ),
    distinctiveSections: mapDistinctiveSections(
      data.distinctive_sections,
      fallback.distinctiveSections || [],
    ),
    sectionHeadings: mapSectionHeadings(data.section_headings),
    fieldExamples,
    exampleDocument: isCompleteCreationSkillExampleDocument(data.example_document)
      ? data.example_document.trim()
      : fallback.exampleDocument,
    suggestedCategoryKeywords: data.suggested_category_keywords || [],
    analysisMode: data.analysis_mode || 'local_model',
    fallbackReason: typeof data.fallback_reason === 'string'
      ? data.fallback_reason
      : undefined,
  }
}

const creationSkillFallbackReasonForCode = (code: string) => (
  code === 'INVALID_CREATION_SKILL_ANALYSIS' || code === 'INCOMPLETE_CREATION_SKILL_ANALYSIS'
    ? 'invalid_service_response'
    : 'analysis_request_failed'
)

async function requestCreationSkillAnalysisLegacy(
  apiBaseUrl: string,
  source: CreationSkillSource,
): Promise<CreationSkillAnalysis> {
  const requestInit = creationSkillAnalysisRequest(source)

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response: Response
    try {
      response = await fetchWithLocalhostFallback(
        `${apiBaseUrl}/api/creation/skills/analyze`,
        requestInit,
      )
    } catch (error) {
      if (attempt === 0 && isTransientCreationSkillNetworkError(error)) {
        await waitForCreationSkillAnalysisRetry()
        continue
      }
      return buildClientCreationSkillFallback(source, 'analysis_request_failed')
    }

    if (!response.ok) {
      const failure = await readCreationSkillAnalysisError(response)
      if (
        attempt === 0
        && RETRYABLE_CREATION_SKILL_ANALYSIS_ERRORS.has(failure.code)
      ) {
        await waitForCreationSkillAnalysisRetry()
        continue
      }
      return buildClientCreationSkillFallback(source, 'analysis_request_failed')
    }

    let data: any
    try {
      data = await response.json()
      return mapCreationSkillAnalysis(data, source)
    } catch {
      return buildClientCreationSkillFallback(source, 'invalid_service_response')
    }
  }

  return buildClientCreationSkillFallback(source, 'analysis_request_failed')
}

async function requestCreationSkillAnalysis(
  apiBaseUrl: string,
  source: CreationSkillSource,
): Promise<CreationSkillAnalysis> {
  const deadline = Date.now() + CREATION_SKILL_ANALYSIS_JOB_TIMEOUT_MS
  jobAttempts: for (let attempt = 0; attempt < 2; attempt += 1) {
    let createResponse: Response
    try {
      createResponse = await fetchWithLocalhostFallback(
        `${apiBaseUrl}/api/creation/skills/analyze/jobs`,
        creationSkillAnalysisRequest(source),
      )
    } catch (error) {
      if (attempt === 0 && isTransientCreationSkillNetworkError(error)) {
        await waitForCreationSkillAnalysisRetry()
        continue
      }
      return requestCreationSkillAnalysisLegacy(apiBaseUrl, source)
    }

    // 旧 Core Engine 尚未提供异步任务时继续使用原同步接口。
    if (createResponse.status === 404 || createResponse.status === 405) {
      return requestCreationSkillAnalysisLegacy(apiBaseUrl, source)
    }
    if (!createResponse.ok) {
      const failure = await readCreationSkillAnalysisError(createResponse)
      if (
        attempt === 0
        && RETRYABLE_CREATION_SKILL_ANALYSIS_ERRORS.has(failure.code)
      ) {
        await waitForCreationSkillAnalysisRetry()
        continue
      }
      return buildClientCreationSkillFallback(
        source,
        creationSkillFallbackReasonForCode(failure.code),
      )
    }

    const created = await createResponse.json().catch(() => null)
    const jobId = String(created?.job_id || '').trim()
    if (!jobId) return buildClientCreationSkillFallback(source, 'invalid_service_response')

    let pollFailures = 0
    while (Date.now() < deadline) {
      await waitForCreationSkillAnalysisRetry()
      let statusResponse: Response
      try {
        statusResponse = await fetchWithLocalhostFallback(
          `${apiBaseUrl}/api/creation/skills/analyze/jobs/${encodeURIComponent(jobId)}`,
        )
        pollFailures = 0
      } catch (error) {
        pollFailures += 1
        if (
          pollFailures < CREATION_SKILL_ANALYSIS_MAX_POLL_FAILURES
          && isTransientCreationSkillNetworkError(error)
        ) {
          continue
        }
        return buildClientCreationSkillFallback(source, 'analysis_request_failed')
      }

      if (!statusResponse.ok) {
        const failure = await readCreationSkillAnalysisError(statusResponse)
        if (
          attempt === 0
          && RETRYABLE_CREATION_SKILL_ANALYSIS_ERRORS.has(failure.code)
        ) {
          await waitForCreationSkillAnalysisRetry()
          continue jobAttempts
        }
        return buildClientCreationSkillFallback(
          source,
          creationSkillFallbackReasonForCode(failure.code),
        )
      }
      const job = await statusResponse.json().catch(() => null)
      if (job?.status === 'succeeded') {
        try {
          return mapCreationSkillAnalysis(job.result, source)
        } catch {
          return buildClientCreationSkillFallback(source, 'invalid_service_response')
        }
      }
      if (job?.status === 'failed') {
        const code = String(job.error_code || '')
        if (
          attempt === 0
          && RETRYABLE_CREATION_SKILL_ANALYSIS_ERRORS.has(code)
        ) {
          await waitForCreationSkillAnalysisRetry()
          continue jobAttempts
        }
        return buildClientCreationSkillFallback(
          source,
          creationSkillFallbackReasonForCode(code),
        )
      }
    }
    return buildClientCreationSkillFallback(source, 'analysis_request_failed')
  }

  return buildClientCreationSkillFallback(source, 'analysis_request_failed')
}

export function analyzeCreationSkill(
  apiBaseUrl: string,
  source: CreationSkillSource,
): Promise<CreationSkillAnalysis> {
  const requestKey = JSON.stringify([
    apiBaseUrl,
    source.kind,
    source.id,
    source.title,
    source.docType,
    source.content,
  ])
  const inFlight = inFlightCreationSkillAnalyses.get(requestKey)
  if (inFlight) return inFlight

  let request: Promise<CreationSkillAnalysis>
  request = requestCreationSkillAnalysis(apiBaseUrl, source).finally(() => {
    if (inFlightCreationSkillAnalyses.get(requestKey) === request) {
      inFlightCreationSkillAnalyses.delete(requestKey)
    }
  })
  inFlightCreationSkillAnalyses.set(requestKey, request)
  return request
}

export async function listLocalCreationSkills(
  apiBaseUrl: string,
  query: LocalCreationSkillQuery = {},
): Promise<LocalCreationSkill[]> {
  const search = new URLSearchParams()
  if (query.sourceKind && query.sourceId) {
    search.set('source_kind', query.sourceKind)
    search.set('source_id', query.sourceId)
  }
  if (query.installed !== undefined) search.set('installed', String(query.installed))
  const suffix = search.toString()
  const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/skills${suffix ? `?${suffix}` : ''}`)
  if (!response.ok) throw new Error(await parseError(response, '读取技能失败'))
  return (await response.json()).map(mapLocalSkill)
}

export async function saveLocalCreationSkill(
  apiBaseUrl: string,
  input: Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'>,
  id?: number,
): Promise<LocalCreationSkill> {
  const preservePackage = (input.sourceKind === 'imported' || input.sourceKind === 'market')
    && Boolean(input.packageFiles?.length)
  const normalizedInput = preservePackage
    ? input
    : {
      ...input,
      packageFiles: agentSkillPackageFiles({
        ...input,
        id: id || input.clientSkillKey,
        packageFiles: [],
      }),
    }
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/creation/skills${id ? `/${id}` : ''}`,
    {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializeLocalSkill(normalizedInput)),
    },
  )
  if (!response.ok) throw new Error(await parseError(response, '保存技能失败'))
  return mapLocalSkill(await response.json())
}

export async function deleteLocalCreationSkill(apiBaseUrl: string, id: number): Promise<void> {
  const response = await fetchWithLocalhostFallback(`${apiBaseUrl}/api/creation/skills/${id}`, { method: 'DELETE' })
  if (!response.ok && response.status !== 204) throw new Error(await parseError(response, '删除技能失败'))
}

export async function fetchCreationSkillCategories(adminApiBaseUrl: string): Promise<CreationSkillCategory[]> {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), 3500)
  try {
    const response = await fetch(`${adminApiBaseUrl}/v1/creation-skill-categories`, {
      headers: serviceEnvironmentHeaders(),
      signal: controller.signal,
    })
    if (!response.ok) throw new Error(await parseError(response, '读取创作类目失败'))
    const payload = await response.json()
    const categories = (payload.data || []).map((item: any) => ({
      id: item.id,
      key: item.key,
      name: item.name,
      level: item.level,
      parentId: item.parent_id,
      sortOrder: item.sort_order,
    })) as CreationSkillCategory[]
    return categories.some(item => item.level === 4)
      ? categories
      : OFFLINE_CREATION_SKILL_CATEGORIES
  } catch {
    return OFFLINE_CREATION_SKILL_CATEGORIES
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

export async function searchCreationSkillMarket(
  adminApiBaseUrl: string,
  query: CreationSkillMarketQuery = {},
): Promise<CreationSkillMarketPage> {
  const search = new URLSearchParams()
  if (query.query?.trim()) search.set('q', query.query.trim())
  if (query.categoryId) search.set('category_id', query.categoryId)
  search.set('limit', String(query.limit ?? 24))
  search.set('offset', String(query.offset ?? 0))
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(
    () => controller.abort(),
    CREATION_SKILL_MARKET_TIMEOUT_MS,
  )
  try {
    const response = await fetch(`${adminApiBaseUrl}/v1/creation-skills?${search}`, {
      headers: serviceEnvironmentHeaders(),
      signal: controller.signal,
    })
    if (!response.ok) throw new Error(await parseError(response, '读取技能市场失败'))
    const payload = await response.json()
    return {
      items: Array.isArray(payload?.data?.items)
        ? payload.data.items.map(mapMarketSkill)
        : [],
      total: Number(payload?.data?.total || 0),
      limit: Number(payload?.data?.limit || query.limit || 24),
      offset: Number(payload?.data?.offset || query.offset || 0),
    }
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error('技能市场请求超时，请稍后重试')
    }
    throw error
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

export async function fetchCreationSkillMarketDetail(
  adminApiBaseUrl: string,
  id: string,
): Promise<CreationSkillMarketItem> {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), CREATION_SKILL_MARKET_TIMEOUT_MS)
  try {
    const response = await fetch(`${adminApiBaseUrl}/v1/creation-skills/${encodeURIComponent(id)}`, {
      headers: serviceEnvironmentHeaders(),
      signal: controller.signal,
    })
    if (!response.ok) throw new Error(await parseError(response, '读取技能详情失败'))
    const payload = await response.json()
    return mapMarketSkill(payload?.data || {})
  } catch (error) {
    if (controller.signal.aborted) throw new Error('技能详情请求超时，请稍后重试')
    throw error
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

export function marketCreationSkillToLocalInput(
  skill: CreationSkillMarketItem,
): Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'> {
  return {
    clientSkillKey: `market-${skill.id}`,
    cloudSkillId: skill.id,
    sourceKind: 'market',
    sourceId: skill.id,
    title: skill.title,
    summary: skill.summary,
    categoryId: skill.categoryId,
    skillDescription: {
      ...skill.skillDescription,
      documentTypes: [...skill.skillDescription.documentTypes],
      problems: [...skill.skillDescription.problems],
      domains: [...skill.skillDescription.domains],
      deliverables: [...skill.skillDescription.deliverables],
    },
    executionSteps: skill.executionSteps.map(step => ({
      ...step,
      agents: [...step.agents],
      skills: [...step.skills],
      tools: [...step.tools],
      retainWebpageScreenshot: step.retainWebpageScreenshot === true,
    })),
    commonTitles: [...skill.commonTitles],
    titleStyle: skill.titleStyle,
    textStyle: skill.textStyle,
    diagramStyle: skill.diagramStyle,
    writingGuidelines: [...skill.writingGuidelines],
    distinctiveSections: [...(skill.distinctiveSections || [])].map(section => ({
      ...section,
      examples: [...section.examples],
    })),
    sectionHeadings: { ...skill.sectionHeadings },
    fieldExamples: cloneFieldExamples(skill.fieldExamples),
    exampleDocument: skill.exampleDocument,
    status: 'saved',
    installed: true,
    published: false,
    packageFiles: skill.packageFiles.map(file => ({ ...file })),
  }
}

export async function publishCreationSkill(
  adminApiBaseUrl: string,
  token: string,
  skill: Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'>,
  published: boolean,
): Promise<{ id: string; published: boolean }> {
  if (!skill.categoryId) throw new Error('技能当前为“私有”类目，请先选择非私有的创作类目再发布到市场')
  if (!published && !skill.cloudSkillId) throw new Error('未发布的本地技能草稿不会上传')
  const response = await fetch(
    `${adminApiBaseUrl}/v1/creation-skills${skill.cloudSkillId ? `/${skill.cloudSkillId}` : ''}`,
    {
      method: skill.cloudSkillId ? 'PUT' : 'POST',
      headers: {
        ...serviceEnvironmentHeaders(),
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        client_skill_key: skill.clientSkillKey,
        title: skill.title,
        summary: skill.summary,
        category_id: skill.categoryId,
        content: {
          skill_description: {
            purpose: skill.skillDescription.purpose,
            document_types: skill.skillDescription.documentTypes,
            problems: skill.skillDescription.problems,
            domains: skill.skillDescription.domains,
            deliverables: skill.skillDescription.deliverables,
          },
          execution_steps: skill.executionSteps,
          common_titles: skill.commonTitles,
          title_style: skill.titleStyle,
          text_style: skill.textStyle,
          diagram_style: skill.diagramStyle,
          writing_guidelines: skill.writingGuidelines,
          distinctive_sections: skill.distinctiveSections || [],
          section_headings: {
            common_titles: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.commonTitles,
            title_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.titleStyle,
            text_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.textStyle,
            diagram_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.diagramStyle,
            writing_guidelines: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.writingGuidelines,
          },
          field_examples: {
            common_titles: skill.fieldExamples.commonTitles,
            title_style: skill.fieldExamples.titleStyle,
            text_style: skill.fieldExamples.textStyle,
            diagram_style: skill.fieldExamples.diagramStyle,
            writing_guidelines: skill.fieldExamples.writingGuidelines,
          },
          example_document: skill.exampleDocument,
        },
        package_files: agentSkillPackageFiles({
          ...skill,
          id: skill.clientSkillKey,
        }).map(file => ({
          path: file.path,
          media_type: file.mediaType,
          content_base64: file.contentBase64,
          size_bytes: file.sizeBytes,
        })),
        published,
      }),
    },
  )
  if (!response.ok) throw new Error(await parseError(response, published ? '发布技能失败' : '下架技能失败'))
  const payload = await response.json()
  return { id: payload.data.id, published: payload.data.published }
}

export function categoryPathFor(categories: CreationSkillCategory[], leafId?: string | null) {
  if (!leafId) return []
  const byId = new Map(categories.map(item => [item.id, item]))
  const path: CreationSkillCategory[] = []
  let cursor = byId.get(leafId)
  while (cursor) {
    path.unshift(cursor)
    cursor = cursor.parentId ? byId.get(cursor.parentId) : undefined
  }
  return path
}

export function creationSkillCategoryOptions(
  categories: CreationSkillCategory[],
): CreationSkillCategoryOption[] {
  const categoryIds = new Set(categories.map(category => category.id))
  const childrenByParent = new Map<string | null, CreationSkillCategory[]>()
  const originalIndex = new Map(categories.map((category, index) => [category.id, index]))
  const compareCategories = (left: CreationSkillCategory, right: CreationSkillCategory) =>
    left.sortOrder - right.sortOrder
    || left.name.localeCompare(right.name, 'zh-CN')
    || (originalIndex.get(left.id) || 0) - (originalIndex.get(right.id) || 0)

  categories.forEach(category => {
    const parentId = category.parentId && categoryIds.has(category.parentId)
      ? category.parentId
      : null
    const siblings = childrenByParent.get(parentId) || []
    siblings.push(category)
    childrenByParent.set(parentId, siblings)
  })
  childrenByParent.forEach(children => children.sort(compareCategories))

  const options: CreationSkillCategoryOption[] = []
  const visited = new Set<string>()
  const appendCategory = (category: CreationSkillCategory, depth: number) => {
    if (visited.has(category.id)) return
    visited.add(category.id)
    options.push({ ...category, depth })
    const children = childrenByParent.get(category.id) || []
    children.forEach(child => {
      appendCategory(child, depth + 1)
    })
  }

  const roots = childrenByParent.get(null) || []
  roots.forEach(category => appendCategory(category, 0))
  const remainingCategories = [...categories].sort(compareCategories)
  remainingCategories.forEach(category => {
    appendCategory(category, Math.max(0, category.level - 1))
  })
  return options
}

export function suggestCreationSkillCategory(
  categories: CreationSkillCategory[],
  analysis: CreationSkillAnalysis,
  source?: CreationSkillSource | null,
): CreationSkillCategory | undefined {
  const text = [
    source?.title,
    source?.docType,
    source?.content.slice(0, 8_000),
    analysis.title,
    analysis.summary,
    ...analysis.commonTitles,
    ...analysis.suggestedCategoryKeywords,
  ].filter(Boolean).join('\n').toLowerCase()
  const leaves = categories.filter(item => item.level === 4)
  const scoringRules: Array<{ pattern: RegExp; keys: string[]; score: number }> = [
    { pattern: /电商|零售|商品|订单|购物|履约|交易链路/, keys: ['internet-ecommerce', 'ecommerce-'], score: 14 },
    { pattern: /企业服务|saas|b端|后台|平台|系统|软件/, keys: ['internet-enterprise-service', 'enterprise-'], score: 8 },
    { pattern: /银行|支付|信贷|金融|风控/, keys: ['finance-banking-payment', 'bank-'], score: 14 },
    { pattern: /保险|保单|理赔|精算/, keys: ['finance-insurance', 'insurance-'], score: 16 },
    { pattern: /智能制造|产线|工艺|工业软件/, keys: ['manufacturing-smart', 'smart-'], score: 15 },
    { pattern: /消费品|质量策划|工业设计/, keys: ['manufacturing-consumer', 'consumer-'], score: 12 },
    { pattern: /咨询|行业研究|项目建议书/, keys: ['professional-consulting', 'consulting-'], score: 14 },
    { pattern: /品牌|内容策划|视觉规范/, keys: ['professional-brand-media', 'brand-'], score: 14 },
    { pattern: /云计算|数据平台|人工智能|大模型|机器学习/, keys: ['internet-cloud-data-ai'], score: 16 },
    { pattern: /网络安全|信息安全|威胁建模|安全事件/, keys: ['internet-cybersecurity'], score: 16 },
    { pattern: /证券|基金|资管|投资研究|估值/, keys: ['finance-securities-fund'], score: 16 },
    { pattern: /汽车|零部件|半导体|电子|机械|装备|航空航天/, keys: ['manufacturing-'], score: 13 },
    { pattern: /农业|种植|畜牧|养殖|林业|渔业|水产/, keys: ['agriculture-forestry-fishery'], score: 16 },
    { pattern: /建筑|施工|工程造价|房地产|物业|设施管理/, keys: ['construction-realestate'], score: 16 },
    { pattern: /煤矿|矿山|石油|天然气|电网|电力|新能源|储能/, keys: ['energy-mining'], score: 16 },
    { pattern: /公路|铁路|航空运输|航运|港口|仓储|物流|快递|配送/, keys: ['transport-logistics'], score: 16 },
    { pattern: /商超|百货|批发|进出口|跨境贸易|经销/, keys: ['wholesale-retail-trade'], score: 16 },
    { pattern: /课程|教学|教研|学校|高校|职业教育|培训/, keys: ['education-training'], score: 16 },
    { pattern: /医院|临床|护理|药品|生物科技|医疗器械|康养|养老/, keys: ['healthcare-life-science'], score: 16 },
    { pattern: /新闻|出版|影视|音视频|广告|公关|赛事|博物馆|展览/, keys: ['culture-media-sports'], score: 16 },
    { pattern: /旅游|旅行|酒店|餐饮|景区|度假/, keys: ['tourism-hospitality-catering'], score: 16 },
    { pattern: /政策|政府|事业单位|社区服务|应急管理|公共安全/, keys: ['government-public-service'], score: 16 },
    { pattern: /电信|通信网络|卫星通信|数据中心/, keys: ['telecom-communication'], score: 16 },
    { pattern: /环保|污水|固废|环卫|环境治理|供水|燃气|供热/, keys: ['environment-utilities'], score: 16 },
    { pattern: /科研|实验室|检验检测|认证审核/, keys: ['research-testing'], score: 16 },
    { pattern: /基金会|慈善|公益|行业协会|商会|社会工作/, keys: ['social-nonprofit'], score: 16 },
    { pattern: /家政|美容美发|维修服务|婚庆|宠物服务/, keys: ['life-personal-service'], score: 16 },
    { pattern: /企业战略|经营管理|财务预算|人才发展|市场营销|采购|数据治理/, keys: ['corporate-functions'], score: 12 },
    { pattern: /技术架构|总体架构|系统边界|关键链路|组件设计/, keys: ['architect', 'architecture'], score: 24 },
    { pattern: /接口设计|\bapi\b/, keys: ['software-engineer', 'api-design'], score: 22 },
    { pattern: /技术设计|软件设计|实现方案/, keys: ['software-engineer', 'technical-design', 'system-design'], score: 18 },
    { pattern: /产品需求|\bprd\b/, keys: ['product-manager', 'prd'], score: 24 },
    { pattern: /产品设计|产品方案/, keys: ['product-manager', 'product-design'], score: 20 },
    { pattern: /\bui\b|界面设计|交互稿/, keys: ['designer', 'ui-design'], score: 24 },
    { pattern: /\bux\b|用户体验/, keys: ['designer', 'ux-design'], score: 24 },
    { pattern: /运营方案|活动运营|增长运营/, keys: ['operator', 'operation-plan'], score: 20 },
    { pattern: /风险策略|风控策略/, keys: ['risk-manager', 'risk-policy'], score: 22 },
    { pattern: /数据分析|指标分析/, keys: ['data-analyst', 'data-analysis'], score: 20 },
    { pattern: /实施方案|客户交付/, keys: ['customer-success', 'implementation-plan'], score: 20 },
  ]

  const scored = leaves.map(item => {
    const path = categoryPathFor(categories, item.id)
    const keys = path.map(part => part.key).join(' ')
    let score = 0
    for (const part of path) {
      if (text.includes(part.name.toLowerCase())) score += part.level * 7
    }
    for (const rule of scoringRules) {
      if (rule.pattern.test(text) && rule.keys.some(key => keys.includes(key))) score += rule.score
    }
    return { item, score }
  })
  scored.sort((left, right) => right.score - left.score || left.item.sortOrder - right.item.sortOrder)
  return scored[0]?.score > 0 ? scored[0].item : undefined
}

// 输入时自动推荐已下线：输入过程中不再做任何召回计算，只有用户显式 @ 才引入 Skill。
export function matchCreationSkills(
  prompt: string,
  skills: LocalCreationSkill[],
  _categories: CreationSkillCategory[] = OFFLINE_CREATION_SKILL_CATEGORIES,
  limit = 3,
): MatchedCreationSkill[] {
  if (!prompt.trim()) return []
  return skills
    .filter(skill => skill.status === 'saved' && skill.installed)
    .filter(skill => prompt.includes(`@${skill.title}`))
    .slice(0, Math.max(1, limit))
    .map(skill => ({ skill, reason: 'mentioned' as const, score: 1_000 }))
}

// 调用 sidecar 模型路由（与 Tool/Agent 路由同构：自描述渐进式披露 + 模型决策 + 白名单）。
async function requestExecutionSkillRoute(
  apiBaseUrl: string,
  prompt: string,
  skills: LocalCreationSkill[],
  signal?: AbortSignal,
): Promise<{ matches: MatchedCreationSkill[]; source: 'model' | 'fallback'; reasoning: string }> {
  const candidates = skills.filter(skill => skill.status === 'saved' && skill.installed)
  const trimmed = prompt.trim()
  if (!trimmed || !candidates.length) {
    return { matches: [], source: 'fallback', reasoning: '' }
  }
  const response = await fetchWithLocalhostFallback(
    `${apiBaseUrl}/api/creation/skills/match`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: trimmed,
        skills: candidates.map(skill => ({
          id: skill.id,
          title: skill.title,
          summary: skill.summary,
          skill_description: {
            purpose: skill.skillDescription.purpose,
            document_types: skill.skillDescription.documentTypes,
            problems: skill.skillDescription.problems,
            domains: skill.skillDescription.domains,
            deliverables: skill.skillDescription.deliverables,
          },
        })),
      }),
      signal,
    },
  )
  if (!response.ok) throw new Error(await parseError(response, '模型技能召回失败'))
  const payload = await response.json()
  const byId = new Map(candidates.map(skill => [skill.id, skill]))
  const rawIds = Array.isArray(payload?.skill_ids) ? payload.skill_ids : []
  const matches: MatchedCreationSkill[] = []
  for (const rawId of rawIds) {
    const skill = byId.get(Number(rawId))
    if (!skill) continue
    matches.push({ skill, reason: 'automatic', score: 900 })
    if (matches.length >= 1) break
  }
  return {
    matches,
    source: payload?.source === 'model' ? 'model' : 'fallback',
    reasoning: String(payload?.reasoning || ''),
  }
}

// 提交后的 Loop 执行时技能解析：显式 @ 优先；否则完全由模型依据 Skill
// 自描述决策。模型输出不可恢复或服务不可用时安全降级为不挂载 Skill，
// 不再根据主题词重合度猜测模板，避免错误模板取得严格工作流控制权。
export async function resolveExecutionSkills(options: {
  apiBaseUrl: string
  prompt: string
  skills: LocalCreationSkill[]
  signal?: AbortSignal
}): Promise<ExecutionSkillResolution> {
  const { apiBaseUrl, prompt, skills, signal } = options
  const mentioned = matchCreationSkills(prompt, skills)
  if (mentioned.length) {
    return { matches: mentioned, source: 'mentioned', reasoning: '' }
  }
  try {
    const routed = await requestExecutionSkillRoute(apiBaseUrl, prompt, skills, signal)
    if (routed.source === 'model') {
      return { matches: routed.matches, source: 'model', reasoning: routed.reasoning }
    }
  } catch {
    // Skill 路由不可用不应阻断普通创作，也不能猜测并强制套用模板。
  }
  return { matches: [], source: 'unavailable', reasoning: '' }
}

export function resolveCreationSkillDependencies(
  primarySkills: LocalCreationSkill[],
  installedSkills: LocalCreationSkill[],
  limit = 4,
): LocalCreationSkill[] {
  const maximum = Math.max(1, limit)
  const result: LocalCreationSkill[] = []
  const seen = new Set<number>()
  const dependencyIndex = new Map<string, LocalCreationSkill>()
  const normalizeReference = (value: string | number) => String(value).trim().toLowerCase()

  installedSkills
    .filter(skill => skill.status === 'saved' && skill.installed)
    .forEach(skill => {
      for (const reference of [skill.id, skill.clientSkillKey, skill.title]) {
        const key = normalizeReference(reference)
        if (key && !dependencyIndex.has(key)) dependencyIndex.set(key, skill)
      }
    })

  const append = (skill: LocalCreationSkill) => {
    if (seen.has(skill.id) || result.length >= maximum) return
    seen.add(skill.id)
    result.push(skill)
  }
  primarySkills.forEach(append)

  for (let cursor = 0; cursor < result.length && result.length < maximum; cursor += 1) {
    for (const step of result[cursor].executionSteps) {
      for (const reference of step.skills) {
        const dependency = dependencyIndex.get(normalizeReference(reference))
        if (dependency) append(dependency)
        if (result.length >= maximum) break
      }
      if (result.length >= maximum) break
    }
  }
  return result
}

export function buildCreationSkillInstruction(
  matches: MatchedCreationSkill[],
  categories: CreationSkillCategory[] = OFFLINE_CREATION_SKILL_CATEGORIES,
): string {
  if (matches.length === 0) return ''
  const recipes = matches.map(({ skill, reason }, index) => {
    const category = categoryPathFor(categories, skill.categoryId).map(item => item.name).join(' / ')
    const descriptionContext = [
      `能力目标：${skill.skillDescription.purpose}`,
      `适用文档：${skill.skillDescription.documentTypes.join('；')}`,
      `解决问题：${skill.skillDescription.problems.join('；')}`,
      skill.skillDescription.domains.length ? `涉及领域：${skill.skillDescription.domains.join('；')}` : '',
      `目标产物：${skill.skillDescription.deliverables.join('；')}`,
    ].filter(Boolean).join('\n')
    const workflowContext = skill.executionSteps.map((step, stepIndex) => [
      `步骤 ${stepIndex + 1}｜${step.title}：${step.objective}`,
      step.output.trim() ? `产出：${step.output}` : '',
      step.agents.length ? `可调用 Agent：${step.agents.join('、')}` : '',
      step.skills.length ? `可调用 Skill：${step.skills.join('、')}` : '',
      step.tools.length ? `可调用 Tool：${step.tools.join('、')}` : '',
    ].filter(Boolean).join('；')).join('\n')
    const packageContext = buildAgentSkillContext(skill)
    const hasMemoryBreadProfile = codexSkillPackageFiles(skill)
      .some(file => file.path === 'references/memorybread-creation.json')
    const includeStructuredProjection = skill.sourceKind !== 'imported' || hasMemoryBreadProfile
    return [
      `S#${index + 1} ${skill.title}（${reason === 'mentioned' ? '用户明确选择' : '根据需求自动匹配'}）`,
      `适用场景与目标：${skill.summary}`,
      category ? `创作类目：${category}` : '',
      descriptionContext,
      `执行工作流：\n${workflowContext}`,
      '严格结构契约：execution_steps 是唯一的执行流程和一级章节白名单。必须按声明顺序逐步执行，每一步单独形成对应产出；不得跳步、调换步骤或合并不同步骤的数据与表格。除非用户本轮明确要求，否则不得添加 Skill 未声明的结论、重点进展、风险阻塞、后续计划或其它通用模板章节。',
      ...(includeStructuredProjection ? [
        `标题设计风格：${skill.commonTitles.join('；')}`,
        `源标题脱敏仿写：${skill.fieldExamples.commonTitles.join('；')}`,
        `行文设计思路：${skill.textStyle}`,
        `行文仿写示例：${skill.fieldExamples.textStyle.join('；')}`,
        `图片生成方式：${skill.diagramStyle}`,
        `代码生图示例：${skill.fieldExamples.diagramStyle.join('；')}`,
        skill.writingGuidelines.length ? `话术表达风格：${skill.writingGuidelines.join('；')}` : '',
        `话术仿写示例：${skill.fieldExamples.writingGuidelines.join('；')}`,
      ] : []),
      ...(includeStructuredProjection ? (skill.distinctiveSections || []).map(section => [
        `特色亮点｜${section.title}`,
        `特征说明：${section.description}`,
        `复刻指引：${section.guidance}`,
        `仿写示例：${section.examples.join('；')}`,
      ].join('\n')) : []),
      packageContext,
    ].filter(Boolean).join('\n')
  })
  return `\n\n已安装并匹配的技能：\n${recipes.join('\n\n')}\nSkill 的 SKILL.md、execution_steps 和引用规则高于通用文档模板。只输出 Skill 明确声明的步骤产出和章节，不得按文档类型自行补齐常见栏目；脚本内容只可作为说明阅读，不得声称已经执行脚本。示例文档不进入本轮事实与结构上下文；不得虚构业务事实。`
}

function buildAgentSkillContext(skill: LocalCreationSkill) {
  const sections: string[] = []
  let remaining = MAX_IMPORTED_SKILL_CONTEXT_CHARS
  for (const file of codexSkillPackageFiles(skill)) {
    // 示例文档可能带有只为展示写法而虚构的主题和章节，不能进入运行时
    // 结构上下文，否则模型会把示例中的通用栏目误当成当前 Skill 要求。
    if (file.path === 'references/example.md') continue
    const text = skillFileText(file)
    if (text === null || !text.trim()) continue
    const heading = `[技能文件：${file.path}]\n`
    if (remaining <= heading.length) break
    const content = text.slice(0, remaining - heading.length)
    sections.push(`${heading}${content}`)
    remaining -= heading.length + content.length
    if (content.length < text.length) {
      sections.push('[技能文件内容已按上下文上限截断]')
      break
    }
  }
  return sections.length ? `Agent Skills 目录内容：\n${sections.join('\n\n')}` : ''
}

export function buildClientCreationSkillFallback(
  source: CreationSkillSource,
  fallbackReason = 'analysis_request_failed',
): CreationSkillAnalysis {
  const title = source.title.trim() || '未命名文档'
  const styleContent = selectCreationSkillStyleContent(title, source.content)
  const docType = source.docType.trim() || inferDocumentType(styleContent, title)
  const rawHeadings = extractRawDocumentHeadings(styleContent, title)
  const structure = extractDocumentStructure(styleContent)
  const resolvedStructure = structure.length > 0 ? structure : defaultStructureFor(docType)
  const commonTitles = describeClientHeadingStyle(rawHeadings)
  const headingExamples = sanitizeClientHeadingExamples(rawHeadings, resolvedStructure, title)
  const writingGuidelines = extractClientVoiceStyle(styleContent)
  const phraseExamples = clientVoiceExamples(styleContent)
  const diagramStyle = describeClientDiagramGeneration(styleContent)
  const abstractTitle = inferAbstractSkillTitle(source, docType)
  const categoryKeywords = detectCategoryKeywords(`${title}\n${docType}\n${source.content.slice(0, 8_000)}`)
  return {
    title: abstractTitle,
    summary: `直接复刻这类${docType}的子标题句式、章节推进、惯用话术和代码生图方式，可作为下一次创作的本地风格草稿。`,
    skillDescription: defaultCreationSkillDescription(
      abstractTitle,
      `用于复用${docType}形成事实、分析、方案和交付结论的方法。`,
      docType,
      categoryKeywords.slice(0, 3),
    ),
    executionSteps: defaultCreationSkillExecutionSteps(
      abstractTitle,
      `${title}\n${docType}\n${styleContent}`,
    ),
    commonTitles,
    titleStyle: commonTitles.join('；'),
    textStyle: describeClientWritingFlow(resolvedStructure, styleContent),
    diagramStyle,
    writingGuidelines,
    distinctiveSections: clientDistinctiveSections(styleContent),
    sectionHeadings: { ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS },
    fieldExamples: {
      commonTitles: headingExamples,
      titleStyle: [...headingExamples],
      textStyle: [clientFlowExample(resolvedStructure, styleContent)],
      diagramStyle: [clientDiagramExample(diagramStyle)],
      writingGuidelines: phraseExamples,
    },
    exampleDocument: clientFallbackExampleDocument(rawHeadings),
    suggestedCategoryKeywords: categoryKeywords,
    analysisMode: 'client_heuristic_fallback',
    fallbackReason,
  }
}

function selectCreationSkillStyleContent(documentTitle: string, documentContent: string) {
  const content = String(documentContent || '').trim()
  const matches = Array.from(content.matchAll(/^\s{0,3}#\s+(.+?)\s*$/gm))
  if (matches.length === 0) return content.slice(0, 30_000)
  const normalizedTitle = documentTitle.replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase()
  const titleCore = normalizedTitle.replace(/(?:整体)?(?:技术)?(?:方案|文档|报告|设计|规划|说明|手册|指南)$/u, '')
  const anchors = new Set(
    (documentTitle.match(/[A-Za-z][A-Za-z0-9_-]{2,}/g) || [])
      .map(token => token.toLowerCase())
      .filter(token => !['the', 'and', 'for', 'with'].includes(token)),
  )
  let start = matches.findIndex(match => (
    String(match[1] || '').replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase() === normalizedTitle
  ))
  if (start < 0) start = 0
  const blocks: string[] = []
  for (let index = start; index < matches.length; index += 1) {
    const match = matches[index]
    const heading = String(match[1] || '').trim()
    const normalizedHeading = heading.replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase()
    const headingTokens = new Set((heading.match(/[A-Za-z][A-Za-z0-9_-]{2,}/g) || []).map(token => token.toLowerCase()))
    const related = index === start
      || [...anchors].some(token => headingTokens.has(token))
      || (titleCore.length >= 3 && (normalizedHeading.includes(titleCore) || titleCore.includes(normalizedHeading)))
    const looksLikeAppendix = /近期|补充|更新版|最新调研|浏览(?:记录|快照)|页面快照|历史版本|专项资源|用户行为|^\s*20\d{2}[年/-]/u.test(heading)
    if (!related && looksLikeAppendix) break
    const blockStart = match.index || 0
    const blockEnd = index + 1 < matches.length ? matches[index + 1].index || content.length : content.length
    blocks.push(content.slice(blockStart, blockEnd).trim())
  }
  return (blocks.join('\n\n') || content.slice(matches[start].index || 0)).slice(0, 30_000)
}

function extractRawDocumentHeadings(content: string, documentTitle: string) {
  const markdown = content
    .split(/\r?\n/)
    .map(line => {
      const match = line.match(/^\s*(#{1,6})\s+(.+?)\s*$/)
      return match ? { level: match[1].length, text: match[2].replace(/[*_`#]/g, '').trim().replace(/[：:]$/, '') } : null
    })
    .filter((item): item is { level: number; text: string } => Boolean(item?.text))
  const candidates = markdown.map(item => item.text)
  if (candidates.length === 0) {
    candidates.push(...content
      .split(/\r?\n/)
      .map(line => line.match(/^\s*(?:[一二三四五六七八九十]+、|\d+(?:\.\d+)*[.、]\s*)(.{2,80})\s*$/)?.[1]?.trim() || '')
      .filter(Boolean))
  }
  const normalizedTitle = documentTitle.replace(/[^\p{L}\p{N}]+/gu, '')
  return distinctCreationSkillItems(candidates, 24)
    .filter(item => item.replace(/[^\p{L}\p{N}]+/gu, '') !== normalizedTitle)
    .filter(item => item.length >= 2 && item.length <= 120)
}

function describeClientHeadingStyle(headings: string[]) {
  if (headings.length === 0) {
    return [
      '层级边界：源文档没有可识别的独立子标题；仿写时只在话题明确切换处增加标题',
      '句式骨架：新增标题用“内容对象＋章节动作”的短名词结构，不写完整结论句',
      '使用边界：连续论述优先靠段落承接，不为了显得完整而强行拆成多层目录',
      '措辞选择：标题直接概括下一段承担的职责，不使用宣传口号或空泛形容词',
    ]
  }
  const joined = headings.join('\n')
  const average = headings.reduce((sum, item) => sum + item.replace(/\s+/g, '').length, 0) / headings.length
  const result = [
    average <= 8
      ? '长度节奏：子标题以四到八字的短名词结构为主；同层标题保持相近长度，便于扫读'
      : '长度节奏：子标题多为带限定语的中等长度短句；先限定对象或范围，再落到章节动作',
  ]
  if (/[与及和]/.test(joined)) result.push('并列骨架：使用“名词或动作＋与/及＋名词或结果”；只并列同一章节内同层级的两个重点')
  if (/[：:]/.test(joined)) result.push('冒号骨架：使用“主题＋冒号＋具体判断或动作”；冒号前定位话题，冒号后给阅读重点')
  if (/[？?]|为什么|为何|如何|怎么/.test(joined)) result.push('问句骨架：把待回答的问题直接写入标题；正文首段必须紧接着给出判断或方案')
  if (/从.{1,12}(?:视角|角度|层面).{0,4}看/.test(joined)) result.push('视角骨架：使用“从某一视角看，目标对象”；只在切换分析立场时使用，不当通用前缀')
  if (/建设|设计|实现|落地|优化|验证|复盘|说明|分析/.test(joined)) result.push('动作标记：保留“设计、实现、验证、复盘”等任务词；用动作说明章节职责，不用抽象形容词')
  if (/背景|目标|现状|方案|风险|验证|结论|后续/.test(joined)) result.push('路线标题：直接使用背景、目标、方案、风险、验证、后续等内容角色词，让目录呈现推进顺序')
  if (/[A-Za-z]{2,}/.test(joined)) result.push('术语嵌入：英文技术词作为精确对象嵌入中文标题；保留必要术语，不把整句改成英文口号')
  if (result.length < 4) result.push('层级一致：同层标题保持相同语法结构，不在名词短语、问句和完整结论句之间随意切换')
  return Array.from(new Set(result)).slice(0, 8)
}

function sanitizeClientHeadingExamples(headings: string[], structure: string[], documentTitle: string) {
  const preservedTerms = new Set(['api', 'sdk', 'os', 'runtime', 'agent', 'ai', 'ui', 'ux', 'http', 'https'])
  const titleFragments = (documentTitle.match(/[A-Za-z][A-Za-z0-9_-]{2,}/g) || [])
    .filter(item => !preservedTerms.has(item.toLowerCase()))
  const chineseCore = documentTitle
    .replace(/[A-Za-z0-9_\-\s]+/g, '')
    .replace(/(?:整体)?(?:技术)?(?:方案|文档|报告|设计|规划|说明|手册|指南)$/u, '')
    .trim()
  if (chineseCore.length >= 2) titleFragments.push(chineseCore)
  const examples = [documentTitle, ...headings].map(heading => {
    let value = heading
      .replace(/`[^`]+`|“[^”]+”|「[^」]+」/g, '目标对象')
      .replace(/\d+(?:\.\d+)*/g, '阶段')
      .replace(/[\p{L}\p{N}·_-]{1,16}?(?:事业群|事业部|研发中心|产品部|项目组|工作组)/gu, '相关团队')
    for (const fragment of [...titleFragments].sort((left, right) => right.length - left.length)) {
      value = value.replace(new RegExp(fragment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), '协作工作台')
    }
    value = value
      .replace(/\b[A-Z][A-Za-z0-9_-]{2,}\b/g, term => (
        preservedTerms.has(term.toLowerCase()) ? term : '协作工作台'
      ))
    value = compactCreationSkillPlaceholders(value)
    value = value.replace(
      /^(.{2,16}?)(迁移方案|优化方案|实施方案|设计方案|架构设计|流程设计|复盘报告|分析报告)$/u,
      (matched, prefix: string, suffix: string) => /^(?:总体|核心|背景|现状|目标|范围|风险|结论|后续)/.test(prefix)
        ? matched
        : `协作工作台${suffix}`,
    )
    if (/^从.{1,12}(?:视角|角度|层面)看[，,:：]?(?:协作工作台|目标对象)$/u.test(value)) {
      value = `${value}的角色与边界`
    }
    if (/^[A-Za-z][A-Za-z0-9_-]*\s*[：:]\s*(?:协作工作台|目标对象)$/u.test(value)) {
      value = `${value}的调度边界`
    }
    return compactCreationSkillPlaceholders(value).slice(0, 80)
  }).filter(isCompleteCreationSkillHeadingExample)
  return distinctCreationSkillItems(examples, 6).length
    ? distinctCreationSkillItems(examples, 6)
    : structure.slice(0, 3)
}

function clientDistinctiveSections(content: string): CreationSkillDistinctiveSection[] {
  const sections: CreationSkillDistinctiveSection[] = []
  const add = (section: CreationSkillDistinctiveSection) => {
    if (sections.length < 4) sections.push(section)
  }
  if (/定义|可以理解为|核心目标|换言之/.test(content.slice(0, 4_000))) {
    add({
      title: '定义先行的概念建立',
      description: '源文档在展开方案前先解释核心对象是什么、解决什么问题，再用核心目标限定后续讨论，定义本身承担阅读入口。',
      guidance: '核心对象首次出现时，先用一句通俗类比降低理解门槛，再补一句职责边界；随后列出目标或非目标。仅在术语可能被不同角色误解时使用。',
      examples: [
        '协作工作台可以理解为任务流转的统一入口：它连接请求、处理角色与结果证据，但不替代各环节的专业判断。',
        '核心目标是让接手者在不额外询问的情况下，判断当前状态、下一步动作与完成依据。',
      ],
    })
  }
  if ((content.match(/(?:\*\*)?[^。\n：:]{2,18}(?:\*\*)?[：:]/g) || []).length >= 3) {
    add({
      title: '短标签驱动的信息展开',
      description: '源文档反复用短标签加冒号定位信息角色，再在同一行或后续短段中补充解释，使高密度内容仍能快速扫描。',
      guidance: '标签控制在一个概念或动作内，并让同组标签保持同一语法类型；冒号后先给结论，再补条件。连续论证不要强行拆成标签。',
      examples: [
        '职责边界：维护角色只确认自己能够验证的资源状态，不代替申请角色补写用途。',
        '完成证据：释放动作必须留下可观察结果，无法确认时回到复核状态。',
      ],
    })
  }
  if (/```(?:plantuml|mermaid)/i.test(content)) {
    add({
      title: '代码图示与正文同词复现',
      description: '源文档把可执行图示代码放在解释之后，并让节点、分组和连线继续使用正文已经建立的术语，图不是独立装饰。',
      guidance: '先用正文说明阅读顺序和关键关系，再给 PlantUML 或 Mermaid 代码；图中只保留正文已有对象，连线使用动作词，图后补充异常或边界。',
      examples: ['正文先说明申请、确认与释放的主链路，再用 PlantUML 活动图纵向排列动作，并把跨角色步骤放入对应泳道。'],
    })
  }
  if ((content.match(/^\s*---+\s*$/gm) || []).length >= 2) {
    add({
      title: '分隔线控制议题切换',
      description: '源文档用独立分隔线标记较大的议题或文档入口切换，让读者在长内容中明确感知上下文已经重置。',
      guidance: '只在讨论对象或交付目标发生明显变化时使用分隔线；分隔线后重新给出标题或一句入口判断，不把它当作普通段落装饰。',
      examples: ['完成总体方案说明后使用分隔线，下一部分以“评测接入：先明确入口与返回结果”重新建立阅读上下文。'],
    })
  }
  return sections
}

function clientFallbackExampleDocument(sourceHeadings: string[]) {
  const joined = sourceHeadings.join('\n')
  let document = DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT
  if (/从.{1,12}(?:视角|角度|层面).{0,4}看/.test(joined)) {
    document = document.replace(
      '## 背景与问题：一次冲突暴露出的状态断点',
      '## 从使用视角看，预约冲突的来源',
    )
  }
  if (/[？?]|为什么|为何|如何|怎么/.test(joined)) {
    const questions: Array<[string, string]> = [
      ['## 背景与问题：一次冲突暴露出的状态断点', '## 为什么现有预约方式需要调整'],
      ['## 目标与范围：先明确要解决什么', '## 这次要解决什么，不解决什么'],
      ['## 方案设计：让状态、责任与动作相互对应', '## 方案如何落到执行'],
      ['## 核心流程：从提出申请到完成释放', '## 一次预约如何走完整个流程'],
      ['## 风险与保障：异常不能重新回到人工猜测', '## 出现异常时如何保持边界清楚'],
      ['## 验证与复盘：用可观察结果收束判断', '## 怎样判断这套方案真正有效'],
      ['## 结论与后续：把临时协调变成稳定机制', '## 最终要形成什么结果'],
    ]
    for (const [from, to] of questions) document = document.replace(from, to)
    return document
  }
  if (!/[：:]/.test(joined)) {
    document = document
      .replace('# 共享评审空间：预约流程与协作边界优化方案', /[与及]/.test(joined)
        ? '# 共享评审空间预约流程与协作边界优化方案'
        : '# 共享评审空间预约流程优化方案')
      .replace(/^## ([^：\n]+)：.*$/gm, '## $1')
  }
  return document
}

function describeClientWritingFlow(structure: string[], content: string) {
  const paragraphs = content.split(/\n\s*\n/).map(item => item.trim()).filter(Boolean)
  const average = paragraphs.reduce((sum, item) => sum + item.length, 0) / Math.max(1, paragraphs.length)
  const paragraphStyle = average > 120
    ? '段内通常先给判断，再连续补充原因、边界和落法'
    : '用短段落推进，一个段落只承担一个判断、动作或补充说明'
  const listStyle = /^\s*(?:[-*+]|\d+[.、])\s+/m.test(content)
    ? '遇到并列动作或条件时切成列表，列表项保持同一语法起点'
    : '主要依靠连续段落推进，段与段之间用因果或递进关系承接'
  const opener = /定义|可以理解为|核心目标|总体来看/.test(content.slice(0, 1200))
    ? '开篇先给定义或核心判断，再补适用范围，读者无需读完背景才知道文档要解决什么'
    : '开篇先交代问题角色、适用范围和目标，再进入分析，不用口号或宽泛行业背景铺垫'
  const labelStyle = (content.match(/\*\*[^*\n]{2,20}\*\*[：:]/g) || []).length >= 2
    ? '信息展开时大量使用“短标签＋冒号＋解释”，标签定位信息类型，冒号后补依据或动作'
    : '信息展开以完整判断句为主，只在同层信息需要快速扫描时切换为标签或列表'
  const transitions = ['需要说明的是', '具体而言', '基于此', '同时', '此外', '因此', '最后']
    .filter(phrase => content.includes(phrase))
  const transitionStyle = transitions.length
    ? `章节与段落之间沿用“${transitions.slice(0, 4).join('、')}”等连接词，分别承担补充、递进或收束`
    : '章节之间靠标题角色和前后因果自然承接，不额外堆叠模板连接词'
  return `章节路线：全文沿“${structure.join(' → ')}”推进，标题本身承担阅读导航；中段展开分析、方案或取舍，末段必须落到验证和后续动作。开篇配方：${opener}。段内配方：${paragraphStyle}，通常先写本段判断，再补原因、边界和落法。列表条件：${listStyle}，不要把相互依赖的论证拆成彼此孤立的要点。信息密度：${labelStyle}。衔接方式：${transitionStyle}。段落节奏：定义、判断、依据和动作分别承担清晰职责；同一段出现多个转折时应拆段，但不要把一句完整论证切成口号。收束要求：结尾回看目标、给出可验证结果和下一步，不重复摘要，也不新增未经前文论证的判断。不可迁移项：只复刻标题句法、信息顺序和语气，不复制源文档的专名、事实、结论、日期或指标；源文档缺少证据的图示、列表和结论句也不能为了形式完整而补造。交付前自检：逐节确认标题是否预告正文职责、首段是否立即回应标题、并列项是否同构、结尾是否留下可执行动作与验证依据。`
}

const CLIENT_VOICE_PHRASES: Array<[string, string]> = [
  ['需要说明的是', '引出边界、例外或容易误解的前提'],
  ['值得注意的是', '提示风险或需要读者停顿关注的信息'],
  ['具体而言', '把上一层判断拆成可执行细节'],
  ['基于此', '承接前文依据并转入结论或方案'],
  ['换言之', '用更直接的说法重述复杂判断'],
  ['总体来看', '在段落或章节末收束判断'],
  ['首先', '开启有顺序的论述或动作清单'],
  ['其次', '延续同层级的下一个论点'],
  ['最后', '收束一组论点并转入结论'],
  ['同时', '补充并行条件或同步动作'],
  ['此外', '增加独立但相关的补充信息'],
  ['因此', '从原因过渡到判断、动作或结果'],
  ['建议', '用克制语气提出行动'],
  ['需要', '直接声明必要动作或约束'],
  ['应当', '以规范性语气提出要求'],
  ['必须', '标记不可让步的硬约束'],
  ['优先', '表达取舍顺序而不使用夸张措辞'],
  ['避免', '用负向动作明确禁止项'],
  ['确保', '把动作落到预期结果'],
  ['明确', '要求把模糊对象、边界或责任说具体'],
]

function extractClientVoiceStyle(content: string) {
  const matched = CLIENT_VOICE_PHRASES
    .filter(([phrase]) => content.includes(phrase))
  const styles = matched.slice(0, 5).map(([phrase, role]) =>
    `证据话术“${phrase}”：源文档用它${role}；复刻时把它放在承担同类职责的句首，后面紧接完整判断、条件或动作，不让短语单独成句；同一段只使用一次，没有对应逻辑关系时不要把它当作装饰性连接词。`,
  )
  if (content.includes('：')) {
    styles.push('标点句式“短标签＋冒号＋解释”：源文档用冒号把信息角色和具体内容分开；复刻时标签保持短而同构，冒号后先写核心判断再补依据，适合定义、约束和并列说明；连续论证或因果链不要硬拆成标签。')
  }
  if (content.includes('；')) {
    styles.push('长句节奏“分号切分同层判断”：源文档用分号承载彼此并列且各自完整的信息；复刻时让分号两侧保持相同语法起点和相近粒度，读完仍是一组判断；存在先后、因果或转折时应拆句，不用分号掩盖关系。')
  }
  if (/^\s*[-*+]\s+/m.test(content)) {
    styles.push('列表话术“动作词先行”：源文档把可并列扫描的动作、条件或结果写成列表；复刻时每项先用同类动词或名词短语点明职责，再补对象与边界，语气直接克制；相互依赖的论证仍保留连续段落。')
  }
  const modalWords = ['建议', '需要', '应当', '必须', '优先', '避免', '确保', '明确']
    .filter(word => content.includes(word))
  if (modalWords.length) {
    styles.push(`动作语气“${modalWords.slice(0, 4).join('、')}”：源文档用这些词区分建议、必要动作、优先级与禁止项；复刻时把动作主体、作用对象和预期结果写全，强弱程度沿用原文证据；没有硬约束依据时不得把“建议”擅自升级为“必须”。`)
  }
  if (!matched.length) {
    styles.unshift('话术证据边界：源文档没有识别出稳定反复出现的标志性短语，复刻时以原有句法、标点和动作词为准，不额外植入“首先、其次、综上”等模板过渡语；需要承接时直接写清前后判断的因果、递进或范围变化。')
  }
  styles.push(
    '句式节奏复刻：源文档以能够独立成立的陈述句承载判断，复刻时先写清谁对什么采取何种动作，再用后句补原因、条件或结果；一个句子只保留一条主逻辑，出现多次转折时拆句，但不把完整论证切成缺少主谓的口号。',
    '术语与指代控制：从源文档提取可公开复用的专业称呼后，为同一概念固定一种叫法，后文只在指代对象明确时使用“该对象”“这一过程”等代词；不得为了显得专业堆叠近义词，也不得把来源专名带入新的虚构主题。',
    '段落语气迁移：延续源文档先判断、再补依据与适用边界的完整陈述，主语和动作保持明确，专业词首次出现时给出足够上下文；只迁移表达顺序与语气，不复制来源中的专名、事实、指标和业务结论。',
    '话术交付自检：逐段检查连接词是否真的对应补充、递进、因果或收束，动作词是否带清楚的执行对象与结果，列表项是否同构，强制语气是否有依据；删去不承担信息作用的套话，并统一同一概念的称呼。',
  )
  const unique = Array.from(new Set(styles))
  const recipe = unique.length > 8
    ? [...unique.slice(0, 6), ...unique.slice(-2)]
    : unique
  while (recipe.join('').length > 700 && recipe.length > 5) recipe.splice(-3, 1)
  return recipe
}

function clientVoiceExamples(content: string) {
  const phrases = CLIENT_VOICE_PHRASES.filter(([phrase]) => content.includes(phrase)).map(([phrase]) => phrase).slice(0, 3)
  return phrases.length
    ? phrases.map(phrase => `${phrase}，相关角色先确认适用边界，再推进后续动作。`)
    : ['源文档没有稳定的惯用短语，仿写时不额外植入模板化套话。']
}

function describeClientDiagramGeneration(content: string) {
  let choice = ''
  if (/```plantuml|@startuml/i.test(content)) {
    choice = '源文档存在 PlantUML 代码图示，继续使用 PlantUML，并根据正文实际关系选择组件图、时序图或活动图；默认保留源图从左到右的主阅读方向，用 package 或 rectangle 表达边界。'
  } else if (/```mermaid/i.test(content)) {
    choice = '源文档存在 Mermaid 代码图示，继续使用 Mermaid，并沿用 flowchart 或 sequenceDiagram 的表达方式；节点使用短名词，连线使用动作词，分组边界与正文层级对应。'
  } else if (/时序图|调用链|交互顺序/.test(content)) {
    choice = '源文档以时间顺序解释交互，推荐 PlantUML sequence diagram；参与者按正文首次出现顺序排列，消息箭头使用动作词，异常或条件链路放入 alt 分组。'
  } else if (/架构图|组件图|分层|模块关系/.test(content)) {
    choice = '源文档存在分层、模块或依赖关系，推荐 PlantUML component diagram；同层对象横向对齐，使用 package 分组边界，只保留正文重点讨论的关键依赖。'
  } else if (/流程图|步骤|流转|审批/.test(content)) {
    choice = '源文档存在步骤、流转或审批关系，推荐 PlantUML activity diagram；主流程从上到下排列，判断节点写成问题，角色发生切换时使用泳道。'
  } else {
    choice = '源文档未识别到图示代码或图片说明，默认不生成图片；只有当对象关系、时间交互或条件流程仅靠连续文字难以准确理解时，才使用 PlantUML 补充组件图、时序图或活动图。'
  }
  return `证据与启用条件：${choice}选型判断：稳定依赖或分层关系用组件图，跨角色的先后消息用时序图，带判断与回退的动作链用活动图；同一张图只回答一个核心问题，无法明确图要解释什么时继续使用文字。信息筛选：先从正文提取已经定义的对象、边界、动作、条件和结果，再删去背景铺垫、评价性形容词、未被正文解释的内部细节与敏感事实；图中不得新增来源没有支持的节点、关系或结论。布局与阅读路径：主链路保持单一方向，核心对象放在视觉主轴，同层元素对齐，跨层关系通过分组边界表达；分支从触发点就近展开，避免箭头交叉和读者来回跳读。元素与标注：节点名称沿用正文中的短名词，箭头使用可执行的关系动词，条件写在分支或消息上，边界用 package、rectangle、subgraph 或泳道表示；同类元素必须采用同一种形状和命名粒度。视觉规则：使用暖灰、深棕与低饱和强调色区分层级，颜色只承担分组、状态或重点提示，不用渐变、阴影、装饰图标和无意义图例；正文术语、图中术语与标题保持完全一致。图文衔接：在图前先用一段话说明阅读方向、图要回答的问题和暂不覆盖的边界，图后只解释关键关系、异常分支及其对方案的影响，不逐节点复述图面。禁用边界与自检：不把大段正文塞进节点，不用一张图同时承载架构、时序和流程，不用图替代必要的决策依据；交付前检查代码能否渲染、方向是否唯一、连线是否有语义、术语是否一致、每个元素是否都能回指正文。`
}

function clientDiagramExample(style: string) {
  if (style.includes('Mermaid')) return 'Mermaid flowchart：主流程沿同一方向排列，分支只表达正文已经解释的判断条件。'
  if (style.includes('sequence diagram')) return 'PlantUML 时序图：参与者按出现顺序排列，主链路使用实线箭头，条件分支放入 alt 区块。'
  if (style.includes('component diagram')) return 'PlantUML 组件图：用 package 表示层级，用 component 表示模块，依赖箭头标注正文中的关系动词。'
  if (style.includes('activity diagram')) return 'PlantUML 活动图：主流程纵向排列，判断使用条件分支，跨角色动作放入对应泳道。'
  return '默认不生成图片；确需补图时使用 PlantUML，并只画正文已经说明的对象、边界和关系。'
}

function clientFlowExample(structure: string[], content: string) {
  const opener = content.includes('首先') ? '首先，' : ''
  const connector = content.includes('基于此') ? '基于此，' : '随后，'
  const closer = content.includes('因此') ? '因此，' : '最后，'
  return `${opener}先界定示例事项的目标与适用范围。${connector}按“${structure.slice(0, 4).join(' → ')}”展开可选方案与约束。${closer}用验证结果收束判断，并明确后续动作。`
}

export function normalizeCreationSkillTitle(candidate: unknown, source: CreationSkillSource): string {
  const docType = source.docType.trim() || inferDocumentType(source.content, source.title)
  const sourceText = `${source.title}\n${docType}`
  if (/(?:跨部门|跨团队|多团队)/.test(sourceText) && /(?:技术|架构|研发|系统)/.test(sourceText) && /(?:会议|沟通|评审|纪要)/.test(sourceText)) {
    return '跨部门技术沟通会文档'
  }
  if (/整体技术方案/.test(sourceText)) return '运行平台整体技术方案'
  if (/技术架构|架构设计/.test(sourceText)) return '技术架构设计文档'
  if (/技术方案/.test(sourceText)) return '技术方案文档'
  if (/评测接入/.test(sourceText)) return '评测接入文档'
  let title = String(candidate || '').trim()
  const organizationNames = (source.title.match(/[\p{L}\p{N}·_-]{1,12}?(?:事业群|事业部|委员会|项目组|工作组|部门|团队|小组|中心|部)/gu) || [])
    .filter(name => !/^(?:跨|多|各|相关)(?:部门|团队|小组)$/.test(name))
  for (const organization of organizationNames.sort((left, right) => right.length - left.length)) {
    title = title.split(organization).join('')
  }
  title = title
    .replace(/(?:创作|写作)\s*Skill$/i, '')
    .replace(/Skill$/i, '')
    .replace(/沟通会(?:会议)?纪要$/u, '沟通会文档')
    .replace(/会议纪要$/u, '会议文档')
    .replace(/^[\s·—_:：-]+|[\s·—_:：-]+$/g, '')
  if (
    title.length < 4
    || organizationNames.some(name => title.includes(name))
    || (/复盘|总结/.test(title) && !/复盘|总结/.test(sourceText))
  ) {
    return inferAbstractSkillTitle(source, docType)
  }
  return title.slice(0, 80)
}

function extractDocumentStructure(content: string): string[] {
  const headings = content
    .split(/\r?\n/)
    .map(line => line.match(/^\s*(?:#{1,6}\s+|(?:\d+\.)+\s*)([^#].*?)\s*$/)?.[1]?.trim() || '')
    .map(canonicalSkillHeading)
    .filter(item => item.length >= 2 && item.length <= 60)
  return Array.from(new Set(headings)).slice(0, 12)
}

function canonicalSkillHeading(heading: string) {
  const mappings: Array<[RegExp, string]> = [
    [/背景|现状|概述/, '背景与目标'],
    [/为什么|为何|原因|必要性|问题/, '问题与原因'],
    [/目标|范围/, '目标与范围'],
    [/约束|原则/, '约束与设计原则'],
    [/架构|总体设计/, '总体方案'],
    [/方案|策略|路径|落地/, '方案设计'],
    [/流程|步骤/, '核心流程'],
    [/功能|模块/, '核心设计'],
    [/接口|数据/, '接口与数据'],
    [/实施|计划|里程碑/, '实施计划'],
    [/风险|保障/, '风险与保障'],
    [/验证|验收|指标/, '验证与验收'],
    [/结论|总结|后续/, '结论与后续'],
  ]
  return mappings.find(([pattern]) => pattern.test(heading))?.[1] || ''
}

function inferDocumentType(content: string, title: string) {
  const text = `${title}\n${content.slice(0, 4_000)}`.toLowerCase()
  if (/技术架构|总体架构|系统架构/.test(text)) return '技术架构设计文档'
  if (/接口设计|\bapi\b/.test(text)) return '接口设计文档'
  if (/产品需求|\bprd\b/.test(text)) return '产品需求文档'
  if (/\bui\b|界面设计/.test(text)) return 'UI 设计文档'
  if (/用户体验|\bux\b/.test(text)) return '用户体验设计文档'
  if (/行业研究|调研报告/.test(text)) return '行业研究报告'
  if (/运营方案|活动方案/.test(text)) return '运营方案'
  return '创作文档'
}

function inferAbstractSkillTitle(source: CreationSkillSource, docType: string) {
  // 正文可能包含案例、引用或 Bake 追加片段，用途特判只依据标题和文档类型，
  // 避免一次“案例复盘”把技术方案错误命名为复盘总结。
  const text = `${source.title}\n${docType}`
  if (/跨部门|跨团队|多团队/.test(text) && /技术|架构|研发|系统/.test(text) && /会议|沟通|评审|纪要/.test(text)) {
    return '跨部门技术沟通会文档'
  }
  if (/跨部门|跨团队|多团队/.test(text) && /会议|沟通|协作|纪要/.test(text)) return '跨部门协作会议文档'
  if (/架构评审|技术评审|方案评审/.test(text)) return '技术方案评审文档'
  if (/复盘|总结会/.test(text)) return '项目复盘总结文档'
  if (/客户|交付|实施/.test(text) && /沟通|汇报|会议/.test(text)) return '客户交付沟通文档'
  if (/整体技术方案/.test(text)) return '运行平台整体技术方案'
  if (/技术架构|架构设计/.test(text)) return '技术架构设计文档'
  if (/技术方案/.test(text)) return '技术方案文档'
  if (/评测接入/.test(text)) return '评测接入文档'
  const purposeByType: Array<[RegExp, string]> = [
    [/技术架构|系统架构/, '技术架构设计文档'],
    [/接口设计/, '系统接口设计文档'],
    [/产品需求|PRD/i, '产品需求沟通文档'],
    [/产品设计/, '产品方案设计文档'],
    [/UI|用户体验/i, '产品体验设计文档'],
    [/运营/, '运营方案策划文档'],
    [/行业研究|数据分析/, '业务分析研究报告'],
    [/品牌|内容策划/, '品牌内容策划文档'],
  ]
  return purposeByType.find(([pattern]) => pattern.test(docType))?.[1]
    || `${docType.replace(/文档$/, '').replace(/报告$/, '')}创作文档`
}

function defaultStructureFor(docType: string) {
  if (/架构|技术|接口|软件/.test(docType)) {
    return ['背景与目标', '范围与约束', '总体方案', '关键设计', '风险与验证', '实施与演进']
  }
  if (/产品|需求|体验|UI/.test(docType)) {
    return ['背景与目标', '用户与场景', '需求或设计方案', '关键流程', '验收标准', '后续计划']
  }
  return ['背景与目标', '现状与问题', '核心方案', '执行计划', '风险与度量', '结论']
}

function detectCategoryKeywords(text: string) {
  const rules: Array<[RegExp, string]> = [
    [/电商|零售|商品|订单/, '电商零售'],
    [/银行|支付|信贷|金融/, '银行与支付'],
    [/保险|理赔|精算/, '保险'],
    [/制造|产线|工艺|工业/, '智能制造'],
    [/咨询|研究报告/, '咨询与研究'],
    [/品牌|内容策划/, '品牌与内容'],
    [/云计算|数据平台|人工智能|大模型|机器学习/, '云计算、数据与人工智能'],
    [/网络安全|信息安全|威胁建模/, '网络安全'],
    [/证券|基金|资管|投资研究|估值/, '证券、基金与资产管理'],
    [/农业|种植|畜牧|养殖|林业|渔业|水产/, '农林牧渔'],
    [/建筑|施工|工程造价|房地产|物业/, '建筑与房地产'],
    [/煤矿|矿山|石油|天然气|电网|电力|新能源|储能/, '能源与矿业'],
    [/公路|铁路|航空运输|航运|港口|仓储|物流|快递|配送/, '交通运输与物流'],
    [/商超|百货|批发|进出口|跨境贸易|经销/, '批发零售与贸易'],
    [/课程|教学|教研|学校|高校|职业教育|培训/, '教育与培训'],
    [/医院|临床|护理|药品|生物科技|医疗器械|康养|养老/, '医疗健康与生命科学'],
    [/新闻|出版|影视|广告|公关|赛事|博物馆|展览/, '文化传媒与文体娱乐'],
    [/旅游|旅行|酒店|餐饮|景区|度假/, '旅游、酒店与餐饮'],
    [/政策|政府|事业单位|社区服务|应急管理|公共安全/, '政府与公共服务'],
    [/电信|通信网络|卫星通信|数据中心/, '电信与通信'],
    [/环保|污水|固废|环卫|环境治理|供水|燃气|供热/, '环保与公用事业'],
    [/科研|实验室|检验检测|认证审核/, '科研、检测与认证'],
    [/基金会|慈善|公益|行业协会|商会|社会工作/, '社会组织与非营利'],
    [/家政|美容美发|维修服务|婚庆|宠物服务/, '生活与个人服务'],
    [/技术架构|总体架构|系统架构/, '技术架构设计文档'],
    [/接口|\bapi\b/i, '接口设计文档'],
    [/产品需求|\bprd\b/i, '产品需求文档'],
    [/\bui\b|界面设计/i, 'UI 设计文档'],
    [/用户体验|\bux\b/i, '用户体验设计文档'],
    [/运营方案/, '运营方案'],
  ]
  return Array.from(new Set(rules.filter(([pattern]) => pattern.test(text)).map(([, keyword]) => keyword)))
}

function serializeLocalSkill(skill: Omit<LocalCreationSkill, 'id' | 'createdAt' | 'updatedAt'>) {
  return {
    client_skill_key: skill.clientSkillKey,
    cloud_skill_id: skill.cloudSkillId || null,
    source_kind: skill.sourceKind,
    source_id: skill.sourceId,
    title: skill.title,
    summary: skill.summary,
    category_id: skill.categoryId || null,
    skill_description: {
      purpose: skill.skillDescription.purpose,
      document_types: skill.skillDescription.documentTypes,
      problems: skill.skillDescription.problems,
      domains: skill.skillDescription.domains,
      deliverables: skill.skillDescription.deliverables,
    },
    execution_steps: skill.executionSteps.map(step => ({
      id: step.id,
      title: step.title,
      objective: step.objective,
      output: step.output,
      agents: step.agents,
      skills: step.skills,
      tools: step.tools,
      retain_webpage_screenshot: step.retainWebpageScreenshot === true,
    })),
    common_titles: skill.commonTitles,
    title_style: skill.titleStyle,
    text_style: skill.textStyle,
    diagram_style: skill.diagramStyle,
    writing_guidelines: skill.writingGuidelines,
    distinctive_sections: (skill.distinctiveSections || []).map(section => ({
      title: section.title,
      description: section.description,
      guidance: section.guidance,
      examples: section.examples,
    })),
    section_headings: {
      common_titles: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.commonTitles,
      title_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.titleStyle,
      text_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.textStyle,
      diagram_style: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.diagramStyle,
      writing_guidelines: DEFAULT_CREATION_SKILL_SECTION_HEADINGS.writingGuidelines,
    },
    field_examples: {
      common_titles: skill.fieldExamples.commonTitles,
      title_style: skill.fieldExamples.titleStyle,
      text_style: skill.fieldExamples.textStyle,
      diagram_style: skill.fieldExamples.diagramStyle,
      writing_guidelines: skill.fieldExamples.writingGuidelines,
    },
    example_document: skill.exampleDocument,
    package_files: (skill.packageFiles || []).map(file => ({
      path: file.path,
      media_type: file.mediaType,
      content_base64: file.contentBase64,
      size_bytes: file.sizeBytes,
    })),
    status: skill.status,
    installed: skill.installed,
    published: skill.published,
  }
}

function mapLocalSkill(item: any): LocalCreationSkill {
  // 新版允许完整示例文档和各类仿写示例留空。不能再用空值判断旧数据，
  // 否则保存后的响应会被映射成内置默认配方，表现为用户修改没有生效。
  const modernRecipeKeys = ['common_titles', 'title_style', 'text_style', 'diagram_style', 'writing_guidelines']
  const legacyContent = !item?.section_headings
    || !item?.field_examples
    || !modernRecipeKeys.every(key => Object.prototype.hasOwnProperty.call(item.section_headings, key))
    || !modernRecipeKeys.every(key => Object.prototype.hasOwnProperty.call(item.field_examples, key))
  const legacyTitleFields = item?.section_headings?.common_titles === '这类文档标题通常怎么命名'
    || item?.section_headings?.title_style === '标题如何传递重点'
  const legacyDefaults = buildLegacyGeneralizedContent(String(item.title || ''))
  const title = repairStoredCreationSkillTitle(item)
  const summary = String(item.summary || '')
  const legacyTitleExamples = Array.from(new Set([
    ...(Array.isArray(item.common_titles) ? item.common_titles : []),
    ...(Array.isArray(item?.field_examples?.common_titles) ? item.field_examples.common_titles : []),
    ...(Array.isArray(item?.field_examples?.title_style) ? item.field_examples.title_style : []),
  ].map(value => String(value).trim()).filter(Boolean))).slice(0, 6)
  const mapped: LocalCreationSkill = {
    id: Number(item.id),
    clientSkillKey: item.client_skill_key,
    cloudSkillId: item.cloud_skill_id,
    sourceKind: item.source_kind,
    sourceId: item.source_id,
    title,
    summary,
    categoryId: item.category_id,
    skillDescription: mapSkillDescription(item.skill_description, title, summary),
    executionSteps: mapExecutionSteps(item.execution_steps, title, summary),
    commonTitles: legacyContent
      ? legacyDefaults.commonTitles
      : legacyTitleFields && String(item.title_style || '').trim()
        ? [String(item.title_style).trim()]
        : distinctCreationSkillItems(
          (Array.isArray(item.common_titles) ? item.common_titles : [])
            .map((value: unknown) => coerceCreationSkillStringItem(value, 'common_titles')),
          12,
        ),
    titleStyle: legacyContent ? legacyDefaults.titleStyle : item.title_style || '',
    textStyle: legacyContent
      ? legacyDefaults.textStyle
      : hasSerializedSkillObjectItems(item)
        ? repairStoredCreationSkillTextStyle(item.text_style)
        : item.text_style || '',
    diagramStyle: legacyContent
      ? legacyDefaults.diagramStyle
      : hasSerializedSkillObjectItems(item)
        ? repairStoredCreationSkillDiagramStyle(item.diagram_style)
        : item.diagram_style || '',
    writingGuidelines: legacyContent
      ? legacyDefaults.writingGuidelines
      : hasSerializedSkillObjectItems(item)
        ? repairStoredCreationSkillWritingGuidelines(item.writing_guidelines)
        : (Array.isArray(item.writing_guidelines) ? item.writing_guidelines : [])
          .map((value: unknown) => coerceCreationSkillStringItem(value, 'writing_guidelines'))
          .filter(Boolean),
    distinctiveSections: mapDistinctiveSections(item.distinctive_sections),
    sectionHeadings: mapSectionHeadings(item.section_headings),
    fieldExamples: legacyTitleFields && legacyTitleExamples.length
      ? {
        ...mapFieldExamples(item.field_examples, legacyContent),
        commonTitles: legacyTitleExamples,
        titleStyle: legacyTitleExamples,
      }
      : mapFieldExamples(item.field_examples, legacyContent),
    exampleDocument: legacyContent
      ? item.example_document?.trim() || DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT
      : String(item.example_document || '').trim(),
    packageFiles: Array.isArray(item.package_files)
      ? item.package_files.map((file: any) => ({
        path: String(file.path || ''),
        mediaType: String(file.media_type || 'application/octet-stream'),
        contentBase64: String(file.content_base64 || ''),
        sizeBytes: Number(file.size_bytes || 0),
      })).filter((file: CreationSkillPackageFile) => file.path && file.contentBase64)
      : [],
    status: item.status === 'draft' ? 'draft' : 'saved',
    installed: Boolean(item.installed),
    published: Boolean(item.published),
    createdAt: Number(item.created_at),
    updatedAt: Number(item.updated_at),
  }
  if (mapped.packageFiles?.length === 0 && mapped.sourceKind !== 'imported') {
    mapped.packageFiles = agentSkillPackageFiles(mapped)
  }
  return mapped
}

function repairStoredCreationSkillTitle(item: any) {
  const title = String(item?.title || '').trim()
  const styleEvidence = JSON.stringify([
    ...(Array.isArray(item?.common_titles) ? item.common_titles : []),
    ...(Array.isArray(item?.field_examples?.common_titles) ? item.field_examples.common_titles : []),
  ])
  if (/复盘|总结/.test(title) && /整体技术方案|OS\s*[/+＋]?整体技术方案/i.test(styleEvidence)) {
    return '运行平台整体技术方案'
  }
  return title
}

function hasSerializedSkillObjectItems(item: any) {
  return [
    ...(Array.isArray(item?.common_titles) ? item.common_titles : []),
    ...(Array.isArray(item?.field_examples?.common_titles) ? item.field_examples.common_titles : []),
  ].some(value => typeof value === 'string' && /^\{\s*['"][^'"]+['"]\s*:/u.test(value.trim()))
}

function repairStoredCreationSkillTextStyle(value: unknown) {
  const text = String(value || '').trim()
  if (text.length >= 400) return text
  const route = '目标与范围 → 核心判断 → 方案展开 → 验证与后续'
  const supplement = `执行配方：开篇先界定核心对象、适用范围与目标，让读者在进入细节前知道文档要解决什么。章节沿“${route}”推进，标题直接预告下一节承担的内容职责。段内先给判断，再补形成判断的依据、影响边界和具体落法；只有并列动作、条件或结果需要快速比较时才切成列表，并让列表项保持同一语法起点。章节之间依靠因果、递进或范围变化自然承接，不堆叠模板连接词。阅读密度上，一个段落只承担一个主判断；定义、例外和行动要求分别成段，避免把多个逻辑转折压进同一句。结尾回看开篇目标，以可观察结果、责任边界和下一步动作收束，不重复摘要，也不新增前文没有论证的结论。不可迁移项：只复刻标题句法、信息顺序和语气，不复制来源中的专名、事实、日期、指标或业务判断；缺少证据的图示和结论也不能为了形式完整而补造。交付前逐节检查标题是否回应正文、判断是否带依据、并列项是否同构、后续动作是否可执行，并检查术语前后一致。`
  return text ? `${text}\n\n${supplement}` : supplement
}

function repairStoredCreationSkillDiagramStyle(value: unknown) {
  const text = String(value || '').trim()
  return mergeCreationSkillDiagramStyle(text, describeClientDiagramGeneration(text))
}

function repairStoredCreationSkillWritingGuidelines(value: unknown) {
  const items = (Array.isArray(value) ? value : [])
    .map(entry => coerceCreationSkillStringItem(entry, 'writing_guidelines'))
    .filter(Boolean)
  return mergeCreationSkillWritingGuidelines(items, extractClientVoiceStyle(items.join('\n')))
}

function mapMarketSkill(item: any): CreationSkillMarketItem {
  const content = item?.content || {}
  const title = String(item.title || '')
  const summary = String(item.summary || '')
  const legacyTitleFields = content?.section_headings?.common_titles === '这类文档标题通常怎么命名'
    || content?.section_headings?.title_style === '标题如何传递重点'
  const legacyTitleExamples = Array.from(new Set([
    ...(Array.isArray(content.common_titles) ? content.common_titles : []),
    ...(Array.isArray(content?.field_examples?.common_titles) ? content.field_examples.common_titles : []),
    ...(Array.isArray(content?.field_examples?.title_style) ? content.field_examples.title_style : []),
  ].map(value => String(value).trim()).filter(Boolean))).slice(0, 6)
  return {
    id: String(item.id || ''),
    title,
    summary,
    isOfficial: Boolean(item.is_official),
    categoryId: String(item.category_id || ''),
    categoryPath: Array.isArray(item.category_path)
      ? item.category_path.map((category: any) => ({
        id: String(category.id || ''),
        key: String(category.key || ''),
        name: String(category.name || ''),
        level: Number(category.level) as 1 | 2 | 3 | 4,
        parentId: category.parent_id ? String(category.parent_id) : undefined,
        sortOrder: Number(category.sort_order || 0),
      }))
      : [],
    author: {
      id: String(item?.author?.id || ''),
      nickname: String(item?.author?.nickname || '匿名面包师'),
    },
    packageFiles: Array.isArray(item.package_files)
      ? item.package_files.map((file: any) => ({
        path: String(file.path || ''),
        mediaType: String(file.media_type || 'application/octet-stream'),
        contentBase64: String(file.content_base64 || ''),
        sizeBytes: Number(file.size_bytes || 0),
      })).filter((file: CreationSkillPackageFile) => file.path && file.contentBase64)
      : [],
    packageName: String(item.package_name || ''),
    packageFileCount: Number(item.package_file_count || 0),
    packageSizeBytes: Number(item.package_size_bytes || 0),
    packageSha256: String(item.package_sha256 || ''),
    skillDescription: mapSkillDescription(
      content.skill_description,
      title,
      summary,
      '',
      Array.isArray(item.category_path)
        ? item.category_path.map((category: any) => String(category.name || '')).filter(Boolean)
        : [],
    ),
    executionSteps: mapExecutionSteps(content.execution_steps, title, summary),
    commonTitles: legacyTitleFields && String(content.title_style || '').trim()
      ? [String(content.title_style).trim()]
      : (Array.isArray(content.common_titles) ? content.common_titles : [])
        .map((value: unknown) => coerceCreationSkillStringItem(value, 'common_titles'))
        .filter(Boolean),
    titleStyle: String(content.title_style || ''),
    textStyle: hasSerializedSkillObjectItems(content)
      ? repairStoredCreationSkillTextStyle(content.text_style)
      : String(content.text_style || ''),
    diagramStyle: hasSerializedSkillObjectItems(content)
      ? repairStoredCreationSkillDiagramStyle(content.diagram_style)
      : String(content.diagram_style || ''),
    writingGuidelines: hasSerializedSkillObjectItems(content)
      ? repairStoredCreationSkillWritingGuidelines(content.writing_guidelines)
      : (Array.isArray(content.writing_guidelines) ? content.writing_guidelines : [])
        .map((value: unknown) => coerceCreationSkillStringItem(value, 'writing_guidelines'))
        .filter(Boolean),
    distinctiveSections: mapDistinctiveSections(content.distinctive_sections),
    sectionHeadings: mapSectionHeadings(content.section_headings),
    fieldExamples: legacyTitleFields && legacyTitleExamples.length
      ? {
        ...mapFieldExamples(content.field_examples),
        commonTitles: legacyTitleExamples,
        titleStyle: legacyTitleExamples,
      }
      : mapFieldExamples(content.field_examples),
    exampleDocument: String(content.example_document || '').trim() || DEFAULT_CREATION_SKILL_EXAMPLE_DOCUMENT,
    publishedAt: item.published_at || null,
    updatedAt: String(item.updated_at || ''),
  }
}

function buildLegacyGeneralizedContent(title: string) {
  const kind = /技术|架构|系统|接口/i.test(title)
    ? '技术方案'
    : /复盘|总结|报告/.test(title)
      ? '阶段复盘'
      : /运营|活动|内容/.test(title)
        ? '运营方案'
        : '专业协作'
  return {
    commonTitles: [
      `子标题优先使用“对象或章节角色＋${kind}动作”的短名词结构`,
      '同层级标题保持相同词序和相近长度，用动作词区分分析、设计、实施与复盘',
      '需要同时表达两个重点时使用“名词＋与＋名词”的并列结构',
    ],
    titleStyle: '子标题采用短名词结构，同层级保持相同词序和相近长度。',
    textStyle: '沿“目标与约束 → 方案与依据 → 风险与验证”推进；短段落先给判断，再补充适用范围和落法。',
    diagramStyle: '源记录没有保留图示证据，默认不生成图片；确需补图时使用 PlantUML，并只画正文已说明的对象、边界与关系。',
    writingGuidelines: [
      '习惯用“需要”直接声明必要动作或约束。',
      '习惯用“明确”要求把对象、范围和责任说具体。',
      '习惯用“确保”把动作落到预期结果。',
    ],
  }
}

function mapSectionHeadings(_item: any): CreationSkillSectionHeadings {
  return {
    ...DEFAULT_CREATION_SKILL_SECTION_HEADINGS,
  }
}

function mapFieldExamples(item: any, fallbackOnEmpty = true): CreationSkillFieldExamples {
  const normalize = (value: unknown, fallback: string[], key: string) => {
    const mapped = Array.isArray(value)
      ? value.map(entry => coerceCreationSkillStringItem(entry, key)).filter(Boolean)
      : []
    if (key === 'common_titles' || key === 'title_style') {
      const repaired = mapped.map(fictionalizeCreationSkillHeadingExample)
        .filter(isCompleteCreationSkillHeadingExample)
      return repaired.length ? repaired : fallbackOnEmpty ? [...fallback] : []
    }
    return mapped.length ? mapped : fallbackOnEmpty ? [...fallback] : []
  }
  return {
    commonTitles: normalize(item?.common_titles, DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.commonTitles, 'common_titles'),
    titleStyle: normalize(item?.title_style, DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.titleStyle, 'title_style'),
    textStyle: normalize(item?.text_style, DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.textStyle, 'text_style'),
    diagramStyle: normalize(item?.diagram_style, DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.diagramStyle, 'diagram_style'),
    writingGuidelines: normalize(item?.writing_guidelines, DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.writingGuidelines, 'writing_guidelines'),
  }
}

function mapDistinctiveSections(
  value: unknown,
  fallback: CreationSkillDistinctiveSection[] = [],
): CreationSkillDistinctiveSection[] {
  if (!Array.isArray(value)) return fallback.map(section => ({ ...section, examples: [...section.examples] }))
  const sections = value.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const source = item as Record<string, unknown>
    const examples = Array.isArray(source.examples)
      ? distinctCreationSkillItems(source.examples, 6)
      : []
    const section = {
      title: String(source.title || '').trim(),
      description: String(source.description || '').trim(),
      guidance: String(source.guidance || '').trim(),
      examples,
    }
    return section.title && section.description && section.guidance && section.examples.length
      ? section
      : null
  }).filter((section): section is CreationSkillDistinctiveSection => Boolean(section))
  return (sections.length ? sections : fallback).slice(0, 6)
}

function cloneFieldExamples(examples: CreationSkillFieldExamples): CreationSkillFieldExamples {
  return {
    commonTitles: [...examples.commonTitles],
    titleStyle: [...examples.titleStyle],
    textStyle: [...examples.textStyle],
    diagramStyle: [...examples.diagramStyle],
    writingGuidelines: [...examples.writingGuidelines],
  }
}
