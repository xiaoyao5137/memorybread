import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { AlertCircle, AtSign, Check, ChevronDown, ChevronRight, GripVertical, Loader2, Plus, Save, SlidersHorizontal, Trash2, Wand2, X } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { MentionHighlightInput, MentionHighlightTextarea } from './MentionHighlightField'
import {
  analyzeCreationSkill,
  categoryPathFor,
  CREATION_SKILL_AGENT_OPTIONS,
  CREATION_SKILL_TOOL_OPTIONS,
  DEFAULT_CREATION_SKILL_FIELD_EXAMPLES,
  fetchCreationSkillCategories,
  listLocalCreationSkills,
  saveLocalCreationSkill,
  type CreationSkillAnalysis,
  type CreationSkillCategory,
  type CreationSkillSource,
  type LocalCreationSkill,
} from '../utils/creationSkills'
import { toLocalApiError, toUserFacingError } from '../utils/userFacingError'
import './CreationSkillEditor.css'

interface CreationSkillEditorProps {
  source?: CreationSkillSource | null
  initialSkill?: LocalCreationSkill | null
  onClose: () => void
  onSaved: (skill: LocalCreationSkill) => void
}

interface SkillForm {
  title: string
  summary: string
  purpose: string
  documentTypes: string
  problems: string
  domains: string
  deliverables: string
  executionSteps: Array<{
    id: string
    title: string
    objective: string
    agents: string[]
    skills: string[]
    tools: string[]
    retainWebpageScreenshot: boolean
  }>
  categoryId: string
  commonTitles: string
  titleStyle: string
  textStyle: string
  diagramStyle: string
  writingGuidelines: string
  titleStyleHeading: string
  textStyleHeading: string
  diagramStyleHeading: string
  writingGuidelinesHeading: string
  commonTitleExamples: string
  titleStyleExamples: string
  textStyleExamples: string
  diagramStyleExamples: string
  writingGuidelineExamples: string
  distinctiveSections: Array<{
    title: string
    description: string
    guidance: string
    examples: string
  }>
  exampleDocument: string
}

const emptyForm: SkillForm = {
  title: '',
  summary: '',
  purpose: '',
  documentTypes: '',
  problems: '',
  domains: '',
  deliverables: '',
  executionSteps: [],
  categoryId: '',
  commonTitles: '',
  titleStyle: '',
  textStyle: '',
  diagramStyle: '',
  writingGuidelines: '',
  titleStyleHeading: '',
  textStyleHeading: '',
  diagramStyleHeading: '',
  writingGuidelinesHeading: '',
  commonTitleExamples: '',
  titleStyleExamples: '',
  textStyleExamples: '',
  diagramStyleExamples: '',
  writingGuidelineExamples: '',
  distinctiveSections: [],
  exampleDocument: '',
}

// 手工新建从空白开始；界面上只给灰色示例提示，不预填任何默认值。
const manualNewForm = (): SkillForm => ({ ...emptyForm })

// 原“Skill 描述”中的能力目标与解决的问题合并进技能简介，用于创作时召回技能。
const mergeSkillSummary = (summary: string, purpose: string, problems: string[]) => {
  const parts = [summary, purpose, ...problems].map(item => item.trim()).filter(Boolean)
  const merged: string[] = []
  parts.forEach(part => {
    if (!merged.some(item => item.includes(part) || part.includes(item))) merged.push(part)
  })
  return merged.join('\n').slice(0, 400)
}

// 旧协议的“步骤目标”与“步骤产出”在界面上合并为“执行动作”单字段；
// 编辑旧技能时把两段内容拼回一个文本框，保证信息不丢失。
const mergeStepAction = (objective: string, output: string) => {
  const goal = objective.trim()
  const deliverable = output.trim()
  if (!deliverable || goal.includes(deliverable)) return goal
  return goal ? `${goal}\n产出：${deliverable}` : deliverable
}

const toForm = (skill: LocalCreationSkill): SkillForm => ({
  title: skill.title,
  // 编辑态以已持久化的简介为准。旧版用途/问题字段若再次拼回输入框，
  // 用户删改后重新打开会看到旧内容回流，表现为“保存无效”。
  summary: skill.summary,
  purpose: skill.skillDescription.purpose,
  documentTypes: skill.skillDescription.documentTypes.join('\n'),
  problems: skill.skillDescription.problems.join('\n'),
  domains: skill.skillDescription.domains.join('\n'),
  deliverables: skill.skillDescription.deliverables.join('\n'),
  executionSteps: skill.executionSteps.map(step => withResourceMentions({
    id: step.id,
    title: step.title,
    objective: mergeStepAction(step.objective, step.output),
    agents: [...step.agents],
    skills: [...step.skills],
    tools: [...step.tools],
    retainWebpageScreenshot: step.retainWebpageScreenshot === true,
  }, skill.title)),
  categoryId: skill.categoryId || '',
  commonTitles: skill.commonTitles.join('\n'),
  titleStyle: skill.titleStyle,
  textStyle: skill.textStyle,
  diagramStyle: skill.diagramStyle,
  writingGuidelines: skill.writingGuidelines.join('\n'),
  titleStyleHeading: skill.sectionHeadings.titleStyle,
  textStyleHeading: skill.sectionHeadings.textStyle,
  diagramStyleHeading: skill.sectionHeadings.diagramStyle,
  writingGuidelinesHeading: skill.sectionHeadings.writingGuidelines,
  commonTitleExamples: skill.fieldExamples.commonTitles.join('\n'),
  titleStyleExamples: skill.fieldExamples.titleStyle.join('\n'),
  textStyleExamples: skill.fieldExamples.textStyle.join('\n'),
  diagramStyleExamples: skill.fieldExamples.diagramStyle.join('\n'),
  writingGuidelineExamples: skill.fieldExamples.writingGuidelines.join('\n'),
  distinctiveSections: (skill.distinctiveSections || []).map(section => ({
    title: section.title,
    description: section.description,
    guidance: section.guidance,
    examples: section.examples.join('\n'),
  })),
  exampleDocument: skill.exampleDocument,
})

const analysisToForm = (analysis: CreationSkillAnalysis): SkillForm => ({
  title: analysis.title,
  summary: mergeSkillSummary(analysis.summary, analysis.skillDescription.purpose, analysis.skillDescription.problems),
  purpose: analysis.skillDescription.purpose,
  documentTypes: analysis.skillDescription.documentTypes.join('\n'),
  problems: analysis.skillDescription.problems.join('\n'),
  domains: analysis.skillDescription.domains.join('\n'),
  deliverables: analysis.skillDescription.deliverables.join('\n'),
  executionSteps: analysis.executionSteps.map(step => withResourceMentions({
    id: step.id,
    title: step.title,
    objective: mergeStepAction(step.objective, step.output),
    agents: [...step.agents],
    skills: [...step.skills],
    tools: [...step.tools],
    retainWebpageScreenshot: step.retainWebpageScreenshot === true,
  }, analysis.title)),
  categoryId: '',
  commonTitles: analysis.commonTitles.join('\n'),
  titleStyle: analysis.titleStyle,
  textStyle: analysis.textStyle,
  diagramStyle: analysis.diagramStyle,
  writingGuidelines: analysis.writingGuidelines.join('\n'),
  titleStyleHeading: analysis.sectionHeadings.titleStyle,
  textStyleHeading: analysis.sectionHeadings.textStyle,
  diagramStyleHeading: analysis.sectionHeadings.diagramStyle,
  writingGuidelinesHeading: analysis.sectionHeadings.writingGuidelines,
  commonTitleExamples: analysis.fieldExamples.commonTitles.join('\n'),
  titleStyleExamples: analysis.fieldExamples.titleStyle.join('\n'),
  textStyleExamples: analysis.fieldExamples.textStyle.join('\n'),
  diagramStyleExamples: analysis.fieldExamples.diagramStyle.join('\n'),
  writingGuidelineExamples: analysis.fieldExamples.writingGuidelines.join('\n'),
  distinctiveSections: (analysis.distinctiveSections || []).map(section => ({
    title: section.title,
    description: section.description,
    guidance: section.guidance,
    examples: section.examples.join('\n'),
  })),
  exampleDocument: analysis.exampleDocument,
})

