import { create } from 'zustand'
import type { AccountType, ActionCommand, AuthSession, BakeTab, CloudBalance, CloudSubscription, CloudUser, RagContext, RepositoryTab, ServiceEnvironment, WindowMode } from '../types'

export interface BakeNavigationTarget {
  windowMode: WindowMode
  bakeTab?: BakeTab
  repositoryTab?: RepositoryTab
  selectedMemoryId?: string | null
  selectedTemplateId?: string | null
  selectedSopId?: string | null
  selectedKnowledgeId?: string | null
  selectedCaptureId?: string | null
  repositoryMemoryFocusId?: string | null
  bakeTemplateFocusId?: string | null
  bakeKnowledgeFocusId?: string | null
  bakeSopFocusId?: string | null
  repositoryCaptureSourceCaptureId?: string | null
}

type CaptureBackTarget = BakeNavigationTarget

export interface CreationModelConfig {
  id: string
  enabled: boolean
  apiKey: string
  baseUrl?: string
}

export interface CreationReferenceItem {
  id: number
  title: string
  doc_type: string
  final_weight: number
  relevance_score: number
  quality_score: number
  completeness_score: number
  usage_score: number
  format_score: number
  freshness_score: number
  usage_count: number
  reason: string
  summary?: string
  source_url?: string
}

export interface CreationReferencePreview {
  requirement: {
    topic: string
    doc_type: string
    audience: string
    style: string
    keywords: string[]
  }
  references: CreationReferenceItem[]
}

export interface CreationDataReferenceItem {
  source_id: number
  title: string
  source_kind: string
  freshness_class: string
  refresh_required: boolean
  can_use: boolean
  evidence_status?: string
  evidence_reason?: string
  unavailable_reason?: string
}

export interface CreationChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: number
  runId?: string
  runIds?: string[]
}

export interface CreationAgentEvent {
  schema_version: 'creation.agent.v1' | string
  event_id: string
  session_id: string
  run_id: string
  sequence: number
  timestamp: number
  type: string
  status: 'running' | 'waiting' | 'completed' | 'failed' | string
  actor: {
    kind: 'agent' | 'tool' | 'skill' | string
    id: string
    name: string
  }
  summary: string
  goal?: {
    objective: string
    status: string
    revision: number
    remaining_steps: string[]
    outcome?: string
  }
  environment_patch?: Record<string, unknown>
  data?: Record<string, unknown>
}

export interface CreationDraft {
  prompt: string
  docType: string
  audience: string
  generatedContent: string
  inheritFormat: boolean
  enableRag: boolean
  enableWebSearch: boolean
  enableImageGeneration: boolean
  contentWeight: number
  qualityWeight: number
  completenessWeight: number
  usageWeight: number
  formatWeight: number
  freshnessWeight: number
  referencePreview: CreationReferencePreview | null
  dataReferences: CreationDataReferenceItem[]
  sessionId: string | null
  rootRequest: string
  conversation: CreationChatMessage[]
  agentEvents: CreationAgentEvent[]
}

export interface CreationBackTarget {
  windowMode: WindowMode
  bakeTab?: BakeTab
  selectedTemplateId?: string | null
}

export interface AppState {
  // ── 窗口模式 ────────────────────────────────────────────────────────────────
  windowMode: WindowMode
  bakeTab: BakeTab
  repositoryTab: RepositoryTab
  selectedMemoryId: string | null
  selectedTemplateId: string | null
  selectedSopId: string | null
  selectedKnowledgeId: string | null
  selectedCaptureId: string | null
  repositoryMemoryFocusId: string | null
  bakeTemplateFocusId: string | null
  bakeKnowledgeFocusId: string | null
  bakeSopFocusId: string | null
  bakeMemoryOffset: number
  bakeKnowledgeOffset: number
  bakeKnowledgeQuery: string
  bakeKnowledgeFrom: string
  bakeKnowledgeTo: string
  bakeKnowledgeLimit: number
  bakeTemplateOffset: number
  bakeTemplateQuery: string
  bakeTemplateFrom: string
  bakeTemplateTo: string
  bakeTemplateLimit: number
  bakeSopOffset: number
  bakeSopQuery: string
  bakeSopFrom: string
  bakeSopTo: string
  bakeSopLimit: number
  bakeCaptureOffset: number
  repositoryMemoryQuery: string
  repositoryMemoryFrom: string
  repositoryMemoryTo: string
  repositoryMemoryLimit: number
  repositoryCaptureQuery: string
  repositoryCaptureFrom: string
  repositoryCaptureTo: string
  repositoryCaptureLimit: number
  repositoryCaptureSourceCaptureId: string | null
  captureBackTarget: CaptureBackTarget | null
  bakeNavigationStack: BakeNavigationTarget[]
  creationDraft: CreationDraft
  creationBackTarget: CreationBackTarget | null

