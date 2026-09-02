import React from 'react'
import { createPortal } from 'react-dom'
import { Lightbulb, Loader2, Sparkles, X } from 'lucide-react'
import {
  inlineEditActionLabel,
  type CreationInlineEditAction,
  type CreationSelectionSnapshot,
} from './creationInlineEdit'

interface Props {
  snapshot: CreationSelectionSnapshot | null
  actions: CreationInlineEditAction[]
  customPrompt: string
  maxCustomPromptBytes: number
  promptOpen: boolean
  runningAction: CreationInlineEditAction | null
  error: string
  onInteractionStart: () => void
  onInteractionEnd: () => void
  onCustomPromptChange: (value: string) => void
  onPromptOpenChange: (open: boolean) => void
  onRun: (action: CreationInlineEditAction) => void
  onBrainstorm: () => void
  onCancel: () => void
  onClose: () => void
}

const actionTitle = (action: CreationInlineEditAction) => ({
  polish: '改善表达和语气，不新增事实',
  brainstorm: '围绕所选内容探索方向，确认后再写回文档',
  expand: '补充已有上下文支持的解释和过渡',
  elaborate: '补齐对象、条件、步骤、边界或验收维度',
}[action])

const CreationSelectionToolbar: React.FC<Props> = ({
  snapshot,
  actions,
  customPrompt,
  maxCustomPromptBytes,
  promptOpen,
  runningAction,
  error,
  onInteractionStart,
  onInteractionEnd,
  onCustomPromptChange,
  onPromptOpenChange,
  onRun,
  onBrainstorm,
  onCancel,
  onClose,
}) => {
  if (!snapshot) return null
  const left = Math.min(
    Math.max(snapshot.anchorRect.left + snapshot.anchorRect.width / 2, 190),
    window.innerWidth - 190,
  )
  const preferAbove = snapshot.anchorRect.top > 180
  const top = preferAbove ? snapshot.anchorRect.top - 10 : snapshot.anchorRect.bottom + 10

  return createPortal(
    <div
      className={`creation-selection-toolbar${preferAbove ? ' is-above' : ' is-below'}`}
      role="toolbar"
      aria-label="所选内容操作"
      style={{ left, top }}
      onPointerDownCapture={onInteractionStart}
      onMouseDownCapture={onInteractionStart}
      onMouseDown={(event) => {
        if (!(event.target instanceof HTMLTextAreaElement)) event.preventDefault()
      }}
    >
      <div className="creation-selection-toolbar__actions">
        {actions.map(action => (
          <button
            key={action}
            type="button"
            title={actionTitle(action)}
            disabled={Boolean(runningAction)}
            onClick={() => {
              if (action === 'brainstorm') onBrainstorm()
              else if (action === 'polish') onPromptOpenChange(true)
              else onRun(action)
              onInteractionEnd()
            }}
          >
            {runningAction === action
              ? <Loader2 size={14} className="spin" />
              : action === 'brainstorm' ? <Lightbulb size={14} /> : <Sparkles size={14} />}
            {inlineEditActionLabel(action)}
          </button>
        ))}
        {runningAction ? (
          <button type="button" className="is-danger" onClick={() => {
            onCancel()
            onInteractionEnd()
          }}>中止</button>
        ) : (
          <button type="button" aria-label="关闭所选内容操作" onClick={() => {
            onClose()
            onInteractionEnd()
          }}><X size={14} /></button>
        )}
      </div>
      {promptOpen && actions.includes('polish') && !runningAction && (
        <div className="creation-selection-toolbar__prompt">
          <label htmlFor="creation-inline-polish-prompt">补充你的润色要求（可选）</label>
          <textarea
            id="creation-inline-polish-prompt"
            autoFocus
            rows={3}
            value={customPrompt}
            placeholder="例如：更专业，但不要有官话"
            onChange={event => onCustomPromptChange(event.target.value)}
          />
          <div>
            <small>{new TextEncoder().encode(customPrompt).length}/{maxCustomPromptBytes} 字节</small>
            <button type="button" onClick={() => {
              onPromptOpenChange(false)
              onInteractionEnd()
            }}>取消</button>
            <button
              type="button"
              className="is-primary"
              disabled={new TextEncoder().encode(customPrompt).length > maxCustomPromptBytes}
              onClick={() => {
                onRun('polish')
                onInteractionEnd()
              }}
            >
              开始润色
            </button>
          </div>
        </div>
      )}
      {error && <div className="creation-selection-toolbar__error" role="alert">{error}</div>}
    </div>,
    document.body,
  )
}

export default CreationSelectionToolbar
