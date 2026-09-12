import { useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import type { CreationBrainstormQuestion, CreationBrainstormState } from '../store/useAppStore'
import { brainstormDecisionSummary, brainstormExplorationStageLabel } from '../utils/brainstormSummary'
import { BrainstormContext, splitBrainstormCopy } from './CreationBrainstormCopy'

interface Props {
  state: CreationBrainstormState
  rootRequest: string
  disabled: boolean
  onSave: (edits: Record<string, string>) => Promise<void>
  initialDrafts?: Record<string, string>
  onDraftChange: (drafts: Record<string, string>) => void
}

export default function CreationBriefEditor({ state, rootRequest, disabled, onSave, initialDrafts = {}, onDraftChange }: Props) {
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  const treeId = useId()
  const [treeExpanded, setTreeExpanded] = useState(false)
  const treeHeadingRef = useRef<HTMLDivElement>(null)
  const wasTreeExpandedRef = useRef(false)
  const selectedDetailsRef = useRef<HTMLDivElement>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>(initialDrafts)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const history = state.history || []
  const archived = state.archived_questions || []
  const allTurns = [...history, ...archived]
  const questions = [...history.map(turn => turn.question), ...(state.current_question ? [state.current_question] : []), ...archived.map(turn => turn.question)]
    .filter((question, index, items) => items.findIndex(item => item.id === question.id) === index)
  const selected = questions.find(question => question.id === selectedId)
  const selectedParent = questions.find(question => question.id === selected?.parent_question_id)
  const selectedParentBranch = selectedParent && (selected?.parent_option_id
    ? selectedParent.options.find(option => option.id === selected.parent_option_id)?.label || '原选项（已调整）'
    : allTurns.find(turn => turn.question.id === selectedParent.id)?.answer?.custom_text ? '自定义思路' : '整体思路')
  useEffect(() => {
    if (selectedId) selectedDetailsRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [selectedId])
  useEffect(() => {
    if (wasTreeExpandedRef.current && !treeExpanded) treeHeadingRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    wasTreeExpandedRef.current = treeExpanded
  }, [treeExpanded])
  const selectNode = (id: string) => {
    if (id === selectedId) selectedDetailsRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    setSelectedId(id)
  }
  const fields = [
    { id: 'root_request', label: '创作需求', value: state.root_request || rootRequest },
    ...state.decisions.map(decision => ({ id: decision.question_id, label: decision.dimension, value: decision.summary })),
    { id: 'open_flags', label: '待决定与补充', value: state.open_flags.join('\n') },
  ]
  const treeDepthStyle = (depth: number) => ({ '--creation-brief-tree-depth': depth } as CSSProperties)
  const renderQuestion = (question: CreationBrainstormQuestion, ancestors: string[] = []): React.ReactNode => {
    if (ancestors.includes(question.id)) return null
    const answer = allTurns.find(turn => turn.question.id === question.id)?.answer
    const children = questions.filter(child => child.parent_question_id === question.id)
    const isArchived = archived.some(turn => turn.question.id === question.id)
    const effectiveSource = state.decisions.find(decision => decision.question_id === question.id)?.source || answer?.source
    const hasManualEdit = state.brief_edits?.[question.id] !== undefined
    const directChildren = children.filter(child => !question.options.some(option => option.id === child.parent_option_id))
    const branchStatus = (branchChildren: CreationBrainstormQuestion[]) => {
      if (isArchived || hasManualEdit || effectiveSource !== 'user' || state.phase === 'abandoned'
        || branchChildren.some(child => !archived.some(turn => turn.question.id === child.id))) return null
      return <small className="creation-brief-tree__branch-status">{question.exploration_stage === 'validation' ? '已讨论' : '待深入'}</small>
    }
    const renderChildren = (branchChildren: CreationBrainstormQuestion[]) => branchChildren.length > 0
      ? <ul>{branchChildren.map(child => renderQuestion(child, [...ancestors, question.id]))}</ul>
      : null
    const optionBranches = question.options.filter(option => answer?.selected_option_ids.includes(option.id)
      || children.some(child => child.parent_option_id === option.id))
    return <li key={question.id} style={treeDepthStyle(ancestors.length * 2 + 1)}>
      <button type="button" aria-pressed={selectedId === question.id} onClick={() => selectNode(question.id)}>
        <span>{question.dimension}</span><small>{isArchived ? '已失效 · 可回看' : effectiveSource === 'user_excluded' ? '已排除' : answer ? '已回答' : '当前问题'} · {brainstormExplorationStageLabel(question.exploration_stage)}</small>
      </button>
      {(optionBranches.length > 0 || answer?.custom_text || directChildren.length > 0) && <ul>
        {optionBranches.map(option => {
          const branchChildren = children.filter(child => child.parent_option_id === option.id)
          const isSelected = answer?.selected_option_ids.includes(option.id)
          return <li key={option.id} style={treeDepthStyle(ancestors.length * 2 + 2)}>
            <button type="button" className="creation-brief-tree__option" onClick={() => selectNode(question.id)}>{option.label}</button>
            {(!isSelected || hasManualEdit) && <small className="creation-brief-tree__branch-status">原分支</small>}
            {renderChildren(branchChildren)}
            {isSelected && branchStatus(branchChildren)}
          </li>
        })}
        {answer?.custom_text ? <li style={treeDepthStyle(ancestors.length * 2 + 2)}>
          <button type="button" className="creation-brief-tree__option" onClick={() => selectNode(question.id)}>自定义思路</button>
          {renderChildren(directChildren)}
          {branchStatus(directChildren)}
        </li> : directChildren.map(child => renderQuestion(child, [...ancestors, question.id]))}
      </ul>}
    </li>
  }
  const save = async () => {
    setSaving(true)
    setError('')
    try {
      await onSave(drafts)
      if (!mountedRef.current) return
      setDrafts({})
      onDraftChange({})
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : '简报保存失败，请重试')
    } finally { if (mountedRef.current) setSaving(false) }
  }
  return <div className="creation-brief-editor">
    <section className="creation-brief-tree" aria-label="脑暴思路树">
      <div className="creation-brief-tree__heading" ref={treeHeadingRef}><h3>脑暴思路树</h3>
        <button type="button" className="creation-brief-tree__toggle" aria-expanded={treeExpanded} aria-controls={treeId}
          aria-label={treeExpanded ? '收起思路树' : '展开完整思路树'} onClick={() => setTreeExpanded(expanded => !expanded)}>{treeExpanded ? '收起' : '展开查看'}</button>
      </div>
      <p>先逐一讨论同层方向，再沿已选思路探索解法、细化落地与验证效果。点击节点查看题目和回答。</p>
      <div id={treeId} className={`creation-brief-tree__scroll${treeExpanded ? ' is-expanded' : ''}`} tabIndex={0}><button type="button" onClick={() => selectNode("__root_request__")}>创作需求</button>
        <ul>{questions.filter(question => !question.parent_question_id || !questions.some(parent => parent.id === question.parent_question_id)).map(question => renderQuestion(question))}</ul>
      </div>
      {treeExpanded && <div className="creation-brief-tree__footer"><button type="button" aria-label="收起思路树并返回顶部" aria-expanded={treeExpanded} aria-controls={treeId}
        onClick={() => setTreeExpanded(false)}>收起思路树</button></div>}
      {(selectedId === "__root_request__" || selected) && <div ref={selectedDetailsRef}>
      {selectedId === "__root_request__" && <p>{state.brief_edits?.root_request ?? state.root_request ?? rootRequest}</p>}
      {selected && <section className="creation-brief-question" aria-label="思路节点题目">
        <h4>{selected.prompt}</h4>
        {selectedParent && <dl className="creation-brief-question__parent">
          <div><dt>父问题</dt><dd>{selectedParent.prompt}</dd></div>
          <div><dt>所属分支</dt><dd>{selectedParentBranch}</dd></div>
        </dl>}
        <BrainstormContext text={selected.why_now} details={selected.context_details} />
        <p>讨论阶段：{brainstormExplorationStageLabel(selected.exploration_stage)}</p>
        <small>{selected.type === 'multi_choice' ? '可多选' : '单选'}{selected.single_choice_reason ? ` · ${selected.single_choice_reason}` : ''}</small>
        <ul>{selected.options.map(option => {
          const { copy, details } = splitBrainstormCopy(option.description, option.details)
          return <li key={option.id}>
            <strong>{allTurns.find(turn => turn.question.id === selected.id)?.answer?.selected_option_ids.includes(option.id) ? '✓ ' : ''}{option.label}</strong>
            {copy && <p>{copy}</p>}
            {details && <details className="creation-brainstorm-copy-details"><summary>查看选项依据</summary><p>{details}</p></details>}
          </li>
        })}</ul>
        {allTurns.find(turn => turn.question.id === selected.id)?.answer?.custom_text && <p>{allTurns.find(turn => turn.question.id === selected.id)?.answer?.custom_text}</p>}
      </section>}
      </div>}
    </section>
    <div className="creation-brief-editor__heading"><h3>创作简报</h3><span>{brainstormDecisionSummary(state.decisions)}</span><span>版本 {state.revision}</span></div>
    <p>脑暴会持续补充简报。你可以直接修改下面的内容，保存后用于后续脑暴与创作。</p>
    {fields.map(field => <label className="creation-brief-field" key={field.id}>
      <span>{field.label}{state.brief_edits?.[field.id] !== undefined ? <small> · 已人工修改</small> : state.decisions.find(decision => decision.question_id === field.id)?.source === 'user_excluded' ? <small> · 已排除，重新回答可恢复</small> : state.decisions.find(decision => decision.question_id === field.id)?.source === 'agent_assumption' ? <small> · 合理假设</small> : null}</span>
      <textarea aria-label={`简报：${field.label}`} value={drafts[field.id] ?? state.brief_edits?.[field.id] ?? field.value}
        disabled={disabled || saving || state.decisions.some(decision => decision.question_id === field.id && decision.source === 'user_excluded')} maxLength={12000} rows={field.id === 'root_request' ? 3 : 4}
        onChange={event => { const next = { ...drafts, [field.id]: event.target.value }; setDrafts(next); onDraftChange(next) }} />
    </label>)}
    <div className="creation-brief-editor__save"><button type="button" disabled={disabled || saving || !Object.keys(drafts).length} onClick={() => void save()}>{saving ? '正在保存…' : '保存简报修改'}</button>
      <span role="status">{Object.keys(drafts).length ? '有未保存修改，请保存后继续脑暴或创作' : '简报已保存'}</span></div>
    {error && <p role="alert">{error}</p>}
  </div>
}
