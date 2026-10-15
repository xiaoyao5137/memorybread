import React, { useEffect, useRef, useState } from 'react'
import { emptyWorkContext, workPreviewText, workQualityProfiles,
  type CreationWorkContext, type WorkPreviewAction, type WorkPreviewResult } from '../utils/creationWorkContext'

interface Props {
  value?: CreationWorkContext | null
  onChange: (value: CreationWorkContext) => void
  disabled?: boolean
  canPreview?: boolean
  onPreview: (action: WorkPreviewAction, signal: AbortSignal) => Promise<WorkPreviewResult>
}

const controlStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', border: '1px solid var(--mb-border-strong)',
  borderRadius: 7, padding: '8px 10px', background: 'var(--mb-bg-elevated)', color: 'var(--mb-text-primary)',
  font: 'inherit', resize: 'vertical',
}
const buttonStyle: React.CSSProperties = {
  border: '1px solid var(--mb-border-strong)', borderRadius: 6, padding: '6px 10px',
  color: 'var(--mb-text-primary)', background: 'var(--mb-bg-elevated)', fontSize: 12, cursor: 'pointer',
}

export default function CreationWorkContextEditor({ value, onChange, disabled, canPreview, onPreview }: Props) {
  const context = value || emptyWorkContext()
  const [busy, setBusy] = useState<WorkPreviewAction | null>(null)
  const [preview, setPreview] = useState<WorkPreviewResult | null>(null)
  const [previewAction, setPreviewAction] = useState<WorkPreviewAction | null>(null)
  const [notice, setNotice] = useState('')
  const abort = useRef<AbortController | null>(null)
  useEffect(() => () => abort.current?.abort(), [])
  const update = (patch: Partial<CreationWorkContext>) => onChange({ ...context, ...patch })
  const textField = (key: 'purpose' | 'decision_goal' | 'reader_context' | 'constraints', label: string, placeholder: string) => (
    <label style={{ display: 'grid', gap: 5, fontSize: 13 }}>
      {label}
      <textarea value={context[key]} rows={key === 'purpose' ? 2 : 3} maxLength={1500}
        disabled={disabled || Boolean(busy)} placeholder={placeholder} style={controlStyle}
        onChange={event => update({ [key]: event.target.value,
          ...(['purpose', 'decision_goal'].includes(key) && event.target.value.trim() ? { review_enabled: true } : {}) })} />
    </label>
  )
  const startPreview = async (action: WorkPreviewAction) => {
    if (busy) return
    const controller = new AbortController()
    abort.current = controller
    setBusy(action); setNotice(''); setPreview(null); setPreviewAction(action)
    try {
      const result = await onPreview(action, controller.signal)
      if (!controller.signal.aborted) setPreview(result)
    } catch (error) {
      if (!controller.signal.aborted) setNotice(error instanceof Error ? error.message : '预览暂时不可用，请稍后重试')
    } finally {
      if (abort.current === controller) { abort.current = null; setBusy(null) }
    }
  }
  const saveSample = () => {
    if (!preview || preview.status !== 'candidate' || !preview.content) return
    if (context.style_samples.length >= 3) { setNotice('已保存三条表达样稿，请先移除不再使用的样稿'); return }
    if (preview.content.length > 1500) { setNotice('这份候选超过样稿上限，请复制认可的段落后放入表达样稿'); return }
    update({ style_samples: [...context.style_samples, preview.content] })
    setNotice('已保存为本次创作的表达样稿')
  }
  return <fieldset disabled={disabled} style={{ margin: 0, padding: 12, border: '1px solid var(--mb-border-strong)', borderRadius: 8, display: 'grid', gap: 12, minWidth: 0 }}>
    <legend style={{ fontSize: 13, padding: '0 5px' }}>工作场景与表达</legend>
    <label style={{ display: 'grid', gap: 5, fontSize: 13 }}>使用场景
      <select value={value?.profile_id || ''} disabled={Boolean(busy)} style={controlStyle} onChange={event => {
        const selected = workQualityProfiles.find(profile => profile.id === event.target.value)
        update({ profile_id: selected?.id || 'general', purpose: context.purpose || selected?.label || '', review_enabled: Boolean(selected) })
      }}>
        <option value="">默认写作（可选）</option>
        {workQualityProfiles.map(profile => <option key={profile.id} value={profile.id}>{profile.label}</option>)}
      </select>
    </label>
    {textField('purpose', '这份内容用于什么', '例如：在项目评审会上说明推荐方案和实施边界')}
    {textField('decision_goal', '希望读者决定什么', '例如：是否批准试点，以及需要哪些团队参与')}
    {textField('reader_context', '读者已知背景', '写明已有共识，不必在正文重复解释')}
    {textField('constraints', '资源与范围约束', '已有人员、时间、系统边界；不确定的安排可注明待确认')}
    <label style={{ display: 'flex', gap: 7, alignItems: 'start', fontSize: 12 }}>
      <input type="checkbox" checked={context.review_enabled} disabled={Boolean(busy)} onChange={event => update({ review_enabled: event.target.checked })} />
      检查成稿是否符合上述工作用途
    </label>
    <div style={{ display: 'grid', gap: 8 }}>
      <span style={{ fontSize: 13 }}>表达样稿（最多三条，每条一千五百字）</span>
      <span style={{ fontSize: 12, color: 'var(--mb-text-secondary)' }}>放入你认可的表达，只学习写法；样稿中的事实不会用于当前方案。</span>
      {context.style_samples.map((sample, index) => <div key={index} style={{ display: 'grid', gap: 5 }}>
        <textarea aria-label={`表达样稿 ${index + 1}`} value={sample} maxLength={1500} rows={4} style={controlStyle} disabled={Boolean(busy)}
          onChange={event => update({ style_samples: context.style_samples.map((current, position) => position === index ? event.target.value : current) })} />
        <button type="button" style={buttonStyle} disabled={Boolean(busy)} onClick={() => update({ style_samples: context.style_samples.filter((_, position) => position !== index) })}>移除样稿 {index + 1}</button>
      </div>)}
      {context.style_samples.length < 3 && <button type="button" style={buttonStyle} disabled={Boolean(busy)} onClick={() => update({ style_samples: [...context.style_samples, ''] })}>添加表达样稿</button>}
    </div>
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
      {([['trial', '试写关键段落'], ['review', '整篇决策审阅'], ['summary', '生成决策摘要']] as const).map(([action, label]) => <button type="button" key={action} style={buttonStyle}
        disabled={!canPreview || Boolean(busy)} onClick={() => void startPreview(action)}>{busy === action ? '正在预览…' : label}</button>)}
      {busy && <button type="button" style={buttonStyle} onClick={() => { abort.current?.abort(); abort.current = null; setBusy(null); setNotice('已取消预览，正文已保留') }}>取消预览</button>}
    </div>
    <p style={{ margin: 0, fontSize: 12, color: 'var(--mb-text-secondary)' }}>脑暴开始前填写用途和边界；开始后调整用于后续写作与审阅。预览使用本地创作模型，结果单独展示，可复制后继续修改正文。</p>
    {notice && <p role="status" style={{ margin: 0, fontSize: 12 }}>{notice}</p>}
    {preview && <section aria-label="工作文档预览结果" style={{ display: 'grid', gap: 8 }}>
      <strong style={{ fontSize: 13 }}>{preview.status === 'unverified' ? '候选尚未通过核验' : preview.status === 'revise' ? '审阅发现需调整内容' : preview.status === 'pass' ? '审阅通过' : '关键内容候选'}</strong>
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', font: 'inherit', fontSize: 12, margin: 0, maxHeight: 360, overflow: 'auto' }}>{workPreviewText(preview)}</pre>
      <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap' }}>
        <button type="button" style={buttonStyle} onClick={() => { void navigator.clipboard.writeText(preview.content || workPreviewText(preview)).then(() => setNotice('已复制预览结果')).catch(() => setNotice('复制失败，请手动选择预览文字')) }}>复制结果</button>
        {previewAction === 'trial' && preview.status === 'candidate' && <button type="button" style={buttonStyle} onClick={saveSample}>认可并保存为表达样稿</button>}
      </div>
    </section>}
  </fieldset>
}