  // ── RAG Panel ───────────────────────────────────────────────────────────────
  ragQuery:     string
  ragAnswer:    string
  ragContexts:  RagContext[]
  ragLoading:   boolean
  ragError:     string | null

  // ── Action Confirm ──────────────────────────────────────────────────────────
  pendingAction:    ActionCommand | null
  actionConfirmed:  boolean

  // ── 全局配置 ─────────────────────────────────────────────────────────────────
  apiBaseUrl:     string
  adminApiBaseUrl: string
  gatewayApiBaseUrl: string
  sidecarVersion: string
  accountType: AccountType
  serviceEnvironment: ServiceEnvironment
  debugModeEnabled: boolean
  /** Legacy read-only flag kept false for state compatibility. */
  localDebugModeEnabled: boolean
  authToken: string | null
  authExpiresAt: string | null
  currentUser: CloudUser | null
  cloudBalance: CloudBalance | null
  cloudSubscription: CloudSubscription | null

  // ── 首次引导 ─────────────────────────────────────────────────────────────────
  hasCompletedSetup: boolean
  setupSkipped:      boolean
  creationModelConfigs: CreationModelConfig[]

  // ── 操作方法 ─────────────────────────────────────────────────────────────────
  setWindowMode:         (mode: WindowMode) => void
  setBakeTab:            (tab: BakeTab) => void
  setRepositoryTab:      (tab: RepositoryTab) => void
  setSelectedMemoryId:  (id: string | null) => void
  setSelectedTemplateId: (id: string | null) => void
  setSelectedSopId:      (id: string | null) => void
  setSelectedKnowledgeId:(id: string | null) => void
  setSelectedCaptureId:  (id: string | null) => void
  setRepositoryMemoryFocusId: (id: string | null) => void
  setBakeTemplateFocusId: (id: string | null) => void
  setBakeKnowledgeFocusId: (id: string | null) => void
  setBakeSopFocusId: (id: string | null) => void
  setBakeMemoryOffset:  (offset: number) => void
  setBakeKnowledgeOffset:(offset: number) => void
  setBakeKnowledgeQuery: (query: string) => void
  setBakeKnowledgeFrom:  (value: string) => void
  setBakeKnowledgeTo:    (value: string) => void
  setBakeKnowledgeLimit: (limit: number) => void
  setBakeTemplateOffset: (offset: number) => void
  setBakeTemplateQuery:  (query: string) => void
  setBakeTemplateFrom:   (value: string) => void
  setBakeTemplateTo:     (value: string) => void
  setBakeTemplateLimit:  (limit: number) => void
  setBakeSopOffset:      (offset: number) => void
  setBakeSopQuery:       (query: string) => void
  setBakeSopFrom:        (value: string) => void
  setBakeSopTo:          (value: string) => void
  setBakeSopLimit:       (limit: number) => void
  setBakeCaptureOffset:  (offset: number) => void
  setRepositoryMemoryQuery: (query: string) => void
  setRepositoryMemoryFrom:  (value: string) => void
  setRepositoryMemoryTo:    (value: string) => void
  setRepositoryMemoryLimit: (limit: number) => void
  setRepositoryCaptureQuery: (query: string) => void
  setRepositoryCaptureFrom:  (value: string) => void
  setRepositoryCaptureTo:    (value: string) => void
  setRepositoryCaptureLimit: (limit: number) => void
  setRepositoryCaptureSourceCaptureId: (id: string | null) => void
  setCaptureBackTarget: (target: CaptureBackTarget | null) => void
  clearCaptureBackTarget: () => void
  pushBakeNavigationTarget: (target: BakeNavigationTarget) => void
  popBakeNavigationTarget: () => BakeNavigationTarget | null
  clearBakeNavigationStack: () => void
  setCreationDraft: (patch: Partial<CreationDraft>) => void
  resetCreationDraft: () => void
  setCreationBackTarget: (target: CreationBackTarget | null) => void
  clearCreationBackTarget: () => void
  setRagQuery:           (q: string) => void
  setRagResult:          (answer: string, contexts: RagContext[]) => void
  setRagLoading:         (loading: boolean) => void
  setRagError:           (err: string | null) => void
  setPendingAction:      (action: ActionCommand | null) => void
  confirmAction:         () => void
  cancelAction:          () => void
  setApiBaseUrl:         (url: string) => void
  setSidecarVersion:     (v: string) => void
  setAccountType:        (type: AccountType) => void
  setAuthSession:        (session: AuthSession) => void
  clearAuthSession:      () => void
  setCloudBalance:       (balance: CloudBalance | null) => void
  setCloudSubscription:  (subscription: CloudSubscription | null) => void
  setServiceEnvironment: (environment: ServiceEnvironment) => void
  setDebugModeEnabled: (enabled: boolean) => void
  setHasCompletedSetup:  (v: boolean) => void
  setSetupSkipped:       (v: boolean) => void
  setCreationModelConfigs: (configs: CreationModelConfig[]) => void
  setCreationModelConfig: (id: string, patch: Partial<CreationModelConfig>) => void
  reset:                 () => void
}

