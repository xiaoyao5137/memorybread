import React, { useEffect, useRef, useState } from 'react'
import type { ModelEntry } from '../types'
import { CREATION_MODEL_PREFERENCE_KEY, serviceEnvironmentHeaders, useAppStore } from '../store/useAppStore'
import type { CreationModelConfig } from '../store/useAppStore'
import { fetchBillingBalance } from '../utils/authApi'
import { REMOTE_CREATION_MODEL_ID, canUseRemoteCreationModel } from '../utils/modelSelection'
import { toUserFacingError } from '../utils/userFacingError'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import { useConfirmDialog } from './useConfirmDialog'

const SIDECAR = 'http://127.0.0.1:7071'
const MODEL_LOAD_RETRY_DELAYS_MS = [0, 1_000, 2_000, 4_000]

const wait = (delayMs: number) => new Promise<void>(resolve => window.setTimeout(resolve, delayMs))

type OllamaSetupDetail = {
  message?: string
  is_macos?: boolean
  system_version?: string
  arch?: string
  ollama_installed?: boolean
  ollama_running?: boolean
  ollama_version?: string
  brew_available?: boolean
  can_auto_install?: boolean
  version_compatible?: boolean
  minimum_macos_major?: number
  official_download_url?: string
  recommended_install_method?: string
}

const PROVIDER_LABEL: Record<string, string> = {
  memorybread: '本地运行',
  ollama: '本地运行', huggingface: '本地向量',
  openai: '云端能力', anthropic: '云端能力',
  gateway: '云端创作',
  tongyi: '云端能力', doubao: '云端能力', deepseek: '云端能力', kimi: '云端能力',
  google: '云端能力', kling: '云端能力',
}
const PROVIDER_COLOR: Record<string, string> = {
  memorybread: '#007AFF',
  ollama: '#007AFF', huggingface: '#FF9500',
  openai: '#34C759', anthropic: '#AF52DE',
  gateway: '#AF52DE',
  tongyi: '#FF6B35', doubao: '#1677FF', deepseek: '#06B6D4', kimi: '#8B5CF6',
  google: '#4285F4', kling: '#FF2D55',
}
const CATEGORY_LABEL: Record<string, string> = {
  llm: '分析模型', embedding: '向量模型', image: '生图模型', ocr: 'OCR', asr: '语音识别', vlm: '视觉模型',
  inference_engine: '运行环境',
}
const STATUS_COLOR: Record<string, string> = {
  not_installed: '#AEAEB2', downloading: '#FF9500', loading: '#FF9500',
  installed: '#34C759', active: '#007AFF', error: '#FF3B30',
}
const STATUS_LABEL: Record<string, string> = {
  not_installed: '未安装', downloading: '下载中', loading: '加载中',
  installed: '已安装', active: '使用中', error: '错误',
}

const ANALYSIS_MODEL_ALIASES = new Set([
  'mbem-v1-local',
])

const VECTOR_MODEL_ALIASES = new Set([
  'bge-small-zh',
])

function normalizeVisibleModels(items: ModelEntry[]): ModelEntry[] {
  const normalized: ModelEntry[] = []
  let hasAnalysisModel = false
  let hasVectorModel = false

  for (const model of items) {
    if (model.category === 'llm') {
      if (!ANALYSIS_MODEL_ALIASES.has(model.id)) continue
      if (hasAnalysisModel) continue
      normalized.push({
        ...model,
        name: 'MBEM v1.0',
        description: 'MemoryBread Extract Model Local 1.0，本地提炼模型 v1，用于采集内容理解、知识提炼和本地咨询分析',
        size_gb: model.size_gb || 3.4,
        tags: ['推荐', '本地', '采集分析'],
      })
      hasAnalysisModel = true
      continue
    }

    if (model.category === 'embedding') {
      if (!VECTOR_MODEL_ALIASES.has(model.id)) continue
      if (hasVectorModel) continue
      normalized.push({
        ...model,
        id: 'bge-small-zh',
        name: 'MBEMB V1.0',
        description: 'MemoryBread向量模型',
      })
      hasVectorModel = true
      continue
    }

    // 云端创作只通过 MemoryBread 品牌模型配置，不显示供应商模型或密钥入口。
    if (model.category === 'image' || model.requires_api_key) continue

    // 推理引擎由首次初始化自动托管，不作为独立模型暴露给用户。
  }

  return normalized
}

