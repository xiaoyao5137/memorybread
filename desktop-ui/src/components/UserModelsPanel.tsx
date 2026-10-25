import './UserModelsPanel.css'
import gateways from '../../../shared/model-gateways/providers.json'
import { createPortal } from 'react-dom'
import { Cloud, Plug, Terminal } from 'lucide-react'
import ModelSelect from './ModelSelect'
import React, { useEffect, useId, useRef, useState } from 'react'
import { useAppStore, CREATION_MODEL_PREFERENCE_KEY } from '../store/useAppStore'
import { getLocalServiceBaseUrl } from '../utils/localServices'

const service = getLocalServiceBaseUrl('creation')
type Profile = { provider?: string; region?: string; workspace_id?: string; id: string; name: string; protocol: string; model: string; base_url: string; command: string; client: string; args?: string[]; has_api_key?: boolean }
type Template = { id: string; name: string; model: string; protocol: string; base_url: string; capabilities: string[] }
const blank: Profile = { id: '', name: '', protocol: 'openai', model: '', base_url: '', command: '', client: 'codex' }
const validTemplates = (value: unknown): Template[] => Array.isArray(value) ? value.filter((t): t is Template => !!t && typeof t.id === 'string' && typeof t.name === 'string' && typeof t.model === 'string' && ['openai', 'anthropic'].includes(t.protocol) && typeof t.base_url === 'string' && Array.isArray(t.capabilities) && t.capabilities.every((c: unknown) => typeof c === 'string')) : []
const cacheKey = 'memorybread.user-model-catalog.v1:'
export default function UserModelsPanel({ scope = 'creation', onAnalysisChange, selectionHost, actionsHost, refreshVersion = 0 }: { scope?: 'creation' | 'analysis'; onAnalysisChange?: (name: string | null) => void; selectionHost?: HTMLElement | null; actionsHost?: HTMLElement | null; refreshVersion?: number }) {
  const isAnalysis = scope === 'analysis'
  const [analysisId, setAnalysisId] = useState<string | null>(null)
  const configs = useAppStore(s => s.creationModelConfigs)
  const adminUrl = useAppStore(s => s.adminApiBaseUrl)
  const apiUrl = useAppStore(s => s.apiBaseUrl)
  const catalogGeneration = useRef(0)
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [templates, setTemplates] = useState<Template[]>([])
  const [draft, setDraft] = useState<Profile | null>(null)
  const [key, setKey] = useState('')
  const [args, setArgs] = useState('[]')
  const [error, setError] = useState('')
  const [detecting, setDetecting] = useState(false)
  const [detectionMessage, setDetectionMessage] = useState('')
  const argsRef = useRef(args)
  argsRef.current = args
  const [busy, setBusy] = useState(false)
  const [connectionResult, setConnectionResult] = useState('')
  const dialogRef = useRef<HTMLDivElement>(null)
  const dialogTitle = useId()
  const lastRefresh = useRef(refreshVersion)
  const [prompt, setPrompt] = useState('请用一句话介绍你自己。')
  const [reply, setReply] = useState('')
  const [preview, setPreview] = useState<string | null>(null)
  useEffect(() => { setConnectionResult('') }, [draft, key, args])
  useEffect(() => { if ((draft || preview) && busy) dialogRef.current?.focus() }, [busy])
  useEffect(() => {
    setDetectionMessage('')
    if (!draft || draft.protocol !== 'cli' || draft.client === 'custom') { setDetecting(false); return }
    const controller = new AbortController()
    let cancelled = false
    const snapshot = draft
    const initialArgs = argsRef.current
    setDetecting(true)
    async function detect() {
      try {
        const result = await request(`${service}/creation/user-models/detect/${encodeURIComponent(snapshot.client)}`, { signal: controller.signal })
        if (cancelled) return
        setDraft(current => {
          if (!current || current.protocol !== 'cli' || current.client !== snapshot.client) return current
          return { ...current,
            command: current.command === snapshot.command && (!snapshot.id || !snapshot.command) && result.command ? result.command : current.command,
            model: current.model === snapshot.model && (!snapshot.id || !snapshot.model) && result.model ? result.model : current.model,
          }
        })
        if (argsRef.current === initialArgs && (!snapshot.id || initialArgs === '[]') && Array.isArray(result.args)) setArgs(JSON.stringify(result.args))
        setDetectionMessage(result.message || '探测完成，可自行修改配置。')
      } catch {
        if (!cancelled) setDetectionMessage('自动探测失败，可手动填写配置。')
      } finally { if (!cancelled) setDetecting(false) }
    }
    void detect()
    return () => { cancelled = true; controller.abort() }
  }, [Boolean(draft), draft?.protocol, draft?.client])
  async function request(url: string, init?: RequestInit) {
    const response = await fetch(url, init)
    const data = await response.json().catch(() => ({}))
    if (response.status === 404 && url.includes('/creation/user-models') && (!data.detail || data.detail === 'Not Found')) {
      throw new Error('本机模型服务缺少此功能，请更新或重启应用后重试。')
    }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '模型服务请求失败')
    return data
  }
  async function persist() {
    await request(`${apiUrl}/preferences/${encodeURIComponent(CREATION_MODEL_PREFERENCE_KEY)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: JSON.stringify(useAppStore.getState().creationModelConfigs) }),
    })
  }
  async function load() {
    setError('')
    try {
      const data = await request(`${service}/creation/user-models`)
      setProfiles(data.models)
      if (isAnalysis) {
        setAnalysisId(data.analysis_model_id || null)
        onAnalysisChange?.(data.models.find((p: Profile) => p.id === data.analysis_model_id)?.name || null)
        return
      }
      // Reconcile device profiles after reinstall/reload; keys stay in the local sidecar.
      const current = useAppStore.getState().creationModelConfigs
      const retained = current.filter(c => !c.id.startsWith('user-') || data.models.some((p: Profile) => p.id === c.id))
      for (const p of data.models as Profile[]) {
        const i = retained.findIndex(c => c.id === p.id)
        const config = { id: p.id, name: p.name, enabled: false, apiKey: 'device-profile' }
        if (i < 0) retained.push(config)
        else retained[i] = { ...retained[i], name: p.name, apiKey: 'device-profile' }
      }
      useAppStore.getState().setCreationModelConfigs(retained)
      await persist()
    } catch (e) { setError(e instanceof Error ? e.message : '读取自定义模型失败') }
  }
  async function refreshCatalog() {
    const generation = ++catalogGeneration.current
    try {
      const models: Template[] = []
      let offset: number | null = 0
      do {
        const response = await fetch(`${adminUrl.replace(/\/$/, '')}/v1/model-catalog?offset=${offset}`)
        if (response.status === 404) throw new Error('CATALOG_NOT_DEPLOYED')
        if (!response.ok) throw new Error('CATALOG_UNAVAILABLE')
        const data = await response.json()
        if (data.data.schema_version !== 1 || !Array.isArray(data.data.models)) throw new Error('目录版本不受支持')
        models.push(...validTemplates(data.data.models))
        const next: number | null = data.data.next_offset
        if (next != null && (next <= (offset ?? 0) || models.length >= 10000)) throw new Error('目录分页无效')
        offset = next
      } while (offset != null)
      if (generation !== catalogGeneration.current) return
      setTemplates(models)
      localStorage.setItem(cacheKey + adminUrl, JSON.stringify(models))
    } catch {
      // The directory is optional; cached choices and manual configuration remain available.
    }
  }
  useEffect(() => {
    if (lastRefresh.current === refreshVersion) return
    lastRefresh.current = refreshVersion
    void load(); void refreshCatalog()
  }, [refreshVersion])
  useEffect(() => {
    if (!draft && !preview) return
    const previous = document.activeElement as HTMLElement | null
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialogRef.current?.querySelector<HTMLElement>('input, textarea')?.focus()
    return () => { document.body.style.overflow = overflow; previous?.focus() }
  }, [Boolean(draft || preview)])

  useEffect(() => { void load() }, [scope])
  useEffect(() => {
    setTemplates([])
    try { setTemplates(validTemplates(JSON.parse(localStorage.getItem(cacheKey + adminUrl) || '[]'))) } catch { /* empty cache */ }
    void refreshCatalog()
  }, [adminUrl])
  async function mutate(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : '操作失败') }
    finally { setBusy(false) }
  }
  function edit(p: Profile) { setDraft(p); setKey(''); setError(''); setConnectionResult(''); setArgs(JSON.stringify(p.args || [])) }
  function draftBody() { return { ...draft, id: draft?.id || undefined, api_key: key || undefined, args: JSON.parse(args) } }
  async function testConnection() {
    if (!dialogRef.current?.querySelector<HTMLFormElement>('form')?.reportValidity()) return
    setConnectionResult('')
    const result = await request(`${service}/creation/user-models/test-connection`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draftBody()) })
    setConnectionResult(result.message || '连接成功')
  }
  function closeDialog() { setDraft(null); setKey(''); setConnectionResult(''); setError('') }
  async function save() {
    if (!draft) return
    const profile = await request(`${service}/creation/user-models`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draftBody()),
    }) as Profile
    setDraft(profile); setKey('')
    const current = useAppStore.getState().creationModelConfigs
    const existing = current.find(c => c.id === profile.id)
    useAppStore.getState().setCreationModelConfigs([...current.filter(c => c.id !== profile.id), { id: profile.id, name: profile.name, enabled: existing?.enabled || false, apiKey: 'device-profile' }])
    await persist()
    setDraft(null); setKey(''); await load()
  }
  async function select(id: string | null) {
    if (isAnalysis) {
      await request(`${service}/creation/user-models/analysis-selection`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_id: id }),
      })
      setAnalysisId(id)
      onAnalysisChange?.(profiles.find(p => p.id === id)?.name || null)
    } else if (id) {
      useAppStore.getState().setCreationModelConfig(id, { enabled: true }); await persist()
    }
  }
  async function remove(id: string) {
    await request(`${service}/creation/user-models/${encodeURIComponent(id)}`, { method: 'DELETE' })
    useAppStore.getState().setCreationModelConfigs(useAppStore.getState().creationModelConfigs.filter(c => c.id !== id))
    await persist(); if (preview === id) { setPreview(null); setReply('') }; await load()
  }
  function closePreview() { setPreview(null); setReply(''); setError('') }
  function handleDialogKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape' && !busy && !dialogRef.current?.querySelector('[aria-expanded="true"]')) {
      event.stopPropagation()
      if (draft) closeDialog()
      else closePreview()
    }
    if (event.key !== 'Tab') return
    const elements = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]') || [])
    if (!elements.length) { event.preventDefault(); dialogRef.current?.focus(); return }
    const first = elements[0]; const last = elements[elements.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
  async function chat(id: string) {
    setReply('')
    const response = await fetch(`${service}/creation/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: id, api_key: 'device-profile', messages: [{ role: 'user', content: prompt }] }) })
    if (!response.ok || !response.body) throw new Error('模型体验请求失败')
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let result = ''
    try {
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const lines = buffer.split('\n'); buffer = lines.pop() || ''
        for (const line of lines) if (line.startsWith('data:')) {
          const event = JSON.parse(line.slice(5))
          if (event.error) throw new Error(event.error)
          result += event.content || ''; setReply(result)
        }
        if (done) break
      }
    } finally { reader.releaseLock() }
  }
  const modelChoices = <div className="user-models user-models__choices" aria-label="自定义模型选择">{profiles.map(p => {
    const active = isAnalysis ? analysisId === p.id : configs.some(c => c.id === p.id && c.enabled)
    return <article key={p.id} className="user-models__card" aria-label={p.name} data-active={active}>
      <div className="user-models__card-main">
        <div className="user-models__card-heading">
          <span className="user-models__card-name">{p.name}</span>
          <span className="user-models__badge">{p.protocol === 'cli' ? '本地客户端' : p.protocol === 'gateway' ? gateways.find(g => g.id === p.provider)?.name || '平台网关' : '自定义 API'}</span>
        </div>
        {p.model && <div className="user-models__card-description">{p.model}</div>}
      </div>
      <div className="user-models__card-controls">
        <span className="user-models__card-status" data-active={active}><i aria-hidden="true" />{active ? '使用中' : '已配置'}</span>
        <div className="user-models__actions">
          {!active && <button type="button" className="user-models__action--activate" aria-label={`启用 ${p.name}`} disabled={busy} onClick={() => void mutate(() => select(p.id))}>启用</button>}
          <button type="button" className="user-models__action--edit" aria-label={`编辑 ${p.name}`} disabled={busy} onClick={() => edit(p)}>编辑</button>
          <button type="button" className="user-models__action--preview" aria-label={`体验 ${p.name}`} disabled={busy} onClick={() => { setPreview(p.id); setReply(''); setError('') }}>体验</button>
          <button type="button" className="user-models__action--delete" aria-label={`删除 ${p.name}`} disabled={busy} onClick={() => void mutate(() => remove(p.id))}>删除</button>
        </div>
      </div>
    </article>
  })}</div>
  return <>
    {selectionHost ? createPortal(modelChoices, selectionHost) : modelChoices}
    <section className="user-models" aria-label="自定义模型">
    {actionsHost ? createPortal(<button type="button" className="user-models__add" onClick={() => edit({ ...blank })} disabled={busy}>添加模型</button>, actionsHost) : <button type="button" onClick={() => edit({ ...blank })} disabled={busy}>添加模型</button>}
    {!draft && !preview && error && <p role="alert">{error}</p>}
    {preview && createPortal(<div className="user-models__overlay" onMouseDown={event => { if (event.target === event.currentTarget && !busy) closePreview() }}>
      <div className="user-models user-models__dialog" ref={dialogRef} role="dialog" tabIndex={-1} aria-modal="true" aria-labelledby={dialogTitle} aria-busy={busy} onKeyDown={handleDialogKeyDown}>
        <div className="user-models__dialog-heading"><h3 id={dialogTitle}>体验模型 · {profiles.find(p => p.id === preview)?.name}</h3><button type="button" aria-label="关闭模型体验" disabled={busy} onClick={closePreview}>×</button></div>
        <form className="user-models__fields" onSubmit={event => { event.preventDefault(); if (prompt.trim() && !busy) void mutate(() => chat(preview)) }}>
          <label>体验消息<textarea rows={3} required value={prompt} disabled={busy} onChange={event => setPrompt(event.target.value)} /></label>
          <div className="user-models__reply" role="log" aria-label="模型回复" aria-live="polite">{reply || (busy ? '正在等待模型回复…' : '发送消息后，模型回复将显示在这里。')}</div>
          {error && <p role="alert">{error}</p>}
          <div className="user-models__dialog-actions"><span /><button type="button" disabled={busy} onClick={closePreview}>关闭</button><button type="submit" disabled={busy || !prompt.trim()}>{busy ? '推理中…' : '发送'}</button></div>
        </form>
      </div>
    </div>, document.body)}
    {draft && createPortal(<div className="user-models__overlay" onMouseDown={event => { if (event.target === event.currentTarget && !busy) closeDialog() }}>
      <div className="user-models user-models__dialog" ref={dialogRef} role="dialog" tabIndex={-1} aria-modal="true" aria-labelledby={dialogTitle} aria-busy={busy} onKeyDown={handleDialogKeyDown}>
      <div className="user-models__dialog-heading"><h3 id={dialogTitle}>{draft.id ? '编辑模型' : '添加模型'}</h3><button type="button" aria-label="关闭模型配置" disabled={busy} onClick={closeDialog}>×</button></div>
      <form onChange={() => setConnectionResult('')} onSubmit={e => { e.preventDefault(); void mutate(save) }} style={{ marginTop: 12 }}><fieldset disabled={busy} className="user-models__fields">
    {templates.length > 0 && <ModelSelect className="user-models__select" label="推荐模型" title="运营模型目录" value="" remoteAllowed
      options={[{ id: '', name: '选择推荐模型', description: '选择后填写自己的接入配置' }, ...templates.map(t => ({ id: t.id, name: t.name, description: t.capabilities.join(' / ') }))]}
      renderIcon={() => <Cloud size={16} />} onChange={id => {
        const t = templates.find(t => t.id === id)
        if (t) edit({ ...blank, name: t.name, protocol: t.protocol, model: t.model, base_url: t.base_url })
      }} />}
      <label>模型名称 <input required value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
      <ModelSelect className="user-models__select" label="调用方式" value={draft.protocol === 'gateway' ? draft.provider || 'aliyun' : draft.protocol} remoteAllowed disabled={busy}
        options={[
          ...gateways.map(g => ({ id: g.id, name: `${g.name}模型网关`, description: g.description })),
          { id: 'openai', name: 'OpenAI 兼容 API', description: '使用自己的密钥连接兼容接口' },
          { id: 'anthropic', name: 'Anthropic API', description: '使用 Anthropic Messages 接口' },
          { id: 'cli', name: '本地客户端', description: '使用本机已安装并登录的客户端' },
        ]} renderIcon={option => option.id === 'cli' ? <Terminal size={16} /> : <Plug size={16} />}
        onChange={method => {
          const gateway = gateways.find(g => g.id === method)
          setKey('')
          setDraft({ ...draft, protocol: gateway ? 'gateway' : method, provider: gateway?.id,
            has_api_key: false, base_url: '', region: 'cn-beijing', workspace_id: '',
            ...(gateway ? { name: draft.name || gateway.name, model: gateway.model } : {}),
            ...(method === 'cli' ? { command: '', model: '' } : {}),
          })
          setArgs('[]')
        }} />
      {draft.protocol === 'cli' ? <>
        <ModelSelect className="user-models__select" label="客户端" value={draft.client} remoteAllowed disabled={busy}
          options={[
            { id: 'codex', name: 'Codex', description: '使用 Codex CLI 的推理能力' },
            { id: 'claude-code', name: 'Claude Code', description: '使用已登录的 Claude Code' },
            { id: 'minimax', name: 'MiniMax', description: '配置本机 MiniMax 客户端' },
            { id: 'kimi', name: 'Kimi', description: '使用 Kimi Code 客户端' },
            { id: 'glm', name: 'GLM', description: '配置本机 GLM 客户端' },
            { id: 'custom', name: '其他客户端', description: '指定可执行文件和调用参数' },
          ]} renderIcon={() => <Terminal size={16} />} onChange={client => { setDraft({ ...draft, client, command: '', model: '' }); setArgs('[]') }} />
        {(detecting || detectionMessage) && <p role="status">{detecting ? '正在探测本地客户端…' : detectionMessage}</p>}
        <label>可执行文件 <input required value={draft.command} placeholder="codex 或 /完整路径/codex" onChange={e => setDraft({ ...draft, command: e.target.value })} /></label>
        <label>调用参数（JSON 数组） <input value={args} onChange={e => setArgs(e.target.value)} /></label>
        <p>请先在客户端完成登录。探测结果可自行修改；模型 ID 留空时使用客户端默认模型。</p>
      </> : <>
        {draft.protocol === 'gateway' ? <>
          <p>平台自动配置调用地址。使用开放平台 API Key，密钥仅保存在本机。</p>
          {draft.provider === 'aliyun' && <>
            <ModelSelect label="百炼地域" value={draft.region || 'cn-beijing'} remoteAllowed options={[
              { id: 'cn-beijing', name: '华北2（北京）' }, { id: 'ap-southeast-1', name: '新加坡' },
              { id: 'us-east-1', name: '美国（弗吉尼亚）' }, { id: 'ap-northeast-1', name: '日本（东京）' },
            ]} onChange={region => { setDraft({ ...draft, region, has_api_key: false }); setKey('') }} />
            <label>业务空间 ID（可选）<input value={draft.workspace_id || ''} placeholder="填写后自动使用业务空间专属网关" onChange={e => { setDraft({ ...draft, workspace_id: e.target.value, has_api_key: false }); setKey('') }} /></label>
            <p>密钥须与地域一致；北京、新加坡可留空业务空间 ID。请使用百炼 API Key，无需 RAM AccessKey。</p>
          </>}
          <a href={gateways.find(g => g.id === draft.provider)?.key_url} target="_blank" rel="noreferrer">打开平台获取 API Key</a>
        </> : <label>API 地址 <input required type="url" value={draft.base_url} placeholder="https://api.example.com/v1" onChange={e => { setDraft({ ...draft, base_url: e.target.value, has_api_key: false }); setKey('') }} /></label>}
        <label>API Key <input required={draft.protocol === 'gateway' && !draft.has_api_key} type="password" autoComplete="new-password" value={key} placeholder={draft.has_api_key ? '已保存，留空保留' : draft.protocol === 'gateway' ? '填写该平台的 API Key' : '填写自己的 API Key；本地服务可留空'} onChange={e => setKey(e.target.value)} /></label>
      </>}
      <label>实际模型 ID {draft.protocol === 'cli' && '（可留空使用客户端默认模型）'} <input required={draft.protocol !== 'cli'} value={draft.model} onChange={e => setDraft({ ...draft, model: e.target.value })} /></label>
      {error && <p role="alert">{error}</p>}
      {connectionResult && <p className="user-models__success" role="status">{connectionResult}</p>}
      <div className="user-models__dialog-actions"><button type="button" disabled={busy || detecting} onClick={() => void mutate(testConnection)}>{busy ? '处理中…' : '测试连接'}</button><span /><button type="button" disabled={busy} onClick={closeDialog}>取消</button><button type="submit" disabled={busy || detecting}>保存模型</button></div>
    </fieldset></form></div></div>, document.body)}
  </section>
  </>
}