// v2 完成标记只会在一键初始化的质检与功能冒烟测试全部通过后写入。
// 旧引导的“已完成/已跳过”不能绕过新版强制门禁。
const SETUP_KEY = 'memory-bread_initialization_v2_done'
const SKIP_KEY  = 'memory-bread_setup_skipped'
export const AUTH_SESSION_KEY = 'memory-bread_auth_session'
export const ACCOUNT_TYPE_KEY = 'memory-bread_account_type'
export const SERVICE_ENVIRONMENT_KEY = 'memory-bread_service_env'
export const DEBUG_MODE_KEY = 'memory-bread_debug_mode_enabled'
export const CREATION_MODEL_KEY = 'memory-bread_creation_models'
export const CREATION_MODEL_PREFERENCE_KEY = 'creation.models'

const AUTH_SESSION_KEYS: Record<ServiceEnvironment, string> = {
  production: `${AUTH_SESSION_KEY}:production`,
  staging: `${AUTH_SESSION_KEY}:staging`,
}
const safeLocalStorage = typeof window !== 'undefined' && typeof window.localStorage?.getItem === 'function'
  ? window.localStorage
  : null

const DEFAULT_CREATION_MODELS: CreationModelConfig[] = [
  { id: 'mbcd-plus-v1', enabled: false, apiKey: '' },
  { id: 'mbcd-std-v1',  enabled: true,  apiKey: '' },
]
const DEFAULT_ADMIN_API_BASE_URLS: Record<ServiceEnvironment, string> = {
  production: 'https://memorybread.cn',
  staging: 'http://127.0.0.1:18080',
}
const DEFAULT_GATEWAY_API_BASE_URLS: Record<ServiceEnvironment, string> = {
  production: 'https://gateway.memorybread.cn',
  staging: 'http://127.0.0.1:18090',
}

const normalizeAccountType = (value?: string | null): AccountType =>
  value === 'platform_admin' || value === 'admin' ? 'platform_admin' : 'user'

const getBuildEnvValue = (key: string): string | null => {
  try {
    return (import.meta as unknown as { env?: Record<string, string | undefined> }).env?.[key] ?? null
  } catch {
    return null
  }
}

const getBuildAccountType = (): string | null => {
  return getBuildEnvValue('VITE_MEMORYBREAD_ACCOUNT_TYPE')
}

const serviceEndpoints = (
  environment: ServiceEnvironment,
): { adminApiBaseUrl: string; gatewayApiBaseUrl: string } => ({
  adminApiBaseUrl: DEFAULT_ADMIN_API_BASE_URLS[environment],
  gatewayApiBaseUrl: DEFAULT_GATEWAY_API_BASE_URLS[environment],
})

const isTruthyEnvValue = (value?: string | null): boolean =>
  ['1', 'true', 'yes', 'on', 'debug'].includes(String(value ?? '').trim().toLowerCase())

const getStartupDebugModeEnabled = (): boolean => {
  try {
    return isTruthyEnvValue((import.meta as unknown as { env?: Record<string, string | undefined> }).env?.VITE_MEMORYBREAD_DEBUG_MODE)
  } catch {
    return false
  }
}

