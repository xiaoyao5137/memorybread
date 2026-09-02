import React from 'react'
import { Check, Loader2, RotateCcw, Sparkles } from 'lucide-react'
import type { CreationBrainstormState } from '../../store/useAppStore'

interface Props {
  state: CreationBrainstormState | null
  loading: boolean
  applying: boolean
  applied: boolean
  error: string
  selectedOptionIds: string[]
  customSelected: boolean
  customAnswer: string
  continuationDirectionId: string
  customDirection: string
  onOptionToggle: (optionId: string, singleChoice: boolean) => void
  onCustomSelectedChange: (selected: boolean) => void
  onCustomAnswerChange: (value: string) => void
  onSubmitAnswer: () => void
  onSkip: () => void
  onContinuationDirectionChange: (directionId: string) => void
  onCustomDirectionChange: (value: string) => void
  onContinue: () => void
  onApply: () => void
  onRetry: () => void
}

const CreationInlineBrainstormCard: React.FC<Props> = ({
  state,
  loading,
  applying,
  applied,
  error,
  selectedOptionIds,
  customSelected,
  customAnswer,
  continuationDirectionId,
  customDirection,
  onOptionToggle,
  onCustomSelectedChange,
  onCustomAnswerChange,
  onSubmitAnswer,
  onSkip,
  onContinuationDirectionChange,
  onCustomDirectionChange,
  onContinue,
  onApply,
  onRetry,
}) => {
  const question = state?.current_question || null
  const singleChoice = question?.type !== 'multi_choice'
  const canSubmit = Boolean(question) && (
    customSelected ? Boolean(customAnswer.trim()) : selectedOptionIds.length > 0
  )
  const canContinue = Boolean(continuationDirectionId) && (
    continuationDirectionId !== '__custom__' || Boolean(customDirection.trim())
  )

  return (
    <article className="creation-brainstorm-turn creation-inline-brainstorm-turn" aria-label="Agent 消息">
      <div className="creation-chat-message__meta">
        <span>创作 Agent</span>
        <span className="creation-brainstorm-turn__step">局部脑暴</span>
      </div>
      <section className="creation-brainstorm-card creation-inline-brainstorm-card" aria-live="polite">
        {loading && !state ? (
          <div className="creation-inline-brainstorm-loading" role="status" aria-label="正在准备局部脑暴选项">
            <Loader2 size={18} className="spin" />
            <div>
              <strong>正在分析所选内容</strong>
              <p>Agent 会先给出可选择的方向，不会直接修改文档。</p>
            </div>
          </div>
        ) : question ? (
          <>
            <header>
              <div>
                <span className="creation-brainstorm-card__eyebrow">{question.dimension} · 第 {state!.depth + 1} 轮</span>
                <strong>{question.prompt}</strong>
              </div>
            </header>
            {question.why_now && <p className="creation-brainstorm-card__why">{question.why_now}</p>}
            <div
              className="creation-brainstorm-options"
              role={singleChoice ? 'radiogroup' : 'group'}
              aria-label="局部脑暴选项"
            >
              {question.options.map(option => {
                const selected = !customSelected && selectedOptionIds.includes(option.id)
                return (
                  <button
                    key={option.id}
                    type="button"
                    role={singleChoice ? 'radio' : 'checkbox'}
                    aria-checked={selected}
                    className={selected ? 'is-selected' : ''}
                    disabled={loading}
                    onClick={() => onOptionToggle(option.id, singleChoice)}
                  >
                    <span className="creation-brainstorm-options__mark">{selected && <Check size={12} />}</span>
                    <span>
                      <strong>{option.label}{option.recommended && <small>推荐</small>}</strong>
                      <small>{option.description}</small>
                    </span>
                  </button>
                )
              })}
              {question.allow_custom && (
                <button
                  type="button"
                  role={singleChoice ? 'radio' : 'checkbox'}
                  aria-checked={customSelected}
                  className={customSelected ? 'is-selected' : ''}
                  disabled={loading}
                  onClick={() => onCustomSelectedChange(!customSelected)}
                >
                  <span className="creation-brainstorm-options__mark">{customSelected && <Check size={12} />}</span>
                  <span><strong>自定义方向</strong><small>补充你希望探索或保留的具体想法</small></span>
                </button>
              )}
            </div>
            {customSelected && (
              <label className="creation-brainstorm-custom">
                你的想法
                <textarea
                  value={customAnswer}
                  placeholder={question.answer_template || '描述你希望采用的方向'}
                  onChange={event => onCustomAnswerChange(event.target.value)}
                />
              </label>
            )}
            <footer>
              {!question.required && <button type="button" className="is-secondary" disabled={loading} onClick={onSkip}>暂时跳过</button>}
              <button type="button" disabled={loading || !canSubmit} onClick={onSubmitAnswer}>
                {loading ? <Loader2 size={14} className="spin" /> : <Check size={14} />} 确认并继续
              </button>
            </footer>
          </>
        ) : state?.phase === 'ready' ? (
          <>
            <header>
              <div>
                <span className="creation-brainstorm-card__eyebrow">局部脑暴已收敛</span>
                <strong>选中的方向已经足够用于改写所选内容</strong>
              </div>
            </header>
            {state.decisions.length > 0 && (
              <div className="creation-inline-brainstorm-decisions" aria-label="已确认的脑暴方向">
                {state.decisions.map(decision => (
                  <div key={decision.question_id}>
                    <small>{decision.dimension}</small>
                    <strong>{decision.summary}</strong>
                  </div>
                ))}
              </div>
            )}
            {!applied && state.continuation_directions.length > 0 && (
              <div className="creation-inline-brainstorm-continuation">
                <span>还想继续探索？</span>
                <div className="creation-brainstorm-options creation-brainstorm-options--continuation" role="radiogroup" aria-label="继续局部脑暴方向">
                  {state.continuation_directions.map(direction => (
                    <button
                      key={direction.id}
                      type="button"
                      role="radio"
                      aria-checked={continuationDirectionId === direction.id}
                      className={continuationDirectionId === direction.id ? 'is-selected' : ''}
                      onClick={() => onContinuationDirectionChange(direction.id)}
                    >
                      <span className="creation-brainstorm-options__mark">{continuationDirectionId === direction.id && <Check size={12} />}</span>
                      <span><strong>{direction.label}{direction.recommended && <small>推荐</small>}</strong><small>{direction.description}</small></span>
                    </button>
                  ))}
                  <button
                    type="button"
                    role="radio"
                    aria-checked={continuationDirectionId === '__custom__'}
                    className={continuationDirectionId === '__custom__' ? 'is-selected' : ''}
                    onClick={() => onContinuationDirectionChange('__custom__')}
                  >
                    <span className="creation-brainstorm-options__mark">{continuationDirectionId === '__custom__' && <Check size={12} />}</span>
                    <span><strong>自定义脑暴方向</strong><small>从你指定的新角度继续追问</small></span>
                  </button>
                </div>
                {continuationDirectionId === '__custom__' && (
                  <textarea
                    aria-label="自定义继续脑暴方向"
                    value={customDirection}
                    placeholder="例如：从反对者视角挑战这段论证"
                    onChange={event => onCustomDirectionChange(event.target.value)}
                  />
                )}
              </div>
            )}
            <footer>
              {applied ? (
                <span className="creation-inline-brainstorm-applied"><Check size={14} /> 已应用到所选内容</span>
              ) : (
                <>
                  <button type="button" className="is-secondary" disabled={loading || applying || !canContinue} onClick={onContinue}>
                    <RotateCcw size={14} /> 继续脑暴
                  </button>
                  <button type="button" disabled={loading || applying} onClick={onApply}>
                    {applying ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />}
                    {applying ? '正在应用…' : '应用到所选内容'}
                  </button>
                </>
              )}
            </footer>
          </>
        ) : null}
        {loading && state && <div className="creation-inline-brainstorm-updating" role="status"><Loader2 size={13} className="spin" /> Agent 正在准备下一步…</div>}
        {error && (
          <div className="creation-brainstorm-card__error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={onRetry}>重试</button>
          </div>
        )}
      </section>
    </article>
  )
}

export default CreationInlineBrainstormCard