// ── API Key 配置弹窗 ──────────────────────────────────────────────────────────
const ApiKeyDialog: React.FC<{
  model: ModelEntry
  onClose: () => void
  onSaved: () => void
}> = ({ model, onClose, onSaved }) => {
  const [values, setValues] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const [error, setError] = useState('')
  const [validMsg, setValidMsg] = useState('')
  const fields = model.api_key_fields || []

  const handleSave = async () => {
    const missing = fields.filter(f => f.required && !values[f.key])
    if (missing.length) { setError(`请填写：${missing.map(f => f.label).join('、')}`); return }
    setSaving(true); setError('')
    try {
      const r = await fetch(`${SIDECAR}/api/models/${model.id}/configure`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields: values }),
      })
      const d = await r.json()
      if (d.status !== 'ok') throw new Error(d.message)
      onSaved(); onClose()
    } catch (e: unknown) { setError(toUserFacingError(e, '保存失败，请稍后重试')) } finally { setSaving(false) }
  }

  const handleValidate = async () => {
    setValidating(true); setValidMsg(''); setError('')
    try {
      await fetch(`${SIDECAR}/api/models/${model.id}/configure`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields: values }),
      })
      const r = await fetch(`${SIDECAR}/api/models/${model.id}/validate`, { method: 'POST' })
      const d = await r.json()
      if (d.valid) setValidMsg('✓ ' + d.message)
      else setError(toUserFacingError(d.message, '配置保存失败，请稍后重试'))
    } catch (e: unknown) { setError(toUserFacingError(e, '验证失败，请稍后重试')) } finally { setValidating(false) }
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }}>
      <div style={{ background: 'white', borderRadius: 16, padding: 24, width: 380, boxShadow: '0 20px 60px rgba(0,0,0,0.2)' }}>
        <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 4 }}>配置 {model.name}</div>
        <div style={{ fontSize: 12, color: '#6E6E73', marginBottom: 18 }}>{model.description}</div>
        {fields.map(f => (
          <div key={f.key} style={{ marginBottom: 14 }}>
            <label style={{ fontSize: 12, color: '#6E6E73', display: 'block', marginBottom: 4 }}>
              {f.label}{f.required && <span style={{ color: '#FF3B30' }}> *</span>}
            </label>
            <input
              type={f.secret ? 'password' : 'text'}
              placeholder={f.placeholder}
              value={values[f.key] || ''}
              onChange={e => setValues(v => ({ ...v, [f.key]: e.target.value }))}
              style={{
                width: '100%', padding: '9px 12px', borderRadius: 8, fontSize: 13,
                border: '1px solid rgba(0,0,0,0.15)', outline: 'none', boxSizing: 'border-box',
              }}
            />
          </div>
        ))}
        {error && <div style={{ fontSize: 12, color: '#FF3B30', marginBottom: 10 }}>{error}</div>}
        {validMsg && <div style={{ fontSize: 12, color: '#34C759', marginBottom: 10 }}>{validMsg}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', marginTop: 4 }}>
          <button onClick={handleValidate} disabled={validating} style={btn('#F2F2F7', '#333', 12)}>
            {validating ? '验证中...' : '验证 Key'}
          </button>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={onClose} style={btn('#F2F2F7', '#333')}>取消</button>
            <button onClick={handleSave} disabled={saving} style={btn('#007AFF', 'white')}>
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── 模型体验对话弹窗 ──────────────────────────────────────────────────────────
type ChatMessage = { role: 'user' | 'assistant'; content: string }

const ModelChatDialog: React.FC<{
  model: ModelEntry
  onClose: () => void
}> = ({ model, onClose }) => {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [inputValue, setInputValue] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [chatError, setChatError] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const chatInputImeGuard = useImeCompositionGuard<HTMLInputElement>()

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }
  useEffect(scrollToBottom, [messages])

  const handleSend = async () => {
    const q = inputValue.trim()
    if (!q || chatLoading) return
    setInputValue('')
    setChatError('')
    const userMsg: ChatMessage = { role: 'user', content: q }
    setMessages(prev => [...prev, userMsg])
    setChatLoading(true)

    try {
      const chatMessages = [...messages, userMsg].map(m => ({ role: m.role, content: m.content }))
      const response = await fetch(`${SIDECAR}/api/models/${model.id}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: chatMessages }),
      })

      if (!response.ok) {
        const errData = await response.json().catch(() => ({ message: `HTTP ${response.status}` }))
        setChatError(toUserFacingError(errData.message, '模型响应失败，请稍后重试'))
        setChatLoading(false)
        return
      }

      // 流式读取 SSE
      const reader = response.body?.getReader()
      if (!reader) {
        setChatError('无法读取响应流')
        setChatLoading(false)
        return
      }

      const decoder = new TextDecoder()
      let assistantContent = ''
      setMessages(prev => [...prev, { role: 'assistant', content: '' }])

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const chunk = decoder.decode(value, { stream: true })
        const lines = chunk.split('\n')
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const dataStr = line.slice(6).trim()
          if (!dataStr) continue
          try {
            const evt = JSON.parse(dataStr)
            if (evt.content) {
              assistantContent += evt.content
              setMessages(prev => {
                const updated = [...prev]
                updated[updated.length - 1] = { role: 'assistant', content: assistantContent }
                return updated
              })
            }
            if (evt.error) {
              setChatError(toUserFacingError(evt.error, '模型响应失败，请稍后重试'))
            }
            if (evt.done) {
              // 流式结束
            }
          } catch { /* ignore parse errors */ }
        }
      }
    } catch (e: unknown) {
      setChatError(toUserFacingError(e, '连接失败，请稍后重试'))
    } finally {
      setChatLoading(false)
    }
  }

  const handleReset = () => {
    setMessages([])
    setChatError('')
    setInputValue('')
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }}>
      <div style={{
        background: 'white', borderRadius: 16, width: 520, maxHeight: '80vh',
        boxShadow: '0 20px 60px rgba(0,0,0,0.2)', display: 'flex', flexDirection: 'column',
      }}>
        {/* 标题栏 */}
        <div style={{
          padding: '12px 16px', borderBottom: '1px solid rgba(0,0,0,0.07)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 15, fontWeight: 700 }}>体验 {model.name}</span>
            <span style={{
              fontSize: 10, padding: '1px 6px', borderRadius: 4,
              background: `${PROVIDER_COLOR[model.provider]}18`, color: PROVIDER_COLOR[model.provider],
            }}>{PROVIDER_LABEL[model.provider]}</span>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={handleReset} style={btn('#F2F2F7', '#333', 11)}>重置</button>
            <button onClick={onClose} style={btn('#F2F2F7', '#333', 11)}>关闭</button>
          </div>
        </div>

        {/* 对话区域 */}
        <div style={{
          flex: 1, overflow: 'auto', padding: '12px 16px',
          minHeight: 300, maxHeight: 'calc(80vh - 120px)',
          background: '#F5F5F7',
        }}>
          {messages.length === 0 && !chatLoading && (
            <div style={{ textAlign: 'center', color: '#AEAEB2', fontSize: 13, padding: 60 }}>
              <div>和 {model.name} 开始对话</div>
              <div style={{ fontSize: 11, marginTop: 4 }}>输入任何问题来体验这个模型的能力</div>
            </div>
          )}
          {messages.map((msg, idx) => (
            <div key={idx} style={{
              display: 'flex', justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
              marginBottom: 8,
            }}>
              <div style={{
                maxWidth: '80%', padding: '8px 12px', borderRadius: 12, fontSize: 13, lineHeight: 1.5,
                background: msg.role === 'user' ? '#007AFF' : 'white',
                color: msg.role === 'user' ? 'white' : '#333',
                border: msg.role === 'user' ? 'none' : '1px solid rgba(0,0,0,0.07)',
              }}>{msg.content}</div>
            </div>
          ))}
          {chatLoading && messages[messages.length - 1]?.role === 'user' && (
            <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
              <div style={{
                padding: '8px 12px', borderRadius: 12, fontSize: 13,
                background: 'white', border: '1px solid rgba(0,0,0,0.07)', color: '#AEAEB2',
              }}>思考中...</div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* 错误提示 */}
        {chatError && (
          <div style={{ padding: '4px 16px', fontSize: 12, color: '#FF3B30', background: '#FF3B3010' }}>
            ⚠️ {chatError}
          </div>
        )}

        {/* 输入区域 */}
        <div style={{
          padding: '8px 16px', borderTop: '1px solid rgba(0,0,0,0.07)',
          display: 'flex', gap: 8,
        }}>
          <input
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onCompositionStart={chatInputImeGuard.onCompositionStart}
            onCompositionEnd={chatInputImeGuard.onCompositionEnd}
            onBlur={chatInputImeGuard.onBlur}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !chatInputImeGuard.isImeEvent(e)) {
                e.preventDefault()
                void handleSend()
              }
            }}
            placeholder="输入消息..."
            disabled={chatLoading}
            style={{
              flex: 1, padding: '8px 12px', borderRadius: 8, fontSize: 13,
              border: '1px solid rgba(0,0,0,0.15)', outline: 'none',
            }}
          />
          <button
            onClick={handleSend}
            disabled={!inputValue.trim() || chatLoading}
            style={btn(chatLoading ? '#AEAEB2' : '#007AFF', 'white', 13)}
          >
            {chatLoading ? '...' : '发送'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── 模型卡片 ──────────────────────────────────────────────────────────────────
const ModelCard: React.FC<{
  model: ModelEntry
  onDownload: () => void
  onActivate: () => void
  onDelete: () => void
  onConfigure: () => void
  onUpgrade?: () => void
  onChat?: () => void
  downloading: boolean
  activating?: boolean
}> = ({ model, onDownload, onActivate, onDelete, onConfigure, onUpgrade, onChat, downloading, activating }) => {
  const isApi = model.requires_api_key
  const isInferenceEngine = model.category === 'inference_engine'
  const isActive = model.status === 'active'
  const isInstalled = model.status === 'installed' || isActive
  const isDownloading = model.status === 'downloading' || downloading
  const isLoading = model.status === 'loading' || activating

  return (
    <div style={{
      background: 'white', borderRadius: 12, padding: '12px 14px',
      border: `1px solid ${isActive ? 'rgba(0,122,255,0.3)' : 'rgba(0,0,0,0.07)'}`,
      marginBottom: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{model.name}</span>
            {model.size_gb > 0 && (
              <span style={{ fontSize: 11, color: '#AEAEB2' }}>{model.size_gb}GB</span>
            )}
            {(model as any).version && (
              <span style={{ fontSize: 11, color: '#AEAEB2' }}>v{(model as any).version}</span>
            )}
            <span style={{
              fontSize: 10, padding: '1px 6px', borderRadius: 4,
              background: `${PROVIDER_COLOR[model.provider]}18`,
              color: PROVIDER_COLOR[model.provider],
            }}>{PROVIDER_LABEL[model.provider] || '本地能力'}</span>
            {model.recommended && (
              <span style={{
                fontSize: 10, padding: '1px 6px', borderRadius: 4,
                background: '#34C75918', color: '#34C759', fontWeight: 600,
              }}>推荐</span>
            )}
          </div>
          <div style={{ fontSize: 12, color: '#6E6E73', marginTop: 3 }}>{model.description}</div>
          {model.tags && model.tags.length > 0 && (
            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 5 }}>
              {model.tags.slice(0, 4).map(t => (
                <span key={t} style={{
                  fontSize: 10, padding: '1px 6px', borderRadius: 4,
                  background: 'rgba(0,0,0,0.05)', color: '#6E6E73',
                }}>{t}</span>
              ))}
            </div>
          )}
          {isDownloading && (
            <div style={{ marginTop: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#6E6E73', marginBottom: 3 }}>
                <span>下载中...</span>
                <span>{model.download_progress || 0}%</span>
              </div>
              <div style={{ height: 4, borderRadius: 2, background: '#E5E5EA' }}>
                <div style={{
                  height: '100%', borderRadius: 2, background: '#FF9500',
                  width: `${model.download_progress || 0}%`, transition: 'width 0.3s',
                }} />
              </div>
            </div>
          )}
          {model.error && (
            <div style={{ marginTop: 8, fontSize: 11, color: '#FF3B30', background: '#FF3B3010', padding: '4px 8px', borderRadius: 4 }}>
              {toUserFacingError(model.error, '模型暂时不可用，请重试')}
            </div>
          )}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5, flexShrink: 0, alignItems: 'flex-end' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: STATUS_COLOR[model.status] || '#AEAEB2' }} />
            <span style={{ fontSize: 11, color: STATUS_COLOR[model.status] || '#AEAEB2' }}>
              {STATUS_LABEL[model.status] || model.status}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 5 }}>
            {isApi && (
              <button onClick={onConfigure} style={btn('#F2F2F7', '#333', 11)}>
                {isInstalled ? '重新配置' : '配置云端能力'}
              </button>
            )}
            {isInferenceEngine && isInstalled && onUpgrade && (model as any).can_upgrade && (
              <button onClick={onUpgrade} style={btn('#007AFF', 'white', 11)}>更新</button>
            )}
            {!isApi && !isInstalled && !isDownloading && !isLoading && !isInferenceEngine && (
              <button onClick={onDownload} style={btn('#007AFF', 'white', 11)}>下载</button>
            )}
            {isInstalled && !isActive && !isLoading && !isInferenceEngine && (
              <button onClick={onActivate} style={btn('#34C759', 'white', 11)}>启用</button>
            )}
            {/* 体验入口：LLM 模型已可用时显示 */}
            {(isActive || (isApi && isInstalled)) && !isInferenceEngine && onChat && (
              <button onClick={onChat} style={btn('#AF52DE18', '#AF52DE', 11)}>体验</button>
            )}
            {isLoading && (
              <span style={{ fontSize: 11, color: '#FF9500', fontWeight: 600 }}>加载中</span>
            )}
            {isInstalled && !isActive && !isLoading && !isInferenceEngine && (
              <button onClick={onDelete} style={btn('#FF3B3018', '#FF3B30', 11)}>删除</button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── 主组件 ────────────────────────────────────────────────────────────────────
type TabType = 'llm' | 'creation'

const ModelManager: React.FC = () => {
  const [tab, setTab] = useState<TabType>('llm')
  const [models, setModels] = useState<ModelEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [modelLoadError, setModelLoadError] = useState('')
  const [configuringModel, setConfiguringModel] = useState<ModelEntry | null>(null)
  const [chattingModel, setChattingModel] = useState<ModelEntry | null>(null)
  const { confirm: confirmDestructive, dialog: confirmDialog } = useConfirmDialog()
  const [downloadingIds, setDownloadingIds] = useState<Set<string>>(new Set())
  const [activatingIds, setActivatingIds] = useState<Set<string>>(new Set())
  const [ollamaSetup, setOllamaSetup] = useState<OllamaSetupDetail | null>(null)
  const [ollamaChecking, setOllamaChecking] = useState(false)
  const [ollamaInstalling, setOllamaInstalling] = useState(false)
  const [ollamaUpgrading, setOllamaUpgrading] = useState(false)
  const [ollamaError, setOllamaError] = useState('')
  const [configuringCreationId, setConfiguringCreationId] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const { apiBaseUrl, adminApiBaseUrl, authToken, currentUser, cloudBalance, setCloudBalance, creationModelConfigs, setCreationModelConfig } = useAppStore(s => ({
    apiBaseUrl: s.apiBaseUrl,
    adminApiBaseUrl: s.adminApiBaseUrl,
    authToken: s.authToken,
    currentUser: s.currentUser,
    cloudBalance: s.cloudBalance,
    setCloudBalance: s.setCloudBalance,
    creationModelConfigs: s.creationModelConfigs,
    setCreationModelConfig: s.setCreationModelConfig,
  }))
  const remoteModelAllowed = canUseRemoteCreationModel(currentUser, cloudBalance)

  const persistCreationModelConfigs = async () => {
    const configs = useAppStore.getState().creationModelConfigs
    await fetch(`${apiBaseUrl}/preferences/${encodeURIComponent(CREATION_MODEL_PREFERENCE_KEY)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: JSON.stringify(configs) }),
    })
  }

  const handleCreationModelChange = (id: string, patch: Partial<CreationModelConfig>) => {
    if (id === REMOTE_CREATION_MODEL_ID && patch.enabled && !remoteModelAllowed) return
    setCreationModelConfig(id, patch)
    void persistCreationModelConfigs()
  }

  const loadModels = async () => {
    setLoading(true)
    setModelLoadError('')
    let lastError: unknown = new Error('模型列表读取失败')
    for (const delayMs of MODEL_LOAD_RETRY_DELAYS_MS) {
      if (delayMs > 0) await wait(delayMs)
      try {
        const r = await fetch(`${SIDECAR}/api/models`)
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const d = await r.json()
        if (d.status !== 'ok') throw new Error(d.message || '模型列表读取失败')
        setModels(normalizeVisibleModels(d.models || []))
        setModelLoadError('')
        setLoading(false)
        return
      } catch (error) {
        lastError = error
      }
    }
    setModelLoadError(toUserFacingError(lastError, 'AI 能力暂时无法读取，请稍后重试'))
    setLoading(false)
  }

  const refreshOllamaSetup = async () => {
    setOllamaChecking(true)
    setOllamaError('')
    try {
      const r = await fetch(`${SIDECAR}/api/ollama/setup-status`)
      const d = await r.json()
      if (d.status === 'ok') setOllamaSetup(d.detail || null)
      else setOllamaSetup(null)
    } catch {
      setOllamaSetup(null)
      setOllamaError('本地运行环境状态暂时无法读取')
    } finally {
      setOllamaChecking(false)
    }
  }

  const handleInstallOllama = async () => {
    if (ollamaSetup && !ollamaSetup.ollama_installed && !ollamaSetup.can_auto_install) {
      setOllamaError('请重新打开应用，MemoryBread 会自动恢复本地 AI 能力')
      return
    }

    setOllamaInstalling(true)
    setOllamaError('')
    try {
      const installResp = await fetch(`${SIDECAR}/api/ollama/install`, { method: 'POST' })
      const installData = await installResp.json()
      if (installData.status !== 'ok') {
        setOllamaError(toUserFacingError(installData.message, '本地运行环境安装失败，请稍后重试'))
        await refreshOllamaSetup()
        return
      }

      const startResp = await fetch(`${SIDECAR}/api/ollama/start`, { method: 'POST' })
      const startData = await startResp.json()
      if (startData.status !== 'ok') {
        setOllamaError(toUserFacingError(startData.message, '本地运行环境启动失败，请重新打开应用'))
      }

      await refreshOllamaSetup()
      await loadModels()
    } catch (error) {
      setOllamaError(toUserFacingError(error, '本地 AI 服务暂时不可用，请重新打开应用'))
    } finally {
      setOllamaInstalling(false)
    }
  }

  useEffect(() => {
    loadModels()
    refreshOllamaSetup()
  }, [])

  useEffect(() => {
    if (!authToken || !currentUser) {
      setCloudBalance(null)
      return
    }
    let cancelled = false
    fetchBillingBalance(adminApiBaseUrl, authToken)
      .then(balance => {
        if (!cancelled) setCloudBalance(balance)
      })
      .catch(() => {
        if (!cancelled) setCloudBalance(null)
      })
    return () => { cancelled = true }
  }, [adminApiBaseUrl, authToken, currentUser, setCloudBalance])

  // 轮询下载进度
  useEffect(() => {
    if (downloadingIds.size === 0) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
      return
    }
    pollRef.current = setInterval(async () => {
      const updates: Record<string, Partial<ModelEntry>> = {}
      let anyDone = false
      for (const id of downloadingIds) {
        try {
          const r = await fetch(`${SIDECAR}/api/models/${id}/status`)
          const d = await r.json()
          updates[id] = { status: d.status, download_progress: d.download_progress, error: d.error }
          if (d.status === 'installed' || d.status === 'active' || d.status === 'error' || d.error) {
            anyDone = true
            if (d.status === 'error' || d.error) {
              setOllamaError(toUserFacingError(d.error || d.message, '下载失败，请稍后重试'))
            }
          }
        } catch { }
      }
      setModels(prev => prev.map(m => updates[m.id] ? { ...m, ...updates[m.id] } : m))
      if (anyDone) {
        setDownloadingIds(prev => {
          const next = new Set(prev)
          for (const [id, u] of Object.entries(updates)) {
            if (u.status === 'installed' || u.status === 'active' || u.status === 'error' || u.error) next.delete(id)
          }
          return next
        })
      }
    }, 3000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [downloadingIds])

  const handleDownload = async (model: ModelEntry) => {
    if (model.provider === 'ollama' && !ollamaSetup?.ollama_running) {
      setOllamaError('请先安装本地运行环境，再下载分析模型')
      return
    }

    try {
      const response = await fetch(`${SIDECAR}/api/models/${model.id}/download`, { method: 'POST' })
      const data = await response.json()
      if (!response.ok || data.status !== 'ok') {
        throw new Error(data.message || `HTTP ${response.status}`)
      }
      setDownloadingIds(prev => new Set(prev).add(model.id))
      setModels(prev => prev.map(m => m.id === model.id ? { ...m, status: 'downloading', download_progress: 0 } : m))
    } catch (error) {
      setOllamaError(toUserFacingError(error, '下载请求失败，请稍后重试'))
    }
  }

  const handleActivate = async (model: ModelEntry) => {
    setActivatingIds(prev => new Set(prev).add(model.id))
    try {
      await fetch(`${SIDECAR}/api/models/${model.id}/activate`, { method: 'POST' })
      await loadModels()
    } catch { }
    setActivatingIds(prev => {
      const next = new Set(prev)
      next.delete(model.id)
      return next
    })
  }

  const handleDelete = async (model: ModelEntry) => {
    if (!(await confirmDestructive({ title: `删除“${model.name}”？`, description: '删除后需要重新下载才能继续使用。' }))) return
    try {
      await fetch(`${SIDECAR}/api/models/${model.id}/delete`, { method: 'DELETE' })
      await loadModels()
    } catch { }
  }

  const handleUpgrade = async () => {
    setOllamaUpgrading(true)
    setOllamaError('')
    try {
      const res = await fetch(`${SIDECAR}/api/ollama/upgrade`, { method: 'POST' })
      const data = await res.json()
      if (data.status === 'upgrading') {
        // 启动轮询
        pollUpgradeStatus()
      } else if (data.status === 'error') {
        setOllamaError(toUserFacingError(data.message, '更新失败，请稍后重试'))
        setOllamaUpgrading(false)
      }
    } catch (e) {
      setOllamaError(toUserFacingError(e, '更新失败，请稍后重试'))
      setOllamaUpgrading(false)
    }
  }

  const pollUpgradeStatus = async () => {
    const poll = async () => {
      try {
        const res = await fetch(`${SIDECAR}/api/ollama/upgrade/status`)
        const data = await res.json()

        if (data.status === 'upgrading') {
          setOllamaError('正在更新本地运行环境...')
          setTimeout(poll, 2000)
        } else if (data.status === 'success') {
          setOllamaError('')
          setOllamaUpgrading(false)
          await refreshOllamaSetup()
          await loadModels()
        } else if (data.status === 'error') {
          setOllamaError(toUserFacingError(data.message, '更新失败，请稍后重试'))
          setOllamaUpgrading(false)
        } else {
          setTimeout(poll, 2000)
        }
      } catch {
        setOllamaError('更新状态暂时无法读取，请稍后重试')
        setOllamaUpgrading(false)
      }
    }
    poll()
  }

  // 按 tab 过滤
  const filtered = models.filter(m => {
    if (tab === 'llm') return ['llm', 'embedding', 'inference_engine'].includes(m.category)
    if (tab === 'creation') return m.category === 'image'
    return false
  })

  // 按产品分类分组，避免暴露底层模型供应商作为主导航。
  const displayGroups = filtered.reduce<Record<string, ModelEntry[]>>((acc, m) => {
    const key = m.category
    if (!acc[key]) acc[key] = []
    acc[key].push(m)
    return acc
  }, {})

  // 当前激活模型
  const activeLlm = models.find(m => (m.status === 'active' || m.is_active) && m.category === 'llm')
  const activeEmb = models.find(m => (m.status === 'active' || m.is_active) && m.category === 'embedding')

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', background: '#F5F5F7' }}>

      {/* 顶部激活状态 */}
      <div style={{ padding: '10px 14px 0', display: 'flex', gap: 8 }}>
        {[
          { label: '采集分析模型', model: activeLlm },
          { label: '向量模型', model: activeEmb },
        ].map(({ label, model }) => (
          <div key={label} style={{
            flex: 1, background: 'white', borderRadius: 10, padding: '8px 12px',
            border: '1px solid rgba(0,0,0,0.07)',
          }}>
            <div style={{ fontSize: 10, color: '#AEAEB2', marginBottom: 2 }}>{label}</div>
            {model ? (
              <div style={{ fontSize: 12, fontWeight: 600, color: '#007AFF' }}>{model.name}</div>
            ) : (
              <div style={{ fontSize: 12, color: '#FF9500' }}>未配置</div>
            )}
          </div>
        ))}
      </div>

      {/* Tab 切换 */}
      <div style={{ display: 'flex', gap: 4, padding: '10px 14px 0' }}>
        {([
          { key: 'llm', label: '采集分析模型' },
          { key: 'creation', label: '创作模型' },
        ] as { key: TabType; label: string }[]).map(t => (
          <button key={t.key} onClick={() => setTab(t.key)} style={{
            fontSize: 12, padding: '5px 12px', borderRadius: 8, border: 'none', cursor: 'pointer',
            background: tab === t.key ? '#007AFF' : 'white',
            color: tab === t.key ? 'white' : '#6E6E73',
            fontWeight: tab === t.key ? 600 : 400,
          }}>{t.label}</button>
        ))}
        <button onClick={loadModels} style={{
          marginLeft: 'auto', fontSize: 11, padding: '5px 10px', borderRadius: 8,
          border: '1px solid rgba(0,0,0,0.1)', background: 'white', color: '#6E6E73', cursor: 'pointer',
        }}>刷新</button>
      </div>

      {/* 内容区 */}
      <div style={{ flex: 1, overflow: 'auto', padding: '10px 14px 14px' }}>
        {tab === 'creation' ? (
          <>
            <CreationModelPanel
              configs={creationModelConfigs}
              remoteAllowed={remoteModelAllowed}
              availableCredit={cloudBalance?.available ?? null}
              openId={configuringCreationId}
              onToggleOpen={setConfiguringCreationId}
              onChange={handleCreationModelChange}
            />
            {Object.entries(displayGroups).map(([category, items]) => (
              <ModelSection
                key={category}
                title={CATEGORY_LABEL[category] || category}
                models={items}
                downloadingIds={downloadingIds}
                activatingIds={activatingIds}
                onDownload={handleDownload}
                onActivate={handleActivate}
                onDelete={handleDelete}
                onConfigure={setConfiguringModel}
                onChat={setChattingModel}
              />
            ))}
          </>
        ) : (
          <>
        {loading && models.length === 0 && (
          <div style={{ textAlign: 'center', color: '#AEAEB2', fontSize: 13, padding: 40 }}>加载中...</div>
        )}

        {modelLoadError && (
          <div role="alert" style={{ color: '#C52828', fontSize: 12, padding: '12px 14px', marginBottom: 10, borderRadius: 10, background: 'rgba(255,59,48,0.08)' }}>
            {modelLoadError}
          </div>
        )}

        {Object.entries(displayGroups).map(([category, items]) => (
          <div key={category} style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#007AFF' }} />
              <span style={{ fontSize: 12, fontWeight: 600, color: '#333' }}>
                {CATEGORY_LABEL[category] || category}
              </span>
            </div>

            {/* 本地运行环境信息 */}
            {category === 'inference_engine' && tab === 'llm' && (
              <div style={{ background: 'white', borderRadius: 10, padding: 12, border: '1px solid rgba(0,0,0,0.07)', marginBottom: 8 }}>
                {ollamaChecking ? (
                  <div style={{ fontSize: 12, color: '#AEAEB2' }}>检测中...</div>
                ) : (
                  <>
                    <div style={{ fontSize: 12, color: ollamaSetup?.ollama_running ? '#34C759' : '#6E6E73', marginBottom: 4 }}>
                      {ollamaSetup?.ollama_running
                        ? '本地运行环境正常'
                        : ollamaSetup?.version_compatible === false
                          ? `当前系统版本不兼容，需要 macOS ${ollamaSetup.minimum_macos_major || 14} 或更高版本`
                          : '本地运行环境尚未就绪'}
                    </div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button onClick={refreshOllamaSetup} style={btn('#F2F2F7', '#333', 11)}>重新检测</button>
                      {ollamaSetup?.ollama_installed && ollamaSetup?.brew_available && (
                        <button onClick={handleUpgrade} disabled={ollamaUpgrading} style={btn('#007AFF', 'white', 11)}>
                          {ollamaUpgrading ? '更新中...' : '更新本地运行环境'}
                        </button>
                      )}
                      {!ollamaSetup?.ollama_running && ollamaSetup?.version_compatible !== false && (
                        <button
                          onClick={handleInstallOllama}
                          disabled={ollamaInstalling}
                          style={btn('#007AFF', 'white', 11)}
                        >
                          {ollamaInstalling
                            ? '安装中...'
                            : ollamaSetup && !ollamaSetup.ollama_installed && !ollamaSetup.can_auto_install
                              ? '打开官方下载页'
                              : ollamaSetup?.ollama_installed
                                ? '启动本地运行环境'
                                : '自动安装并启动'}
                        </button>
                      )}
                    </div>
                    {ollamaError && <div style={{ fontSize: 11, color: '#FF3B30', marginTop: 6 }}>{ollamaError}</div>}
                  </>
                )}
              </div>
            )}

            {items.filter(m => m.category !== 'inference_engine').map(m => (
              <ModelCard
                key={m.id} model={m}
                downloading={downloadingIds.has(m.id)}
                activating={activatingIds.has(m.id)}
                onDownload={() => handleDownload(m)}
                onActivate={() => handleActivate(m)}
                onDelete={() => handleDelete(m)}
                onConfigure={() => setConfiguringModel(m)}
                onUpgrade={m.category === 'inference_engine' ? handleUpgrade : undefined}
                onChat={m.category === 'llm' && (m.status === 'active' || (m.requires_api_key && (m.status as string === 'installed' || m.status as string === 'active'))) ? () => setChattingModel(m) : undefined}
              />
            ))}
          </div>
        ))}

        {!loading && !modelLoadError && filtered.length === 0 && (
          <div style={{ textAlign: 'center', color: '#AEAEB2', fontSize: 13, padding: 40 }}>
            暂无模型
          </div>
        )}
          </>
        )}
      </div>

      {configuringModel && (
        <ApiKeyDialog
          model={configuringModel}
          onClose={() => setConfiguringModel(null)}
          onSaved={loadModels}
        />
      )}
      {chattingModel && (
        <ModelChatDialog
          model={chattingModel}
          onClose={() => setChattingModel(null)}
        />
      )}
      {confirmDialog}
    </div>
  )
}