const loadAuthSession = (environment: ServiceEnvironment): AuthSession | null => {
  const key = AUTH_SESSION_KEYS[environment]
  try {
    const raw = safeLocalStorage?.getItem(key)
      || (environment === 'production' ? safeLocalStorage?.getItem(AUTH_SESSION_KEY) : null)
    if (!raw) return null
    const session = JSON.parse(raw) as AuthSession
    if (!session.access_token || !session.user || Date.parse(session.expires_at) <= Date.now()) {
      safeLocalStorage?.removeItem(key)
      if (environment === 'production') safeLocalStorage?.removeItem(AUTH_SESSION_KEY)
      return null
    }
    safeLocalStorage?.setItem(key, raw)
    return session
  } catch {
    safeLocalStorage?.removeItem(key)
    if (environment === 'production') safeLocalStorage?.removeItem(AUTH_SESSION_KEY)
    return null
  }
}

export function normalizeCreationModels(models: CreationModelConfig[]): CreationModelConfig[] {
  const aliases: Record<string, string> = {
    'claude-opus-4-8': 'mbcd-plus-v1',
  }
  const byId = new Map<string, CreationModelConfig>()
  for (const model of models) {
    const id = aliases[model.id] || model.id
    if (!DEFAULT_CREATION_MODELS.some(defaultModel => defaultModel.id === id)) continue
    byId.set(id, { ...model, id })
  }
  let hasEnabled = false
  const normalized = DEFAULT_CREATION_MODELS.map(defaultModel => {
    const model = { ...defaultModel, ...byId.get(defaultModel.id) }
    if (!model.enabled) return model
    if (hasEnabled) return { ...model, enabled: false }
    hasEnabled = true
    return model
  })
  if (hasEnabled) return normalized
  return normalized.map(model =>
    model.id === 'mbcd-std-v1' ? { ...model, enabled: true } : model
  )
}

export function loadCreationModels(): CreationModelConfig[] {
  try {
    const raw = safeLocalStorage?.getItem(CREATION_MODEL_KEY)
    if (raw) return normalizeCreationModels(JSON.parse(raw))
  } catch { /* ignore */ }
  return normalizeCreationModels(DEFAULT_CREATION_MODELS)
}

const initialCreationDraft: CreationDraft = {
  prompt: '',
  docType: '',
  audience: '',
  generatedContent: '',
  inheritFormat: true,
  enableRag: true,
  enableWebSearch: true,
  enableImageGeneration: false,
  contentWeight: 45,
  qualityWeight: 15,
  completenessWeight: 15,
  usageWeight: 10,
  formatWeight: 10,
  freshnessWeight: 5,
  referencePreview: null,
  dataReferences: [],
  sessionId: null,
  rootRequest: '',
  conversation: [],
  agentEvents: [],
}

const startupDebugModeEnabled = getStartupDebugModeEnabled()
const initialDebugModeEnabled = startupDebugModeEnabled
const initialServiceEnvironment: ServiceEnvironment = initialDebugModeEnabled
  ? 'staging'
  : 'production'
const initialEndpoints = serviceEndpoints(initialServiceEnvironment)
const initialSession = loadAuthSession(initialServiceEnvironment)