const lines = (value: string) => value.split('\n').map(item => item.trim()).filter(Boolean)

const agentLabel = (id: string) => CREATION_SKILL_AGENT_OPTIONS.find(option => option.id === id)?.label || id
const toolLabel = (id: string) => CREATION_SKILL_TOOL_OPTIONS.find(option => option.id === id)?.label || id

interface ResourceOption {
  kind: 'agent' | 'tool' | 'skill'
  id: string
  label: string
  description: string
}

// 能构成 @ 提及名称的字符；用于判断提及后是否已被空白/标点等边界结束。
const MENTION_NAME_CHAR = /[A-Za-z0-9_\u4e00-\u9fa5-]/

// 查找带边界的 @ 提及位置，避免把更长名称的前缀误判成短提及（如 @周报写作技能 里的 @周报）。
const findMentionIndex = (text: string, token: string) => {
  let position = text.indexOf(token)
  while (position >= 0) {
    const next = text[position + token.length]
    if (!next || !MENTION_NAME_CHAR.test(next)) return position
    position = text.indexOf(token, position + 1)
  }
  return -1
}

// 从步骤字段文本中解析用户手工 @ 出来的 Agent / Tool / Skill。
// 按最长名称优先消费，避免短技能名误判长提及的前缀；
// excludedTitles 用于排除技能自身名称等不应被当作 Skill 引用的文本。
const parseMentionedResources = (text: string, installedTitles: string[], excludedTitles: string[] = []) => {
  const agents: string[] = []
  const tools: string[] = []
  const skills: string[] = []
  let remaining = text
  const consume = (token: string, hit: () => void) => {
    if (findMentionIndex(remaining, token) < 0) return
    hit()
    remaining = remaining.split(token).join(' ')
  }
  ;[...CREATION_SKILL_AGENT_OPTIONS]
    .sort((left, right) => right.label.length - left.label.length)
    .forEach(option => consume(`@${option.label}`, () => agents.push(option.id)))
  ;[...CREATION_SKILL_TOOL_OPTIONS]
    .sort((left, right) => right.label.length - left.label.length)
    .forEach(option => consume(`@${option.label}`, () => tools.push(option.id)))
  installedTitles
    .filter(title => !excludedTitles.includes(title))
    .sort((left, right) => right.length - left.length)
    .forEach(title => consume(`@${title}`, () => {
      if (!skills.includes(title)) skills.push(title)
    }))
  Array.from(remaining.matchAll(/@([A-Za-z0-9_\u4e00-\u9fa5-]{2,40})/g)).forEach(match => {
    if (excludedTitles.includes(match[1])) return
    if (!skills.includes(match[1])) skills.push(match[1])
  })
  return { agents, tools, skills }
}

interface StepMentionSource {
  title: string
  objective: string
  agents: string[]
  skills: string[]
  tools: string[]
}

// 编辑旧技能时，把协议里已有的能力回写为 @ 提及文本，保证所见即所得。
// 技能自身名称不允许被回写成 Skill 提及。
const withResourceMentions = <T extends StepMentionSource>(step: T, selfTitle = ''): T => {
  const text = `${step.title}\n${step.objective}`
  const missing: string[] = []
  step.agents.forEach(id => {
    const label = agentLabel(id)
    if (findMentionIndex(text, `@${label}`) < 0) missing.push(`@${label}`)
  })
  step.tools.forEach(id => {
    const label = toolLabel(id)
    if (findMentionIndex(text, `@${label}`) < 0) missing.push(`@${label}`)
  })
  step.skills.forEach(name => {
    if (selfTitle && name === selfTitle) return
    if (findMentionIndex(text, `@${name}`) < 0) missing.push(`@${name}`)
  })
  if (!missing.length) return step
  const suffix = missing.join(' ')
  const objective = step.objective.trim() ? `${step.objective.trim()}\n${suffix}` : suffix
  return { ...step, objective }
}

// 基于已填写信息本地模拟一份示例文档，帮助用户预览结构与表达效果。
const buildExampleDocumentPreview = (form: SkillForm): string => {
  const titlePool = [...lines(form.commonTitleExamples), ...lines(form.commonTitles)]
  const headingPool = form.executionSteps.map(step => step.title.trim()).filter(Boolean)
  const headings = headingPool.length ? headingPool : ['背景与目标', '现状与约束', '方案设计', '实施计划', '风险与验证']
  const skillName = form.title.trim() || '本技能'
  const docTitle = titlePool[0] ? `${titlePool[0]}（模拟示例）` : `${skillName} · 虚构主题示例`
  const flowPool = [...lines(form.textStyle), ...lines(form.textStyleExamples)]
  const tonePool = [...lines(form.writingGuidelines), ...lines(form.writingGuidelineExamples)]
  const deliverablePool = lines(form.deliverables)
  const pick = (pool: string[], seed: number, fallback: string) => (pool.length ? pool[Math.abs(seed) % pool.length] : fallback)
  const purpose = form.purpose.trim() || '把输入的需求组织成结构统一、表达一致的完整文档。'
  const intro = pick(flowPool, 0, '先界定适用范围，再沿“现状 → 判断 → 动作 → 验证”逐层收束。')
  const tone = pick(tonePool, 0, '需要说明的是，目标对象只覆盖已经确认的适用范围。')

  const sections = headings.map((heading, index) => {
    const flow = pick(flowPool, index, intro)
    const toneLine = pick(tonePool, index + 1, tone)
    const deliverable = pick(deliverablePool, index + 1, '可被复核的阶段性产出')
    return [
      `## ${heading}`,
      '',
      `${flow}本节以完全虚构的主题演示该章节的推进方式，不引用任何真实业务信息。${toneLine}`,
      '',
      `这一节需要把判断落到动作与证据上，例如明确“${deliverable}”由谁确认、凭什么确认。`,
    ].join('\n')
  })

  const diagramRules = lines(form.diagramStyle)
  const diagramExamples = lines(form.diagramStyleExamples)
  const diagramSection = [
    '## 图示表达',
    '',
    diagramRules.length ? diagramRules.map(item => `- ${item}`).join('\n') : '- 需要配图时优先使用代码生成图，保持信息密度与正文一致。',
    ...(diagramExamples.length ? ['', '仿写示例：', ...diagramExamples.map(item => `- ${item}`)] : []),
  ].join('\n')

  const toneSection = [
    '## 话术表达要点',
    '',
    tonePool.length ? tonePool.slice(0, 5).map(item => `- ${item}`).join('\n') : `- ${tone}`,
  ].join('\n')

  return [
    `# ${docTitle}`,
    '',
    `> 本示例由已填写的技能信息在本机模拟生成，主题完全虚构，仅用于预览结构与表达效果。`,
    '',
    '## 摘要',
    '',
    `${purpose}示例按“${skillName}”的规则演示文档结构与表达。${intro}`,
    '',
    ...sections,
    '',
    diagramSection,
    '',
    toneSection,
    '',
  ].join('\n')
}

const fallbackNotice = (analysis: CreationSkillAnalysis) => {
  switch (analysis.fallbackReason) {
    case 'invalid_model_output':
      return '本地模型已完成推理，但返回格式未通过校验；已自动生成完整规则草稿并分析类目，请检查后保存。'
    case 'model_timeout':
      return '本地模型分析超时；已自动生成完整规则草稿并分析类目，请检查后保存。'
    case 'model_request_failed':
      return '本地模型请求未完成；已自动生成完整规则草稿并分析类目，请检查模型状态后保存。'
    case 'invalid_service_response':
      return '本地分析结果格式不完整；已自动生成完整规则草稿并分析类目，请检查后保存。'
    case 'analysis_request_failed':
      return '本地分析请求重试后仍未完成；已自动生成完整规则草稿并分析类目，请检查本地服务后保存。'
    default:
      return '本地模型分析未完成；已自动生成完整规则草稿并分析类目，请检查后保存。'
  }
}