const ModelSection: React.FC<{
  title: string
  models: ModelEntry[]
  downloadingIds: Set<string>
  activatingIds: Set<string>
  onDownload: (model: ModelEntry) => void
  onActivate: (model: ModelEntry) => void
  onDelete: (model: ModelEntry) => void
  onConfigure: (model: ModelEntry) => void
  onChat: (model: ModelEntry) => void
}> = ({ title, models, downloadingIds, activatingIds, onDownload, onActivate, onDelete, onConfigure, onChat }) => (
  <div style={{ marginTop: 14, marginBottom: 16 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#007AFF' }} />
      <span style={{ fontSize: 12, fontWeight: 600, color: '#333' }}>{title}</span>
    </div>
    {models.map(m => (
      <ModelCard
        key={m.id}
        model={m}
        downloading={downloadingIds.has(m.id)}
        activating={activatingIds.has(m.id)}
        onDownload={() => onDownload(m)}
        onActivate={() => onActivate(m)}
        onDelete={() => onDelete(m)}
        onConfigure={() => onConfigure(m)}
        onChat={m.category === 'llm' && (m.status === 'active' || (m.requires_api_key && (m.status as string === 'installed' || m.status as string === 'active'))) ? () => onChat(m) : undefined}
      />
    ))}
  </div>
)

const CREATION_MODEL_DEFS = [
  {
    id: 'mbcd-plus-v1',
    name: 'MBCD Plus v1.0',
    description: 'MemoryBread Create Document Plus 1.0，适合更长文本和更高质量的云端创作',
    provider: 'gateway',
    hasBaseUrl: false,
    sizeGb: 0,
  },
  {
    id: 'mbcd-std-v1',
    name: 'MBEM v1.0',
    description: 'MemoryBread Extract Model 1.0，用于本地理解、提炼、创作与咨询',
    provider: 'ollama',
    hasBaseUrl: true,
    sizeGb: 3.4,
  },
] as const

const CREATION_SVC = 'http://127.0.0.1:8001'
const LOCAL_CREATION_MODEL_ID = 'mbcd-std-v1'

type CreationChatEntry = { def: typeof CREATION_MODEL_DEFS[number]; cfg: { id: string; apiKey: string; baseUrl?: string } }

const CreationModelChatDialog: React.FC<{ entry: CreationChatEntry; onClose: () => void }> = ({ entry, onClose }) => {
  const { def } = entry
  const gatewayApiBaseUrl = useAppStore(s => s.gatewayApiBaseUrl)
  const currentUser = useAppStore(s => s.currentUser)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [chatError, setChatError] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const chatInputImeGuard = useImeCompositionGuard<HTMLInputElement>()
  useEffect(() => { endRef.current?.scrollIntoView?.({ behavior: 'smooth' }) }, [messages])

  const handleSend = async () => {
    const q = input.trim()
    if (!q || loading) return
    setInput('')
    setChatError('')
    const userMsg: ChatMessage = { role: 'user', content: q }
    setMessages(prev => [...prev, userMsg])
    setLoading(true)
    try {
      const allMsgs = [...messages, userMsg].map(m => ({ role: m.role, content: m.content }))
      const isGatewayModel = def.provider === 'gateway'
      const resp = await fetch(isGatewayModel
        ? `${gatewayApiBaseUrl.replace(/\/+$/, '')}/v1/gateway/chat`
        : `${SIDECAR}/api/models/mbem-v1-local/chat`, {
        method: 'POST',
        headers: {
          ...(isGatewayModel ? serviceEnvironmentHeaders() : {}),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(isGatewayModel ? {
          request_id: `creation-experience-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          user_id: currentUser?.id || null,
          brand_model_id: def.id,
          caller: 'creation',
          messages: allMsgs,
          stream: false,
          privacy: { content_logging: false, client_scrubbed: true },
          limits: { max_output_tokens: 2048, max_credit: '30.0000' },
        } : { messages: allMsgs }),
      })
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({ message: `HTTP ${resp.status}` }))
        throw new Error(d.message || d.detail || `HTTP ${resp.status}`)
      }
      if (isGatewayModel) {
        const data = await resp.json()
        const content = String(data.content || '').trim()
        if (!content) throw new Error('模型没有返回内容')
        setMessages(prev => [...prev, { role: 'assistant', content }])
        return
      }
      const reader = resp.body?.getReader()
      if (!reader) { setChatError('无法读取响应流'); setLoading(false); return }
      const decoder = new TextDecoder()
      let content = ''
      setMessages(prev => [...prev, { role: 'assistant', content: '' }])
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        for (const line of decoder.decode(value, { stream: true }).split('\n')) {
          if (!line.startsWith('data: ')) continue
          try {
            const evt = JSON.parse(line.slice(6))
            if (evt.content) { content += evt.content; setMessages(prev => { const u = [...prev]; u[u.length - 1] = { role: 'assistant', content }; return u }) }
            if (evt.error) setChatError(toUserFacingError(evt.error, '模型响应失败，请稍后重试'))
          } catch { /* ignore */ }
        }
      }
    } catch (e: unknown) {
      setChatError(toUserFacingError(e, '连接失败，请稍后重试'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
      <div style={{ background: 'white', borderRadius: 16, width: 520, maxHeight: '80vh', boxShadow: '0 20px 60px rgba(0,0,0,0.2)', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '12px 16px', borderBottom: '1px solid rgba(0,0,0,0.07)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 15, fontWeight: 700 }}>体验 {def.name}</span>
          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={() => { setMessages([]); setChatError('') }} style={btn('#F2F2F7', '#333', 11)}>重置</button>
            <button onClick={onClose} style={btn('#F2F2F7', '#333', 11)}>关闭</button>
          </div>
        </div>
        <div style={{ flex: 1, overflow: 'auto', padding: '12px 16px', minHeight: 300, maxHeight: 'calc(80vh - 120px)', background: '#F5F5F7' }}>
          {messages.length === 0 && !loading && (
            <div style={{ textAlign: 'center', color: '#AEAEB2', fontSize: 13, padding: 60 }}>
              <div style={{ fontSize: 40, marginBottom: 8 }}>💬</div>
              <div>和 {def.name} 开始对话</div>
            </div>
          )}
          {messages.map((msg, idx) => (
            <div key={idx} style={{ display: 'flex', justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start', marginBottom: 8 }}>
              <div style={{ maxWidth: '75%', padding: '8px 12px', borderRadius: msg.role === 'user' ? '12px 12px 4px 12px' : '12px 12px 12px 4px', background: msg.role === 'user' ? '#007AFF' : 'white', color: msg.role === 'user' ? 'white' : '#1D1D1F', fontSize: 13, whiteSpace: 'pre-wrap', boxShadow: '0 1px 3px rgba(0,0,0,0.1)' }}>
                {msg.content || (msg.role === 'assistant' && loading ? '▌' : '')}
              </div>
            </div>
          ))}
          {chatError && <div style={{ fontSize: 11, color: '#FF3B30', textAlign: 'center', padding: 8 }}>{chatError}</div>}
          <div ref={endRef} />
        </div>
        <div style={{ padding: '10px 16px', borderTop: '1px solid rgba(0,0,0,0.07)', display: 'flex', gap: 8 }}>
          <input value={input} onChange={e => setInput(e.target.value)}
            onCompositionStart={chatInputImeGuard.onCompositionStart}
            onCompositionEnd={chatInputImeGuard.onCompositionEnd}
            onBlur={chatInputImeGuard.onBlur}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !chatInputImeGuard.isImeEvent(e)) {
                e.preventDefault()
                void handleSend()
              }
            }}
            placeholder="输入消息…"
            style={{ flex: 1, padding: '8px 10px', border: '1px solid #E5E5EA', borderRadius: 8, fontSize: 13, outline: 'none' }} />
          <button onClick={handleSend} disabled={loading || !input.trim()} style={btn(loading || !input.trim() ? '#E5E5EA' : '#007AFF', loading || !input.trim() ? '#AEAEB2' : 'white', 12)}>
            {loading ? '…' : '发送'}
          </button>
        </div>
      </div>
    </div>
  )
}

const CreationModelPanel: React.FC<{
  configs: import('../store/useAppStore').CreationModelConfig[]
  remoteAllowed: boolean
  availableCredit: string | null
  openId: string | null
  onToggleOpen: (id: string | null) => void
  onChange: (id: string, patch: Partial<import('../store/useAppStore').CreationModelConfig>) => void
}> = ({ configs, remoteAllowed, availableCredit, openId, onToggleOpen, onChange }) => {
  const [testState, setTestState] = React.useState<Record<string, { loading: boolean; result?: string; error?: string }>>({})
  const [chattingModel, setChattingModel] = React.useState<CreationChatEntry | null>(null)
  const activeConfig = configs.find(config => config.enabled && (remoteAllowed || config.id !== REMOTE_CREATION_MODEL_ID))
  const activeDef = CREATION_MODEL_DEFS.find(def => def.id === activeConfig?.id)
  const activeModelName = activeDef?.name || 'MBEM v1.0'

  const handleTest = async (def: typeof CREATION_MODEL_DEFS[number], cfg: { id: string; enabled: boolean; apiKey: string; baseUrl?: string }) => {
    if (!cfg.apiKey) return
    setTestState(s => ({ ...s, [def.id]: { loading: true } }))
    try {
      const r = await fetch(`${CREATION_SVC}/creation/test_model`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: def.id,
          api_key: cfg.apiKey,
          base_url: cfg.baseUrl || undefined,
        }),
      })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || '请求失败')
      setTestState(s => ({ ...s, [def.id]: { loading: false, result: data.message } }))
    } catch (e: unknown) {
      setTestState(s => ({ ...s, [def.id]: { loading: false, error: toUserFacingError(e, '验证失败，请稍后重试') } }))
    }
  }

  return (
    <div>
      <div style={{ fontSize: 12, color: '#1D1D1F', marginBottom: 8, fontWeight: 650 }}>
        当前创作模型：{activeModelName}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#AF52DE' }} />
        <span style={{ fontSize: 12, fontWeight: 600, color: '#333' }}>咨询生成模型</span>
      </div>
      <div style={{ fontSize: 11, color: '#8E8E93', marginBottom: 10, lineHeight: 1.5 }}>
        只能启用一个咨询生成模型。未登录或未开启云端创作时，默认使用本地 MBEM v1.0。
        {availableCredit != null ? ` 当前可用 Credit：${availableCredit}` : ''}
      </div>
      {CREATION_MODEL_DEFS.map(def => {
        const cfg = configs.find(c => c.id === def.id) || { id: def.id, enabled: false, apiKey: '' }
        const isOpen = openId === def.id
        const isLocalModel = def.id === LOCAL_CREATION_MODEL_ID
        const isGatewayModel = def.provider === 'gateway'
        const disabled = isGatewayModel && !remoteAllowed
        const ts = testState[def.id]
        return (
          <div key={def.id} style={{ background: 'white', borderRadius: 10, padding: '10px 12px', border: '1px solid rgba(0,0,0,0.07)', marginBottom: 8, opacity: disabled ? 0.58 : 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: PROVIDER_COLOR[def.provider] || '#AEAEB2', flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: '#1D1D1F' }}>{def.name}</span>
                  {def.sizeGb > 0 && (
                    <span style={{ fontSize: 11, color: '#AEAEB2' }}>{def.sizeGb}GB</span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: '#8E8E93', marginTop: 2 }}>{def.description}</div>
              </div>
              {!isLocalModel && !isGatewayModel && cfg.apiKey && (
                <button onClick={() => handleTest(def, cfg)} disabled={ts?.loading} style={btn(ts?.result ? '#34C759' : ts?.error ? '#FF3B30' : '#F2F2F7', ts?.result || ts?.error ? 'white' : '#333', 11)}>
                  {ts?.loading ? '验证中…' : ts?.result ? '已通' : ts?.error ? '失败' : '验证'}
                </button>
              )}
              {!disabled && (
                <button onClick={() => setChattingModel({ def, cfg })} style={btn('#AF52DE18', '#AF52DE', 11)}>体验</button>
              )}
              <label style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: disabled ? 'not-allowed' : 'pointer' }}>
                <input type="checkbox" checked={cfg.enabled && !disabled} disabled={disabled} onChange={() => onChange(def.id, { enabled: !cfg.enabled })} />
                <span style={{ fontSize: 11, color: cfg.enabled && !disabled ? '#007AFF' : '#AEAEB2' }}>启用</span>
              </label>
            </div>
            {ts?.error && <div style={{ fontSize: 11, color: '#FF3B30', marginTop: 6 }}>{ts.error}</div>}
            {ts?.result && <div style={{ fontSize: 11, color: '#34C759', marginTop: 6 }}>回复：{ts.result}</div>}
            {isGatewayModel && (
              <div style={{ marginTop: 8, fontSize: 11, color: '#8E8E93', lineHeight: 1.5 }}>
                {disabled ? '登录且有可用 Credit 后可启用云端创作。' : '云端创作会使用账户 Credit，本地记忆和私有快照内容仍留在你的设备上。'}
              </div>
            )}
          </div>
        )
      })}
      {chattingModel && <CreationModelChatDialog entry={chattingModel} onClose={() => setChattingModel(null)} />}
    </div>
  )
}

function btn(bg: string, color: string, fontSize = 13): React.CSSProperties {
  return {
    background: bg, color, fontSize, fontWeight: 500,
    padding: fontSize <= 11 ? '4px 10px' : '7px 16px',
    borderRadius: 8, border: 'none', cursor: 'pointer',
  }
}

export default ModelManager