const initialState = {
  windowMode:          'rag' as WindowMode,
  bakeTab:             'overview' as BakeTab,
  repositoryTab:       'memory' as RepositoryTab,
  selectedMemoryId:   null,
  selectedTemplateId:  null,
  selectedSopId:       null,
  selectedKnowledgeId: null,
  selectedCaptureId:   null,
  repositoryMemoryFocusId: null,
  bakeTemplateFocusId: null,
  bakeKnowledgeFocusId: null,
  bakeSopFocusId: null,
  bakeMemoryOffset:   0,
  bakeKnowledgeOffset: 0,
  bakeKnowledgeQuery:  '',
  bakeKnowledgeFrom:   '',
  bakeKnowledgeTo:     '',
  bakeKnowledgeLimit:  20,
  bakeTemplateOffset:  0,
  bakeTemplateQuery:   '',
  bakeTemplateFrom:    '',
  bakeTemplateTo:      '',
  bakeTemplateLimit:   20,
  bakeSopOffset:       0,
  bakeSopQuery:        '',
  bakeSopFrom:         '',
  bakeSopTo:           '',
  bakeSopLimit:        20,
  bakeCaptureOffset:   0,
  repositoryMemoryQuery: '',
  repositoryMemoryFrom:  '',
  repositoryMemoryTo:    '',
  repositoryMemoryLimit: 20,
  repositoryCaptureQuery: '',
  repositoryCaptureFrom:  '',
  repositoryCaptureTo:    '',
  repositoryCaptureLimit: 20,
  repositoryCaptureSourceCaptureId: null,
  captureBackTarget: null,
  bakeNavigationStack: [] as BakeNavigationTarget[],
  creationDraft: initialCreationDraft,
  creationBackTarget: null,
  ragQuery:            '',
  ragAnswer:           '',
  ragContexts:         [] as RagContext[],
  ragLoading:          false,
  ragError:            null,
  pendingAction:       null,
  actionConfirmed:     false,
  apiBaseUrl:          'http://127.0.0.1:7070',
  adminApiBaseUrl:     initialEndpoints.adminApiBaseUrl,
  gatewayApiBaseUrl:   initialEndpoints.gatewayApiBaseUrl,
  sidecarVersion:      '0.1.0',
  accountType:         normalizeAccountType(initialSession?.user.roles.includes('platform_admin') ? 'platform_admin' : safeLocalStorage?.getItem(ACCOUNT_TYPE_KEY) || getBuildAccountType()),
  serviceEnvironment:  initialServiceEnvironment,
  debugModeEnabled:    initialDebugModeEnabled,
  localDebugModeEnabled: false,
  authToken:           initialSession?.access_token ?? null,
  authExpiresAt:       initialSession?.expires_at ?? null,
  currentUser:         initialSession?.user ?? null,
  cloudBalance:        null,
  cloudSubscription:   null,
  hasCompletedSetup:   safeLocalStorage?.getItem(SETUP_KEY) === 'true',
  setupSkipped:        safeLocalStorage?.getItem(SKIP_KEY)  === 'true',
  creationModelConfigs: loadCreationModels(),
}