export default function CreationSkillEditor({ source, initialSkill, onClose, onSaved }: CreationSkillEditorProps) {
  const apiBaseUrl = useAppStore(state => state.apiBaseUrl)
  const adminApiBaseUrl = useAppStore(state => state.adminApiBaseUrl)
  const isManualNew = !source && !initialSkill
  const [form, setForm] = useState<SkillForm>(() => (
    initialSkill ? toForm(initialSkill) : isManualNew ? manualNewForm() : emptyForm
  ))
  const [analysis, setAnalysis] = useState<CreationSkillAnalysis | null>(null)
  const [categories, setCategories] = useState<CreationSkillCategory[]>([])
  const [selectedPath, setSelectedPath] = useState<string[]>([])
  const [working, setWorking] = useState<'analyzing' | 'saving' | null>(source && !initialSkill ? 'analyzing' : null)
  const [categoryLoading, setCategoryLoading] = useState(true)
  const [categoryError, setCategoryError] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [savedSkill, setSavedSkill] = useState<LocalCreationSkill | null>(initialSkill || null)
  const [analysisProgress, setAnalysisProgress] = useState(initialSkill ? 100 : 6)
  const [draftSyncing, setDraftSyncing] = useState(false)
  const [installedSkills, setInstalledSkills] = useState<Array<{ title: string; summary: string }>>([])
  const [mention, setMention] = useState<{
    stepIndex: number
    field: 'title' | 'objective'
    query: string
  } | null>(null)
  const [mentionActiveIndex, setMentionActiveIndex] = useState(0)
  const mentionTargetRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  // 步骤字段共用一个输入法组合守卫：组合期内的 Enter 只服务输入法，不触发能力插入。
  const mentionImeGuard = useImeCompositionGuard<HTMLInputElement | HTMLTextAreaElement>()
  const [dragStepIndex, setDragStepIndex] = useState<number | null>(null)
  const dragStepIndexRef = useRef<number | null>(null)
  const workflowListRef = useRef<HTMLDivElement | null>(null)
  const [generatingExample, setGeneratingExample] = useState(false)
  // 关闭编辑器时中断仍在进行的创作 Agent 示例生成，避免请求悬挂。
  const exampleAbortRef = useRef<AbortController | null>(null)
  const draftSignatureRef = useRef('')
  const clientSkillKeyRef = useRef(
    initialSkill?.clientSkillKey || `creation-skill-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`,
  )
  const manualSourceIdRef = useRef(
    `manual-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`,
  )

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !working) onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => {
      window.removeEventListener('keydown', closeOnEscape)
      exampleAbortRef.current?.abort()
    }
  }, [onClose, working])

  // 右上角提示 6 秒后自动消失，也可点击关闭。
  useEffect(() => {
    if (!error && !message) return undefined
    const timer = window.setTimeout(() => {
      setError('')
      setMessage('')
    }, 6000)
    return () => window.clearTimeout(timer)
  }, [error, message])

  useEffect(() => {
    let cancelled = false
    setCategoryLoading(true)
    fetchCreationSkillCategories(adminApiBaseUrl)
      .then(items => {
        if (!cancelled) setCategories(items)
      })
      .catch(err => {
        if (!cancelled) setCategoryError(toUserFacingError(err, '创作类目加载失败'))
      })
      .finally(() => {
        if (!cancelled) setCategoryLoading(false)
      })
    return () => { cancelled = true }
  }, [adminApiBaseUrl])

  useEffect(() => {
    let cancelled = false
    listLocalCreationSkills(apiBaseUrl, { installed: true })
      .then(items => {
        if (!cancelled) setInstalledSkills(items.map(item => ({ title: item.title, summary: item.summary })))
      })
      .catch(() => {
        if (!cancelled) setInstalledSkills([])
      })
    return () => { cancelled = true }
  }, [apiBaseUrl])

  useEffect(() => {
    if (initialSkill || !source) return
    let cancelled = false
    let revealTimer: number | undefined
    setWorking('analyzing')
    setAnalysisProgress(6)
    setError('')
    analyzeCreationSkill(apiBaseUrl, source)
      .then(result => {
        if (cancelled) return
        setAnalysis(result)
        setForm(analysisToForm(result))
        setAnalysisProgress(100)
        revealTimer = window.setTimeout(() => {
          if (!cancelled) setWorking(null)
        }, 240)
      })
      .catch(err => {
        if (cancelled) return
        setError(toUserFacingError(err, '沉淀技能失败'))
        setWorking(null)
      })
    return () => {
      cancelled = true
      if (revealTimer) window.clearTimeout(revealTimer)
    }
  }, [apiBaseUrl, initialSkill, source])

  useEffect(() => {
    if (working !== 'analyzing' || analysisProgress >= 100) return
    const timer = window.setInterval(() => {
      setAnalysisProgress(current => {
        if (current >= 92) return current
        const increment = current < 32 ? 4 : current < 68 ? 2 : 1
        return Math.min(92, current + increment)
      })
    }, 420)
    return () => window.clearInterval(timer)
  }, [analysisProgress, working])

  useEffect(() => {
    if (!categories.length) return
    const path = categoryPathFor(categories, form.categoryId)
    if (path.length) setSelectedPath(path.map(item => item.id))
  }, [categories, form.categoryId])

  const optionsByLevel = useMemo(() => [1, 2, 3, 4].map(level => categories.filter(category => {
    if (category.level !== level) return false
    if (level === 1) return !category.parentId
    return category.parentId === selectedPath[level - 2]
  }).sort((left, right) => left.sortOrder - right.sortOrder || left.name.localeCompare(right.name, 'zh-CN'))), [categories, selectedPath])

  const selectedCategories = useMemo(() => selectedPath
    .map(id => categories.find(category => category.id === id))
    .filter((category): category is CreationSkillCategory => Boolean(category)), [categories, selectedPath])

  const updateField = <K extends keyof SkillForm,>(field: K, value: SkillForm[K]) => {
    setForm(prev => ({ ...prev, [field]: value }))
  }

  const updateSummary = (value: string) => {
    setForm(prev => ({
      ...prev,
      summary: value,
      // 旧协议字段已合并进技能简介。编辑简介时同步兼容字段，避免下游继续消费旧内容。
      purpose: value,
      problems: value.trim() ? value.slice(0, 240) : '',
    }))
  }

  const updateDistinctiveSection = (
    index: number,
    field: 'title' | 'description' | 'guidance' | 'examples',
    value: string,
  ) => setForm(prev => ({
    ...prev,
    distinctiveSections: prev.distinctiveSections.map((section, sectionIndex) => (
      sectionIndex === index ? { ...section, [field]: value } : section
    )),
  }))

  const updateExecutionStep = (
    index: number,
    field: 'title' | 'objective',
    value: string,
  ) => setForm(prev => ({
    ...prev,
    executionSteps: prev.executionSteps.map((step, stepIndex) => (
      stepIndex === index ? { ...step, [field]: value } : step
    )),
  }))

  // 正在编辑的技能自身名称不允许被 @ 提及。
  const selfSkillTitle = form.title.trim() || savedSkill?.title.trim() || ''
  const mentionHighlightLabels = useMemo(() => [
    ...CREATION_SKILL_AGENT_OPTIONS.map(option => option.label),
    ...CREATION_SKILL_TOOL_OPTIONS.map(option => option.label),
    ...installedSkills.map(item => item.title).filter(title => title !== selfSkillTitle),
  ], [installedSkills, selfSkillTitle])

  const stepResources = (step: SkillForm['executionSteps'][number]) => parseMentionedResources(
    `${step.title}\n${step.objective}`,
    installedSkills.map(item => item.title),
    selfSkillTitle ? [selfSkillTitle] : [],
  )

  const stepCapacityReached = (step: SkillForm['executionSteps'][number]) => {
    const current = stepResources(step)
    return current.agents.length + current.tools.length >= 4
  }

  const buildResourceOptions = (
    step: SkillForm['executionSteps'][number],
    query: string,
  ): ResourceOption[] => {
    const normalized = query.trim().replace(/^@/, '').trim().toLowerCase()
    const matches = (text: string) => !normalized || text.toLowerCase().includes(normalized)
    const current = stepResources(step)
    const capacityReached = current.agents.length + current.tools.length >= 4
    const items: ResourceOption[] = []
    // 已提及的 Agent/Tool 仍然展示，避免默认注入的官方能力（如记忆搜索）从选择器中消失；
    // 容量上限只拦截新增能力，重复选中已提及项不会产生重复文本。
    CREATION_SKILL_AGENT_OPTIONS
      .filter(option => matches(`${option.label} ${option.id}`))
      .filter(option => current.agents.includes(option.id) || !capacityReached)
      .forEach(option => items.push({
        kind: 'agent',
        id: option.id,
        label: option.label,
        description: current.agents.includes(option.id) ? 'Agent · 已提及' : 'Agent',
      }))
    CREATION_SKILL_TOOL_OPTIONS
      .filter(option => matches(`${option.label} ${option.id}`))
      .filter(option => current.tools.includes(option.id) || !capacityReached)
      .forEach(option => items.push({
        kind: 'tool',
        id: option.id,
        label: option.label,
        description: current.tools.includes(option.id) ? 'Tool · 已提及' : 'Tool',
      }))
    installedSkills
      .filter(item => item.title !== selfSkillTitle)
      .filter(item => !current.skills.includes(item.title) && matches(`${item.title} ${item.summary}`))
      .forEach(item => items.push({ kind: 'skill', id: item.title, label: item.title, description: item.summary || '已安装 Skill' }))
    const customName = query.trim().replace(/^@/, '').trim()
    if (
      customName
      && customName !== selfSkillTitle
      && !current.skills.includes(customName)
      && !items.some(item => item.kind === 'skill' && item.id === customName)
    ) {
      items.push({ kind: 'skill', id: customName, label: `将“${customName}”作为 Skill 名称引用`, description: '自定义 Skill 引用' })
    }
    return items
  }

  const handleStepFieldChange = (
    index: number,
    field: 'title' | 'objective',
    value: string,
    element: HTMLInputElement | HTMLTextAreaElement,
  ) => {
    updateExecutionStep(index, field, value)
    const caret = element.selectionStart ?? value.length
    // 查询词不允许空白：插入的 @ 提及自带尾随空格，继续编辑正文时不能被误判为
    // 正在进行中的 @ 查询，否则选择器会在打字时重新弹出、回车还会误选能力。
    const active = value.slice(0, caret).match(/@([^\s@]{0,40})$/)
    if (active) {
      setMention({ stepIndex: index, field, query: active[1] })
      setMentionActiveIndex(0)
      mentionTargetRef.current = element
    } else {
      setMention(null)
    }
  }

  const insertMention = (option: ResourceOption) => {
    if (!mention) return
    const { stepIndex, field } = mention
    const value = form.executionSteps[stepIndex]?.[field] ?? ''
    const element = mentionTargetRef.current
    const caret = element ? (element.selectionStart ?? value.length) : value.length
    const anchor = value.slice(0, caret).lastIndexOf('@')
    const start = anchor >= 0 ? anchor : caret
    // 已经提及过的能力不再重复插入，只清理当前 @ 输入并把光标移到已有提及之后。
    const token = `@${option.label}`
    const existingIndex = findMentionIndex(value, token)
    if (existingIndex >= 0) {
      setMention(null)
      setMentionActiveIndex(0)
      if (existingIndex === start) {
        // 正在输入的提及就是该能力：保留文本，仅补齐尾随空白分隔后续输入，
        // 避免把刚确认的提及整段删掉或与后续文字粘连。
        const tokenEnd = existingIndex + token.length
        const separator = value[tokenEnd] && !/\s/.test(value[tokenEnd]) ? ' ' : ''
        updateExecutionStep(stepIndex, field, `${value.slice(0, tokenEnd)}${separator}${value.slice(tokenEnd)}`)
        window.requestAnimationFrame(() => {
          const target = mentionTargetRef.current
          if (!target) return
          target.focus()
          const position = tokenEnd + separator.length
          try { target.setSelectionRange(position, position) } catch { /* 部分输入控件不支持选区 */ }
        })
        return
      }
      const end = existingIndex + token.length
      updateExecutionStep(stepIndex, field, `${value.slice(0, start)}${value.slice(caret)}`)
      window.requestAnimationFrame(() => {
        const target = mentionTargetRef.current
        if (!target) return
        target.focus()
        const position = Math.min(end, value.length - (caret - start))
        try { target.setSelectionRange(position, position) } catch { /* 部分输入控件不支持选区 */ }
      })
      return
    }
    const inserted = `@${option.label} `
    updateExecutionStep(stepIndex, field, `${value.slice(0, start)}${inserted}${value.slice(caret)}`)
    setMention(null)
    setMentionActiveIndex(0)
    window.requestAnimationFrame(() => {
      const target = mentionTargetRef.current
      if (!target) return
      target.focus()
      const position = start + inserted.length
      try { target.setSelectionRange(position, position) } catch { /* 部分输入控件不支持选区 */ }
    })
  }

  const handleStepFieldKeyDown = (
    event: ReactKeyboardEvent<HTMLInputElement | HTMLTextAreaElement>,
    step: SkillForm['executionSteps'][number],
  ) => {
    if (!mention) return
    // 输入法组合期间的 Enter / 方向键只服务输入法，否则确认候选字会误触发能力插入。
    if (mentionImeGuard.isImeEvent(event)) return
    const options = buildResourceOptions(step, mention.query)
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setMentionActiveIndex(current => (options.length ? (current + 1) % options.length : 0))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setMentionActiveIndex(current => (options.length ? (current - 1 + options.length) % options.length : 0))
    } else if (event.key === 'Enter' && options.length) {
      event.preventDefault()
      insertMention(options[Math.min(mentionActiveIndex, options.length - 1)])
    } else if (event.key === 'Escape') {
      // 只关闭选择器；阻止冒泡，避免同时触发弹窗层的 Escape 关闭整个编辑器。
      event.preventDefault()
      event.stopPropagation()
      setMention(null)
    }
  }

  const addExecutionStep = () => setForm(prev => ({
    ...prev,
    executionSteps: [
      ...prev.executionSteps,
      {
        id: `custom-step-${prev.executionSteps.length + 1}`,
        title: '',
        objective: '',
        agents: [],
        skills: [],
        tools: [],
        retainWebpageScreenshot: false,
      },
    ],
  }))

  const removeExecutionStep = (index: number) => setForm(prev => ({
    ...prev,
    executionSteps: prev.executionSteps.filter((_, stepIndex) => stepIndex !== index),
  }))

  const moveExecutionStep = (from: number, to: number) => setForm(prev => {
    if (from === to || from < 0 || to < 0 || from >= prev.executionSteps.length || to >= prev.executionSteps.length) {
      return prev
    }
    const next = [...prev.executionSteps]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    return { ...prev, executionSteps: next }
  })

  // WKWebView 对 button 元素的 HTML5 draggable 支持不稳定，改用指针事件实现拖拽排序。
  const beginStepDrag = (event: { preventDefault: () => void }, index: number) => {
    if (form.executionSteps.length <= 1) return
    event.preventDefault()
    dragStepIndexRef.current = index
    setDragStepIndex(index)
  }

  const draggingStep = dragStepIndex !== null
  useEffect(() => {
    if (!draggingStep) return undefined
    const handleMove = (event: MouseEvent) => {
      const current = dragStepIndexRef.current
      const container = workflowListRef.current
      if (current === null || !container) return
      const items = Array.from(
        container.querySelectorAll<HTMLElement>(':scope > .creation-skill-workflow-step'),
      )
      if (!items.length) return
      let target = items.length - 1
      for (let i = 0; i < items.length; i += 1) {
        const rect = items[i].getBoundingClientRect()
        if (event.clientY < rect.top + rect.height / 2) {
          target = i
          break
        }
      }
      if (target === current) return
      dragStepIndexRef.current = target
      setDragStepIndex(target)
      moveExecutionStep(current, target)
    }
    const handleEnd = () => {
      dragStepIndexRef.current = null
      setDragStepIndex(null)
    }
    window.addEventListener('mousemove', handleMove)
    window.addEventListener('mouseup', handleEnd)
    return () => {
      window.removeEventListener('mousemove', handleMove)
      window.removeEventListener('mouseup', handleEnd)
    }
  }, [draggingStep])

  const addDistinctiveSection = () => setForm(prev => ({
    ...prev,
    distinctiveSections: [
      ...prev.distinctiveSections,
      { title: '', description: '', guidance: '', examples: '' },
    ],
  }))

  const removeDistinctiveSection = (index: number) => setForm(prev => ({
    ...prev,
    distinctiveSections: prev.distinctiveSections.filter((_, sectionIndex) => sectionIndex !== index),
  }))

  // 把当前表单组装成创作 Agent 认识的 Skill 载荷，字段口径与创作面板一致。
  // exampleDocument 传空，避免 Agent 照抄旧示例而不是生成新主题。
  const buildExampleSkillPayload = () => {
    const input = buildLocalInput(false, undefined, 'draft', false)
    return {
      id: input.clientSkillKey,
      title: input.title,
      summary: input.summary,
      skillDescription: input.skillDescription,
      executionSteps: input.executionSteps,
      titleDesignStyle: input.commonTitles,
      writingDesign: input.textStyle,
      imageGeneration: input.diagramStyle,
      voiceStyle: input.writingGuidelines,
      fieldExamples: {
        titleDesignStyle: input.fieldExamples.commonTitles,
        writingDesign: input.fieldExamples.textStyle,
        imageGeneration: input.fieldExamples.diagramStyle,
        voiceStyle: input.fieldExamples.writingGuidelines,
      },
      exampleDocument: '',
    }
  }

  // 读取创作 Agent 事件流：document.delta 实时回填文本框，run.completed 给出终稿。
  const runExampleAgentStream = async (
    response: Response,
    onDelta: (document: string) => void,
  ): Promise<string> => {
    const reader = response.body?.getReader()
    if (!reader) throw new Error('无法读取创作 Agent 事件流')
    const decoder = new TextDecoder()
    let buffer = ''
    let streamed = ''
    let finalDocument = ''
    const processLine = (line: string) => {
      if (!line.startsWith('data: ')) return
      const event = JSON.parse(line.slice(6)) as {
        type?: string
        summary?: string
        actor?: { id?: string }
        data?: Record<string, unknown>
      }
      if (event.type === 'agent.completed'
        && event.actor?.id === 'document_writer_agent') {
        streamed = ''
      }
      if (event.type === 'document.delta') {
        streamed += String(event.data?.content || '')
        onDelta(streamed)
      }
      if (event.type === 'document.replaced') {
        streamed = String(event.data?.content || '')
        onDelta(streamed)
      }
      if (event.type === 'run.completed') {
        finalDocument = String(event.data?.document || '')
      }
      if (event.type === 'run.failed') {
        throw new Error(String(event.summary || '创作 Agent 生成示例失败'))
      }
    }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const streamLines = buffer.split('\n')
      buffer = streamLines.pop() || ''
      streamLines.forEach(processLine)
    }
    if (buffer.trim()) processLine(buffer.trim())
    return finalDocument.trim() || streamed.trim()
  }

  // 示例由创作 Agent 即时生成：把上方已填写的内容作为 Skill 传入，
  // Agent 按配方复刻写法并生成完全虚构主题的完整示例文档。
  const generateExampleDocument = async () => {
    if (generatingExample) return
    const skillPayload = buildExampleSkillPayload()
    if (!skillPayload.title) {
      setError('请先填写技能标题，再让创作 Agent 生成示例')
      return
    }
    setGeneratingExample(true)
    setError('')
    setMessage('')
    const abort = new AbortController()
    exampleAbortRef.current = abort
    try {
      const response = await fetch(`${apiBaseUrl}/api/creation/agent/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abort.signal,
        body: JSON.stringify({
          user_prompt: [
            `请为创作 Skill「${skillPayload.title}」生成一份完整的示例文档，用于该技能的 few-shot 示例。`,
            '主题必须完全虚构，不得出现任何真实的公司、项目、产品、人员、地域、日期和金额。',
            '严格按照该 Skill 的执行步骤、标题句式、惯用话术和图示方式撰写，不要把鲜明风格稀释成通用公文。',
            '正文篇幅约一千二百至两千二百个中文字符，包含主标题、摘要、至少六个二级章节和结论。',
            '只输出 Markdown 文档本身，不要输出任何解释。',
          ].join('\n'),
          design_templates: [],
          doc_type: skillPayload.skillDescription.documentTypes[0] || '',
          audience: '',
          output_format: 'markdown',
          inherit_format: false,
          enable_rag: false,
          enable_web_search: false,
          enable_image_generation: false,
          max_references: 0,
          current_document: '',
          conversation: [],
          selected_skills: [skillPayload],
          model_mode: 'local',
          confirmed: true,
        }),
      })
      if (!response.ok) {
        throw new Error(`创作 Agent 启动失败: ${response.status}`)
      }
      const document = await runExampleAgentStream(response, streamed => {
        setForm(prev => ({ ...prev, exampleDocument: streamed }))
      })
      if (!document) throw new Error('创作 Agent 没有返回示例内容')
      setForm(prev => ({ ...prev, exampleDocument: document }))
      setMessage('示例已由创作 Agent 按当前配方生成，可继续手工微调。')
    } catch (err) {
      if (abort.signal.aborted) return
      // Agent 不可用时退回本机模拟预览，保证用户始终能拿到可编辑的示例。
      setForm(prev => ({ ...prev, exampleDocument: buildExampleDocumentPreview(prev) }))
      setError(toUserFacingError(err, '创作 Agent 生成示例失败，已改用本机模拟预览'))
    } finally {
      if (exampleAbortRef.current === abort) exampleAbortRef.current = null
      setGeneratingExample(false)
    }
  }

  const renderMentionPicker = (
    step: SkillForm['executionSteps'][number],
    index: number,
    field: 'title' | 'objective',
  ) => {
    if (!mention || mention.stepIndex !== index || mention.field !== field) return null
    const options = buildResourceOptions(step, mention.query)
    return (
      <div className="creation-skill-mention-picker" role="listbox" aria-label={`执行步骤 ${index + 1} 选择能力`}>
        <header><AtSign size={14} /><span>选择可调用的 Agent / Tool / Skill</span><small>{options.length} 项</small></header>
        {options.length ? options.map((option, optionIndex) => (
          <button
            key={`${option.kind}-${option.id}`}
            type="button"
            role="option"
            aria-selected={optionIndex === mentionActiveIndex}
            className={optionIndex === mentionActiveIndex ? 'is-active' : ''}
            onMouseDown={event => event.preventDefault()}
            onClick={() => insertMention(option)}
          >
            <strong>@{option.label}</strong>
            <span>{option.description}</span>
          </button>
        )) : (
          <p className="creation-skill-mention-picker__empty">{stepCapacityReached(step) ? '本步新增 Agent 与 Tool 已达上限，仍可选择已提及能力或输入名称添加 Skill。' : '没有匹配的能力，可直接输入 Skill 名称后回车引用。'}</p>
        )}
      </div>
    )
  }

  const selectCategory = (levelIndex: number, value: string) => {
    const next = [...selectedPath.slice(0, levelIndex), value].filter(Boolean)
    setSelectedPath(next)
    setForm(prev => ({ ...prev, categoryId: levelIndex === 3 ? value : '' }))
  }

  // “私有”是客户端默认类目：categoryId 为空即私有，技能只留在本机；
  // 发布到市场前必须选择四级真实类目。
  const selectPrivateCategory = () => {
    setSelectedPath([])
    setForm(prev => ({ ...prev, categoryId: '' }))
  }

  const buildLocalInput = (
    published: boolean,
    cloudSkillId = savedSkill?.cloudSkillId,
    status: LocalCreationSkill['status'] = savedSkill?.status || 'draft',
    installed = savedSkill?.installed || false,
  ) => {
    const resolvedSource: CreationSkillSource = source || (savedSkill ? {
      kind: savedSkill.sourceKind,
      id: savedSkill.sourceId,
      title: savedSkill.title,
      content: '',
      docType: '',
    } : {
      kind: 'manual',
      id: manualSourceIdRef.current,
      title: form.title.trim() || '手工新建技能',
      content: '',
      docType: '',
    })
    const titleDesign = lines(form.commonTitles)
    const titleExamples = lines(form.commonTitleExamples)
    // 界面不再单独录入 Skill 描述字段；优先沿用旧数据，缺失时用技能简介与所选类目推导，保持服务端契约完整。
    const categoryName = categories.find(item => item.id === form.categoryId)?.name.trim() || ''
    const documentTypeLabel = categoryName || '目标文档'
    const summaryText = form.summary.trim()
    return {
      clientSkillKey: savedSkill?.clientSkillKey || clientSkillKeyRef.current,
      cloudSkillId: cloudSkillId || null,
      sourceKind: resolvedSource.kind,
      sourceId: resolvedSource.id,
      title: form.title.trim(),
      summary: summaryText,
      categoryId: form.categoryId || null,
      skillDescription: {
        purpose: summaryText || form.purpose.trim(),
        documentTypes: lines(form.documentTypes).length ? lines(form.documentTypes) : [documentTypeLabel],
        problems: lines(form.problems).length ? lines(form.problems) : [summaryText.slice(0, 240)].filter(Boolean),
        domains: lines(form.domains),
        deliverables: lines(form.deliverables).length ? lines(form.deliverables) : [`一份结构完整、依据清楚的${documentTypeLabel}`],
      },
      executionSteps: form.executionSteps.map((step, index) => {
        const resources = stepResources(step)
        return {
          id: step.id.trim() || `custom-step-${index + 1}`,
          title: step.title.trim(),
          objective: step.objective.trim(),
          // “执行动作”合并了目标与产出；旧协议的 output 兼容留空。
          output: '',
          agents: resources.agents,
          skills: resources.skills,
          tools: resources.tools,
          retainWebpageScreenshot: step.retainWebpageScreenshot,
        }
      }),
      commonTitles: titleDesign,
      // 旧版协议仍要求 titleStyle；新界面不再展示独立字段，只镜像标题设计风格。
      titleStyle: titleDesign.join('；').slice(0, 1200),
      textStyle: form.textStyle.trim(),
      diagramStyle: form.diagramStyle.trim(),
      writingGuidelines: lines(form.writingGuidelines),
      distinctiveSections: form.distinctiveSections.map(section => ({
        title: section.title.trim(),
        description: section.description.trim(),
        guidance: section.guidance.trim(),
        examples: lines(section.examples),
      })),
      sectionHeadings: {
        commonTitles: '标题设计风格',
        titleStyle: '标题设计风格',
        textStyle: '行文设计思路',
        diagramStyle: '图片生成方式',
        writingGuidelines: '话术表达风格',
      },
      fieldExamples: {
        commonTitles: titleExamples,
        titleStyle: titleExamples,
        textStyle: lines(form.textStyleExamples),
        diagramStyle: lines(form.diagramStyleExamples),
        writingGuidelines: lines(form.writingGuidelineExamples),
      },
      exampleDocument: form.exampleDocument.trim(),
      status,
      installed,
      published,
    }
  }

  // 保存只强制技能标题、技能简介与执行步骤；创作配方与完整示例文档允许留空。
  const validate = (
    requiresCategory: boolean,
    status: LocalCreationSkill['status'] = savedSkill?.status || 'draft',
  ) => {
    const input = buildLocalInput(Boolean(savedSkill?.published), savedSkill?.cloudSkillId, status)
    if (!input.title.trim()) throw new Error('请填写技能标题')
    if (!input.summary.trim()) throw new Error('请填写技能简介')
    const executionComplete = input.executionSteps.length > 0
      && input.executionSteps.every(step => (
        step.title
        && step.objective
        && step.agents.length + step.tools.length <= 4
      ))
    if (!executionComplete) throw new Error('请补全执行步骤：每步需填写步骤标题与执行动作')
    if (requiresCategory && !input.categoryId) throw new Error('发布到市场需要选择非“私有”的创作类目')
    return input
  }

  useEffect(() => {
    const isDraft = savedSkill?.status === 'draft' || (!savedSkill && Boolean(analysis))
    if (!isDraft || working) return
    const input = buildLocalInput(false, savedSkill?.cloudSkillId, 'draft', false)
    const executionComplete = input.executionSteps.length > 0
      && input.executionSteps.every(step => (
        step.title
        && step.objective
        && step.agents.length + step.tools.length <= 4
      ))
    if (!input.title || !input.summary || !executionComplete) return
    const signature = JSON.stringify(input)
    if (signature === draftSignatureRef.current) return
    const timer = window.setTimeout(() => {
      draftSignatureRef.current = signature
      setDraftSyncing(true)
      saveLocalCreationSkill(apiBaseUrl, input, savedSkill?.id)
        .then(saved => {
          setSavedSkill(saved)
          onSaved(saved)
        })
        .catch(err => {
          draftSignatureRef.current = ''
          setError(toLocalApiError(err, '自动保存技能草稿失败'))
        })
        .finally(() => setDraftSyncing(false))
    }, 700)
    return () => window.clearTimeout(timer)
  }, [analysis, apiBaseUrl, form, onSaved, savedSkill, working])

  const saveSkill = async () => {
    setWorking('saving')
    setError('')
    setMessage('')
    try {
      const input = validate(false, 'saved')
      const saved = await saveLocalCreationSkill(apiBaseUrl, input, savedSkill?.id)
      setSavedSkill(saved)
      onSaved(saved)
      setMessage('保存成功')
    } catch (err) {
      setError(toLocalApiError(err, '保存技能失败'))
    } finally {
      setWorking(null)
    }
  }

  const sourceLabel = source?.title || (isManualNew ? '手工录入' : '既有文档')
  const busy = working !== null
  const progressLabel = analysisProgress < 30
    ? '正在读取标题与章节层级'
    : analysisProgress < 68
      ? '正在提炼标题句式、行文思路与惯用话术'
      : analysisProgress < 94
        ? '正在归纳适用场景与创作类目'
        : '正在生成可编辑草稿'

  return (
    <div className="creation-skill-modal" role="dialog" aria-modal="true" aria-labelledby="creation-skill-title">
      <div className="creation-skill-editor">
        <header className="creation-skill-editor__header">
          <div>
            {!initialSkill && <span>{source ? `沉淀自：${sourceLabel}` : '来源：手工录入'}</span>}
            <h2 id="creation-skill-title">{initialSkill ? '技能编辑' : isManualNew ? '新建技能' : '沉淀技能'}</h2>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="关闭技能编辑器"><X /></button>
        </header>

        {working === 'analyzing' ? (
          <div className="creation-skill-editor__state" aria-live="polite">
            <Loader2 className="spin" />
            <strong>正在本机分析文档写法</strong>
            <span>{progressLabel}</span>
            <div
              className="creation-skill-analysis-progress"
              role="progressbar"
              aria-label="本机分析进度"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={analysisProgress}
            >
              <div><span style={{ width: `${analysisProgress}%` }} /></div>
              <strong>{analysisProgress}%</strong>
            </div>
          </div>
        ) : (
          <div className="creation-skill-editor__body">
            {isManualNew && (
              <div className="creation-skill-notice creation-skill-notice--warning">
                <AlertCircle size={17} /> 手工新建从空白开始，各字段的灰色文字仅为示例提示，请填写你自己的写法规则；带“每行一个”的字段按行录入。
              </div>
            )}
            {analysis?.analysisMode && analysis.analysisMode !== 'local_model' && (
              <div className="creation-skill-notice creation-skill-notice--warning">
                <AlertCircle size={17} /> {fallbackNotice(analysis)}
              </div>
            )}
            <div className="creation-skill-form-grid">
              <label><span>技能标题</span><input value={form.title} maxLength={80} placeholder="例如：技术架构设计文档写作法" onChange={event => updateField('title', event.target.value)} /></label>
              <label className="creation-skill-field--wide"><span>技能简介 <small>写明能力目标与解决的问题，用于创作时召回这枚技能</small></span><textarea rows={4} value={form.summary} maxLength={400} placeholder="例如：把架构评审文档的写法沉淀为可复用规则，用于产出可评审的技术架构设计文档，解决系统边界与关键取舍不清的问题。" onChange={event => updateSummary(event.target.value)} /></label>
            </div>

            <section className="creation-skill-workflow-card">
              <div className="creation-skill-workflow-heading">
                <div>
                  <span>按顺序执行</span>
                  <h4>执行工作流</h4>
                </div>
                <button type="button" onClick={addExecutionStep} disabled={form.executionSteps.length >= 12}>
                  <Plus size={15} /> 添加步骤
                </button>
              </div>

              {form.executionSteps.length === 0 && (
                <div className="creation-skill-workflow-empty">
                  还没有执行步骤，点击右上角“添加步骤”开始编排。
                </div>
              )}

              <div className={`creation-skill-workflow${draggingStep ? ' is-reordering' : ''}`} ref={workflowListRef}>
                {form.executionSteps.map((step, index) => (
                  <article
                    className={`creation-skill-workflow-step${dragStepIndex === index ? ' is-dragging' : ''}`}
                    key={`${step.id}-${index}`}
                  >
                    <header>
                      <button
                        type="button"
                        className="creation-skill-workflow-step-drag"
                        aria-label={`拖拽调整执行步骤 ${index + 1} 的顺序`}
                        title="按住拖动调整顺序"
                        onMouseDown={event => beginStepDrag(event, index)}
                      >
                        <GripVertical size={15} />
                      </button>
                      <span>{String(index + 1).padStart(2, '0')}</span>
                      <div className="creation-skill-mention-anchor">
                        <MentionHighlightInput
                          aria-label={`执行步骤 ${index + 1} 标题`}
                          value={step.title}
                          mentionLabels={mentionHighlightLabels}
                          maxLength={80}
                          placeholder="例如：开展行业调研，输入 @ 可提及能力"
                          onChange={event => handleStepFieldChange(index, 'title', event.target.value, event.target)}
                          onKeyDown={event => handleStepFieldKeyDown(event, step)}
                          onCompositionStart={mentionImeGuard.onCompositionStart}
                          onCompositionEnd={mentionImeGuard.onCompositionEnd}
                          onBlur={() => { mentionImeGuard.onBlur(); setMention(null) }}
                        />
                        {renderMentionPicker(step, index, 'title')}
                      </div>
                      <button type="button" aria-label={`删除执行步骤 ${index + 1}`} onClick={() => removeExecutionStep(index)} disabled={form.executionSteps.length <= 1}>
                        <Trash2 size={14} />
                      </button>
                    </header>
                    <label>
                      <span>执行动作 <small>写明本步做什么、产出及数量/篇幅要求；输入 @ 提及能力</small></span>
                      <div className="creation-skill-mention-anchor">
                        <MentionHighlightTextarea mentionLabels={mentionHighlightLabels} rows={3} maxLength={500} aria-label={`执行步骤 ${index + 1} 执行动作`} placeholder="例如：展开关键组件，至少形成 3 个子章节，每节不少于 80 字；用 @方案设计 Agent 完成方案。" value={step.objective} onChange={event => handleStepFieldChange(index, 'objective', event.target.value, event.target)} onKeyDown={event => handleStepFieldKeyDown(event, step)} onCompositionStart={mentionImeGuard.onCompositionStart} onCompositionEnd={mentionImeGuard.onCompositionEnd} onBlur={() => { mentionImeGuard.onBlur(); setMention(null) }} />
                        {renderMentionPicker(step, index, 'objective')}
                      </div>
                    </label>
                    {stepResources(step).tools.includes('data_search') && (
                      <label className="creation-skill-webpage-screenshot-option">
                        <input
                          type="checkbox"
                          checked={step.retainWebpageScreenshot}
                          onChange={event => setForm(prev => ({
                            ...prev,
                            executionSteps: prev.executionSteps.map((item, stepIndex) => (
                              stepIndex === index
                                ? { ...item, retainWebpageScreenshot: event.target.checked }
                                : item
                            )),
                          }))}
                        />
                        <span>保留证据截图</span>
                      </label>
                    )}
                  </article>
                ))}
              </div>
            </section>

            {/* 创作类目与配方字段合并进一个大折叠项，默认整体折叠；展开后各字段默认展开可直接编辑 */}
            <details className="creation-skill-recipe-more creation-skill-collapsible">
              <summary>
                <SlidersHorizontal size={16} aria-hidden="true" />
                <h3>创作类目与写作配方</h3>
                <ChevronDown size={16} aria-hidden="true" />
              </summary>
              <div className="creation-skill-recipe-grid">
              <details className="creation-skill-recipe-field creation-skill-collapsible creation-skill-categories-field">
                <summary>
                  <div><h3>创作类目</h3></div>
                  <ChevronDown size={16} aria-hidden="true" />
                </summary>
                <fieldset className="creation-skill-categories">
                  <legend>创作类目</legend>
                  <p>默认为“私有”，技能只保存在本机；要发布到技能市场，需依次选择一级行业 → 二级细分行业 → 三级工种 → 四级具体文档类型。</p>
                  {categoryLoading ? (
                    <div className="creation-skill-category-skeleton" aria-live="polite" aria-label="正在加载创作类目">
                      {[0, 1, 2, 3].map(column => (
                        <div key={column}>
                          <span />
                          <i /><i /><i />
                        </div>
                      ))}
                    </div>
                  ) : categoryError ? (
                    <span className="creation-skill-inline-state creation-skill-inline-state--error">{categoryError}</span>
                  ) : (
                    <div className="creation-skill-category-grid" aria-label="创作类目四级选择">
                      {['一级行业', '二级细分行业', '三级工种', '四级文档类型'].map((label, index) => (
                        <section
                          className={`creation-skill-category-level${index > 0 && !selectedPath[index - 1] ? ' is-disabled' : ''}`}
                          key={label}
                          aria-labelledby={`creation-skill-category-level-${index + 1}`}
                        >
                          <header>
                            <span id={`creation-skill-category-level-${index + 1}`}><b>{index + 1}</b>{label}</span>
                            <small>{optionsByLevel[index].length} 项</small>
                          </header>
                          <div className="creation-skill-category-options" role="listbox" aria-label={label}>
                            {index === 0 && (
                              <button
                                type="button"
                                className={`creation-skill-category-option creation-skill-category-option--private${selectedPath.length === 0 && !form.categoryId ? ' is-selected' : ''}`}
                                role="option"
                                aria-selected={selectedPath.length === 0 && !form.categoryId}
                                onClick={selectPrivateCategory}
                              >
                                <span>私有</span>
                                {selectedPath.length === 0 && !form.categoryId && <Check size={15} aria-hidden="true" />}
                              </button>
                            )}
                            {index > 0 && !selectedPath[index - 1] ? (
                              <span className="creation-skill-category-empty">请先选择上一级</span>
                            ) : optionsByLevel[index].length === 0 ? (
                              <span className="creation-skill-category-empty">暂无可选类目</span>
                            ) : optionsByLevel[index].map(item => {
                              const selected = selectedPath[index] === item.id
                              return (
                                <button
                                  type="button"
                                  className={`creation-skill-category-option${selected ? ' is-selected' : ''}`}
                                  role="option"
                                  aria-selected={selected}
                                  key={item.id}
                                  onClick={() => selectCategory(index, item.id)}
                                >
                                  <span>{item.name}</span>
                                  {selected
                                    ? <Check size={15} aria-hidden="true" />
                                    : index < 3 && <ChevronRight size={14} aria-hidden="true" />}
                                </button>
                              )
                            })}
                          </div>
                        </section>
                      ))}
                      <div className={`creation-skill-category-path${form.categoryId ? ' is-complete' : ''}`} aria-live="polite">
                        <strong>{form.categoryId ? '已选类目' : '当前类目'}</strong>
                        <span>{selectedCategories.length > 0 ? selectedCategories.map(item => item.name).join(' / ') : '私有'}</span>
                        {form.categoryId && <Check size={15} aria-hidden="true" />}
                      </div>
                    </div>
                  )}
                </fieldset>
              </details>
              <details className="creation-skill-recipe-field creation-skill-collapsible">
                <summary>
                  <div><h3>标题设计风格</h3></div>
                  <ChevronDown size={16} aria-hidden="true" />
                </summary>
                <label><span>源文档特征 <small>每行一条</small></span><textarea aria-label="标题设计风格提炼结果" rows={5} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.commonTitles.join('\n例如：')}`} value={form.commonTitles} onChange={event => updateField('commonTitles', event.target.value)} /></label>
                <label className="creation-skill-example-field"><span>源标题脱敏仿写 <small>只替换敏感主客体</small></span><textarea aria-label="标题设计风格示例" rows={3} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.commonTitles.join('\n例如：')}`} value={form.commonTitleExamples} onChange={event => updateField('commonTitleExamples', event.target.value)} /></label>
              </details>
              <details className="creation-skill-recipe-field creation-skill-collapsible">
                <summary>
                  <div><h3>行文设计思路</h3></div>
                  <ChevronDown size={16} aria-hidden="true" />
                </summary>
                <label><span>源文档如何推进</span><textarea aria-label="行文设计思路提炼结果" rows={5} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.textStyle.join('\n例如：')}`} value={form.textStyle} onChange={event => updateField('textStyle', event.target.value)} /></label>
                <label className="creation-skill-example-field"><span>行文仿写示例 <small>每行一个</small></span><textarea aria-label="行文设计思路示例" rows={3} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.textStyle.join('\n例如：')}`} value={form.textStyleExamples} onChange={event => updateField('textStyleExamples', event.target.value)} /></label>
              </details>
              <details className="creation-skill-recipe-field creation-skill-collapsible">
                <summary>
                  <div><h3>图片生成方式</h3></div>
                  <ChevronDown size={16} aria-hidden="true" />
                </summary>
                <label><span>启用条件、选型、信息、布局、视觉、图文衔接与自检</span><textarea aria-label="图片生成方式提炼结果" rows={8} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.diagramStyle.join('\n例如：')}`} value={form.diagramStyle} onChange={event => updateField('diagramStyle', event.target.value)} /></label>
                <label className="creation-skill-example-field"><span>代码生图示例 <small>当前支持 PlantUML、Mermaid 等</small></span><textarea aria-label="图片生成方式示例" rows={3} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.diagramStyle.join('\n例如：')}`} value={form.diagramStyleExamples} onChange={event => updateField('diagramStyleExamples', event.target.value)} /></label>
              </details>
              <details className="creation-skill-recipe-field creation-skill-collapsible">
                <summary>
                  <div><h3>话术表达风格</h3></div>
                  <ChevronDown size={16} aria-hidden="true" />
                </summary>
                <label><span>原词证据、使用位置、表达作用与边界 <small>每行一条完整规则</small></span><textarea aria-label="话术表达风格提炼结果" rows={8} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.writingGuidelines.join('\n例如：')}`} value={form.writingGuidelines} onChange={event => updateField('writingGuidelines', event.target.value)} /></label>
                <label className="creation-skill-example-field"><span>话术迁移示例 <small>每行一个</small></span><textarea aria-label="话术表达风格示例" rows={3} placeholder={`例如：${DEFAULT_CREATION_SKILL_FIELD_EXAMPLES.writingGuidelines.join('\n例如：')}`} value={form.writingGuidelineExamples} onChange={event => updateField('writingGuidelineExamples', event.target.value)} /></label>
              </details>
              <details className="creation-skill-collapsible creation-skill-distinctive">
                <summary>
                  <div>
                    <h3>特色亮点</h3>
                  </div>
                  <ChevronDown size={16} aria-hidden="true" />
                </summary>
                <div className="creation-skill-distinctive-body">
                  <button type="button" onClick={addDistinctiveSection} disabled={form.distinctiveSections.length >= 6}>
                    <Plus size={15} /> 添加特色章节
                  </button>
                  {form.distinctiveSections.length === 0 && (
                    <p className="creation-skill-distinctive-empty">暂无特色亮点；可点击“添加特色章节”手动补充。</p>
                  )}
                  {form.distinctiveSections.map((section, index) => (
                <section className="creation-skill-recipe-field creation-skill-recipe-field--distinctive" key={index}>
                  <header>
                    <span>{String(index + 5).padStart(2, '0')} / 特色亮点</span>
                    <div>
                      <input
                        aria-label={`特色亮点 ${index + 1} 标题`}
                        value={section.title}
                        maxLength={80}
                        placeholder="例如：定义先行的概念建立"
                        onChange={event => updateDistinctiveSection(index, 'title', event.target.value)}
                      />
                      <button type="button" aria-label={`删除特色亮点 ${index + 1}`} onClick={() => removeDistinctiveSection(index)}>
                        <Trash2 size={15} />
                      </button>
                    </div>
                  </header>
                  <label>
                    <span>特征说明</span>
                    <textarea rows={4} placeholder="例如：核心概念首次出现时先给通俗定义。" value={section.description} onChange={event => updateDistinctiveSection(index, 'description', event.target.value)} />
                  </label>
                  <label>
                    <span>复刻指引 <small>包含适用位置、步骤与边界</small></span>
                    <textarea rows={4} placeholder="例如：放在方案设计章节开头，先定义再补充边界。" value={section.guidance} onChange={event => updateDistinctiveSection(index, 'guidance', event.target.value)} />
                  </label>
                  <label className="creation-skill-example-field">
                    <span>完整仿写示例 <small>每行一个</small></span>
                    <textarea rows={3} placeholder="例如：协作工作台可以理解为任务流转的统一入口。" value={section.examples} onChange={event => updateDistinctiveSection(index, 'examples', event.target.value)} />
                  </label>
                </section>
                  ))}
                </div>
              </details>
            </div>
            </details>

            {/* 生成预览独立于折叠区，始终直接展示 */}
            <section className="creation-skill-recipe-field creation-skill-recipe-field--document">
                <header>
                  <span>{String(form.distinctiveSections.length + 5).padStart(2, '0')} / 完整示例文档</span>
                  <h3>生成预览</h3>
                  <button type="button" className="creation-skill-example-generate" onClick={() => void generateExampleDocument()} disabled={generatingExample}>
                    {generatingExample ? <Loader2 className="spin" size={15} /> : <Wand2 size={15} />}
                    {generatingExample ? '创作 Agent 正在生成…' : '生成示例'}
                  </button>
                </header>
                <label><span>完整示例</span><textarea aria-label="完整示例文档" rows={18} placeholder="先填写上方的创作配方，再点击“生成示例”预览效果；也可以直接粘贴 Markdown。" value={form.exampleDocument} onChange={event => updateField('exampleDocument', event.target.value)} /></label>
              </section>

            {(savedSkill?.status === 'draft' || draftSyncing) && (
              <div className="creation-skill-draft-state" aria-live="polite">
                {draftSyncing ? <Loader2 className="spin" size={15} /> : <Check size={15} />}
                <span>{draftSyncing ? '正在自动保存草稿…' : '草稿已自动保存在本机，点击「保存技能」后才会进入可安装状态。'}</span>
              </div>
            )}
          </div>
        )}

        <footer className="creation-skill-editor__footer">
          <button type="button" className="creation-skill-button" onClick={() => void saveSkill()} disabled={busy || working === 'analyzing'}>
            {working === 'saving' ? <Loader2 className="spin" /> : <Save />} {savedSkill?.status === 'saved' ? '保存修改' : '保存技能'}
          </button>
        </footer>
      </div>

      {/* 提示统一放在弹窗右上角，避免底部报错被长表单遮挡 */}
      {(error || message) && (
        <div className="creation-skill-toast-zone" aria-live="assertive">
          {error && (
            <div className="creation-skill-toast creation-skill-toast--error" role="alert">
              <AlertCircle size={16} />
              <span>{error}</span>
              <button type="button" onClick={() => setError('')} aria-label="关闭错误提示"><X size={13} /></button>
            </div>
          )}
          {message && (
            <div className="creation-skill-toast creation-skill-toast--success">
              <Check size={16} />
              <span>{message}</span>
              <button type="button" onClick={() => setMessage('')} aria-label="关闭成功提示"><X size={13} /></button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
