export const CREATION_TOOLS_STORAGE_KEY = 'memory-bread_creation_tools_v1'

export const REQUIRED_CREATION_TOOL_IDS = [
  'internet_search',
  'memory_search',
  'data_search',
  'webpage_scrape',
] as const

export const OPTIONAL_CREATION_TOOL_IDS = [
  'plantuml_diagram',
  'github_search',
] as const

export type CreationToolId =
  | typeof REQUIRED_CREATION_TOOL_IDS[number]
  | typeof OPTIONAL_CREATION_TOOL_IDS[number]

export interface CreationToolDefinition {
  id: CreationToolId
  name: string
  summary: string
  capability: string
  required: boolean
  official: boolean
  resultLimit?: {
    defaultValue: number
    min: number
    max: number
  }
}

export interface CreationToolState {
  id: CreationToolId
  installed: boolean
  enabled: boolean
  resultLimit?: number
}

export const CREATION_TOOL_DEFINITIONS: readonly CreationToolDefinition[] = [
  {
    id: 'internet_search',
    name: '互联网检索 Tool',
    summary: '检索公开网页，为行业、政策、标准和时效性信息补充可核验来源。',
    capability: '按任务意图自动调用 · 外部资料保留来源链接',
    required: true,
    official: true,
  },
  {
    id: 'memory_search',
    name: '记忆搜索 Tool',
    summary: '检索本机记忆、知识和历史文档，让创作延续你的真实上下文。',
    capability: '本地执行 · 原始记忆不上传',
    required: true,
    official: true,
    resultLimit: { defaultValue: 10, min: 1, max: 30 },
  },
  {
    id: 'data_search',
    name: '数据检索 Tool',
    summary: '从本机数据模块召回报表来源与工作数据快照，并按采集时间判断可用性。',
    capability: '本地检索 · 返回时效与来源证据',
    required: true,
    official: true,
    resultLimit: { defaultValue: 30, min: 1, max: 50 },
  },
  {
    id: 'webpage_scrape',
    name: '网页爬取 Tool',
    summary: '按需刷新报表网页；优先复用现有 Chrome 登录会话，不保存浏览器 Cookie。',
    capability: 'Chrome 会话优先 · HTTP 降级',
    required: true,
    official: true,
  },
  {
    id: 'plantuml_diagram',
    name: 'PlantUML 画图 Tool',
    summary: '在架构、流程和时序类任务中生成可继续编辑的 PlantUML 图示代码。',
    capability: '按需调用 · 输出代码图示',
    required: false,
    official: false,
  },
  {
    id: 'github_search',
    name: 'GitHub 检索 Tool',
    summary: '搜索公开 GitHub 仓库，为技术选型和开源方案调研补充项目线索。',
    capability: '按需调用 · 仅检索公开仓库',
    required: false,
    official: false,
  },
]

const definitionById = new Map(
  CREATION_TOOL_DEFINITIONS.map(definition => [definition.id, definition]),
)

const isCreationToolId = (value: unknown): value is CreationToolId =>
  typeof value === 'string' && definitionById.has(value as CreationToolId)

const normalizeResultLimit = (
  definition: CreationToolDefinition,
  value: unknown,
): number | undefined => {
  if (!definition.resultLimit) return undefined
  const numeric = Number(value)
  const resolved = Number.isFinite(numeric)
    ? Math.round(numeric)
    : definition.resultLimit.defaultValue
  return Math.max(definition.resultLimit.min, Math.min(resolved, definition.resultLimit.max))
}

export const normalizeCreationTools = (value: unknown): CreationToolState[] => {
  const byId = new Map<CreationToolId, Partial<CreationToolState>>()
  if (Array.isArray(value)) {
    value.forEach((item) => {
      if (!item || typeof item !== 'object') return
      const candidate = item as Partial<CreationToolState>
      if (!isCreationToolId(candidate.id)) return
      byId.set(candidate.id, candidate)
    })
  }

  return CREATION_TOOL_DEFINITIONS.map((definition) => {
    const stored = byId.get(definition.id)
    const resultLimit = normalizeResultLimit(definition, stored?.resultLimit)
    if (definition.required) {
      return {
        id: definition.id,
        installed: true,
        enabled: true,
        ...(resultLimit == null ? {} : { resultLimit }),
      }
    }
    const installed = stored?.installed === true
    return {
      id: definition.id,
      installed,
      enabled: installed && stored?.enabled === true,
      ...(resultLimit == null ? {} : { resultLimit }),
    }
  })
}

export const loadCreationTools = (): CreationToolState[] => {
  try {
    if (typeof window === 'undefined') return normalizeCreationTools([])
    const raw = window.localStorage.getItem(CREATION_TOOLS_STORAGE_KEY)
    return normalizeCreationTools(raw ? JSON.parse(raw) : [])
  } catch {
    return normalizeCreationTools([])
  }
}

export const saveCreationTools = (tools: CreationToolState[]): CreationToolState[] => {
  const normalized = normalizeCreationTools(tools)
  try {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(CREATION_TOOLS_STORAGE_KEY, JSON.stringify(normalized))
    }
  } catch {
    // 本地存储不可用时仍保留当前会话状态。
  }
  return normalized
}

export const setCreationToolInstalled = (
  tools: CreationToolState[],
  id: CreationToolId,
  installed: boolean,
): CreationToolState[] => {
  const definition = definitionById.get(id)
  if (!definition || definition.required) return normalizeCreationTools(tools)
  return normalizeCreationTools(tools.map(tool => (
    tool.id === id
      ? { ...tool, installed, enabled: installed ? tool.enabled : false }
      : tool
  )))
}

export const setCreationToolEnabled = (
  tools: CreationToolState[],
  id: CreationToolId,
  enabled: boolean,
): CreationToolState[] => {
  const definition = definitionById.get(id)
  if (!definition || definition.required) return normalizeCreationTools(tools)
  return normalizeCreationTools(tools.map(tool => (
    tool.id === id && tool.installed
      ? { ...tool, enabled }
      : tool
  )))
}

export const setCreationToolResultLimit = (
  tools: CreationToolState[],
  id: CreationToolId,
  resultLimit: number,
): CreationToolState[] => {
  const definition = definitionById.get(id)
  if (!definition?.resultLimit) return normalizeCreationTools(tools)
  return normalizeCreationTools(tools.map(tool => (
    tool.id === id ? { ...tool, resultLimit } : tool
  )))
}

export const creationToolResultLimits = (tools: CreationToolState[]) => {
  const normalized = normalizeCreationTools(tools)
  const limitFor = (id: CreationToolId) => (
    normalized.find(tool => tool.id === id)?.resultLimit
    ?? definitionById.get(id)?.resultLimit?.defaultValue
    ?? 1
  )
  return {
    memorySearch: limitFor('memory_search'),
    dataSearch: limitFor('data_search'),
  }
}

export const enabledCreationToolIds = (tools: CreationToolState[]): CreationToolId[] =>
  normalizeCreationTools(tools)
    .filter(tool => tool.installed && tool.enabled)
    .map(tool => tool.id)