export const useAppStore = create<AppState>((set, get) => ({
  ...initialState,

  setWindowMode: (mode) => set({ windowMode: mode }),

  setBakeTab: (tab) => set({ bakeTab: tab }),

  setRepositoryTab: (tab) => set({ repositoryTab: tab }),

  setSelectedMemoryId: (id) => set({ selectedMemoryId: id }),

  setSelectedTemplateId: (id) => set({ selectedTemplateId: id }),

  setSelectedSopId: (id) => set({ selectedSopId: id }),

  setSelectedKnowledgeId: (id) => set({ selectedKnowledgeId: id }),

  setSelectedCaptureId: (id) => set({ selectedCaptureId: id }),

  setRepositoryMemoryFocusId: (id) => set({ repositoryMemoryFocusId: id, bakeMemoryOffset: 0 }),

  setBakeTemplateFocusId: (id) => set({ bakeTemplateFocusId: id, bakeTemplateOffset: 0 }),

  setBakeKnowledgeFocusId: (id) => set({ bakeKnowledgeFocusId: id, bakeKnowledgeOffset: 0 }),

  setBakeSopFocusId: (id) => set({ bakeSopFocusId: id, bakeSopOffset: 0 }),

  setBakeMemoryOffset: (offset) => set({ bakeMemoryOffset: offset }),

  setBakeKnowledgeOffset: (offset) => set({ bakeKnowledgeOffset: offset }),

  setBakeKnowledgeQuery: (query) => set({ bakeKnowledgeQuery: query, bakeKnowledgeOffset: 0 }),

  setBakeKnowledgeFrom: (value) => set({ bakeKnowledgeFrom: value, bakeKnowledgeOffset: 0 }),

  setBakeKnowledgeTo: (value) => set({ bakeKnowledgeTo: value, bakeKnowledgeOffset: 0 }),

  setBakeKnowledgeLimit: (limit) => set({ bakeKnowledgeLimit: limit, bakeKnowledgeOffset: 0 }),

  setBakeTemplateOffset: (offset) => set({ bakeTemplateOffset: offset }),

  setBakeTemplateQuery: (query) => set({ bakeTemplateQuery: query, bakeTemplateOffset: 0 }),

  setBakeTemplateFrom: (value) => set({ bakeTemplateFrom: value, bakeTemplateOffset: 0 }),

  setBakeTemplateTo: (value) => set({ bakeTemplateTo: value, bakeTemplateOffset: 0 }),

  setBakeTemplateLimit: (limit) => set({ bakeTemplateLimit: limit, bakeTemplateOffset: 0 }),

  setBakeSopOffset: (offset) => set({ bakeSopOffset: offset }),

  setBakeSopQuery: (query) => set({ bakeSopQuery: query, bakeSopOffset: 0 }),

  setBakeSopFrom: (value) => set({ bakeSopFrom: value, bakeSopOffset: 0 }),

  setBakeSopTo: (value) => set({ bakeSopTo: value, bakeSopOffset: 0 }),

  setBakeSopLimit: (limit) => set({ bakeSopLimit: limit, bakeSopOffset: 0 }),

  setBakeCaptureOffset: (offset) => set({ bakeCaptureOffset: offset }),

  setRepositoryMemoryQuery: (query) => set({ repositoryMemoryQuery: query, bakeMemoryOffset: 0 }),

  setRepositoryMemoryFrom: (value) => set({ repositoryMemoryFrom: value, bakeMemoryOffset: 0 }),

  setRepositoryMemoryTo: (value) => set({ repositoryMemoryTo: value, bakeMemoryOffset: 0 }),

  setRepositoryMemoryLimit: (limit) => set({ repositoryMemoryLimit: limit, bakeMemoryOffset: 0 }),

  setRepositoryCaptureQuery: (query) => set({ repositoryCaptureQuery: query, bakeCaptureOffset: 0 }),

  setRepositoryCaptureFrom: (value) => set({ repositoryCaptureFrom: value, bakeCaptureOffset: 0 }),

  setRepositoryCaptureTo: (value) => set({ repositoryCaptureTo: value, bakeCaptureOffset: 0 }),

  setRepositoryCaptureLimit: (limit) => set({ repositoryCaptureLimit: limit, bakeCaptureOffset: 0 }),

  setRepositoryCaptureSourceCaptureId: (id) => set({ repositoryCaptureSourceCaptureId: id, bakeCaptureOffset: 0 }),

  setCaptureBackTarget: (target) => set({ captureBackTarget: target }),

  clearCaptureBackTarget: () => set({ captureBackTarget: null }),

  pushBakeNavigationTarget: (target) => set((state) => ({
    bakeNavigationStack: [...state.bakeNavigationStack, target].slice(-20),
    captureBackTarget: target,
  })),

  popBakeNavigationTarget: () => {
    let popped: BakeNavigationTarget | null = null
    set((state) => {
      const next = [...state.bakeNavigationStack]
      popped = next.pop() ?? null
      return {
        bakeNavigationStack: next,
        captureBackTarget: next[next.length - 1] ?? null,
      }
    })
    return popped
  },

  clearBakeNavigationStack: () => set({
    bakeNavigationStack: [],
    captureBackTarget: null,
  }),

  setCreationDraft: (patch) => set((state) => ({
    creationDraft: { ...state.creationDraft, ...patch },
  })),

  resetCreationDraft: () => set({ creationDraft: initialCreationDraft }),

  setCreationBackTarget: (target) => set({ creationBackTarget: target }),

  clearCreationBackTarget: () => set({ creationBackTarget: null }),

  setRagQuery:   (q) => set({ ragQuery: q }),

  setRagResult:  (answer, contexts) => set({
    ragAnswer:  answer,
    ragContexts: contexts,
    ragLoading:  false,
    ragError:    null,
  }),

  setRagLoading: (loading) => set({ ragLoading: loading }),

  setRagError:   (err) => set({ ragError: err, ragLoading: false }),

  setPendingAction: (action) => set({
    pendingAction:   action,
    actionConfirmed: false,
  }),

  confirmAction: () => set({ actionConfirmed: true }),

  cancelAction:  () => set({
    pendingAction:   null,
    actionConfirmed: false,
  }),

  setApiBaseUrl:     (url) => set({ apiBaseUrl: url }),

  setSidecarVersion: (v) => set({ sidecarVersion: v }),

  setAccountType: (type) => {
    safeLocalStorage?.setItem(ACCOUNT_TYPE_KEY, type)
    set({ accountType: type })
  },

  setAuthSession: (session) => {
    const accountType = normalizeAccountType(session.user.roles.includes('platform_admin') ? 'platform_admin' : 'user')
    const environment = get().serviceEnvironment
    safeLocalStorage?.setItem(AUTH_SESSION_KEYS[environment], JSON.stringify(session))
    if (environment === 'production') safeLocalStorage?.removeItem(AUTH_SESSION_KEY)
    safeLocalStorage?.setItem(ACCOUNT_TYPE_KEY, accountType)
    set({
      authToken: session.access_token,
      authExpiresAt: session.expires_at,
      currentUser: session.user,
      accountType,
    })
  },

  clearAuthSession: () => {
    const environment = get().serviceEnvironment
    safeLocalStorage?.removeItem(AUTH_SESSION_KEYS[environment])
    if (environment === 'production') safeLocalStorage?.removeItem(AUTH_SESSION_KEY)
    safeLocalStorage?.setItem(ACCOUNT_TYPE_KEY, 'user')
    set({
      authToken: null,
      authExpiresAt: null,
      currentUser: null,
      cloudBalance: null,
      cloudSubscription: null,
      accountType: 'user',
    })
  },

  setCloudBalance: (balance) => set({ cloudBalance: balance }),

  setCloudSubscription: (subscription) => set({ cloudSubscription: subscription }),

  setServiceEnvironment: (environment) => set((state) => {
    const nextEnvironment: ServiceEnvironment = state.debugModeEnabled
      ? environment
      : 'production'
    const endpoints = serviceEndpoints(nextEnvironment)
    const session = loadAuthSession(nextEnvironment)
    const accountType = normalizeAccountType(
      session?.user.roles.includes('platform_admin') ? 'platform_admin' : 'user',
    )
    safeLocalStorage?.setItem(SERVICE_ENVIRONMENT_KEY, nextEnvironment)
    safeLocalStorage?.setItem(ACCOUNT_TYPE_KEY, accountType)
    return {
      serviceEnvironment: nextEnvironment,
      localDebugModeEnabled: false,
      ...endpoints,
      authToken: session?.access_token ?? null,
      authExpiresAt: session?.expires_at ?? null,
      currentUser: session?.user ?? null,
      cloudBalance: null,
      cloudSubscription: null,
      accountType,
    }
  }),

  setDebugModeEnabled: (enabled) => {
    safeLocalStorage?.setItem(DEBUG_MODE_KEY, String(enabled))
    const environment: ServiceEnvironment = enabled ? 'staging' : 'production'
    safeLocalStorage?.setItem(SERVICE_ENVIRONMENT_KEY, environment)
    const endpoints = serviceEndpoints(environment)
    const session = loadAuthSession(environment)
    const accountType = normalizeAccountType(
      session?.user.roles.includes('platform_admin') ? 'platform_admin' : 'user',
    )
    safeLocalStorage?.setItem(ACCOUNT_TYPE_KEY, accountType)
    set({
      debugModeEnabled: enabled,
      localDebugModeEnabled: false,
      serviceEnvironment: environment,
      ...endpoints,
      authToken: session?.access_token ?? null,
      authExpiresAt: session?.expires_at ?? null,
      currentUser: session?.user ?? null,
      cloudBalance: null,
      cloudSubscription: null,
      accountType,
    })
  },

  setHasCompletedSetup: (v) => {
    safeLocalStorage?.setItem(SETUP_KEY, String(v))
    set({ hasCompletedSetup: v })
  },

  setSetupSkipped: (v) => {
    safeLocalStorage?.setItem(SKIP_KEY, String(v))
    set({ setupSkipped: v })
  },

  setCreationModelConfigs: (configs) => {
    const normalized = normalizeCreationModels(configs)
    safeLocalStorage?.setItem(CREATION_MODEL_KEY, JSON.stringify(normalized))
    set({ creationModelConfigs: normalized })
  },

  setCreationModelConfig: (id, patch) => {
    set((state) => {
      const updated = normalizeCreationModels(state.creationModelConfigs.map(c =>
        c.id === id
          ? { ...c, ...patch }
          : patch.enabled === true
            ? { ...c, enabled: false }
            : c
      ))
      safeLocalStorage?.setItem(CREATION_MODEL_KEY, JSON.stringify(updated))
      return { creationModelConfigs: updated }
    })
  },

  reset: () => set(initialState),
}))

export const serviceEnvironmentHeaders = (
  environment: ServiceEnvironment = useAppStore.getState().serviceEnvironment,
): Record<string, string> => ({
  'X-MemoryBread-Environment': environment,
})
