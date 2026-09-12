import React, { useState } from 'react'
import { Check } from 'lucide-react'
import type { CreationBrainstormOption } from '../store/useAppStore'

// Older saved questions appended evidence to the decision copy. Keep that
// evidence available without requiring the reader to scan it before choosing.
export function splitBrainstormCopy(text: string, details = '') {
  const source = text.trim()
  const marker = /(?:本轮已检索历史记忆|本轮未检索历史记忆|本轮未检索到相关历史记忆|本轮历史记忆检索失败|本轮部分记忆检索失败|本轮按你的资料范围要求未检索历史记忆|沿用历史结论[：:]|记忆依据[：:]|待验证推演[：:]|待验证[：:]|基于当前输入的建议。)/.exec(source)
  const copy = marker ? source.slice(0, marker.index).trim() : source
  const legacyDetails = marker ? source.slice(marker.index).trim() : ''
  return { copy, details: [...new Set([details.trim(), legacyDetails].filter(Boolean))].join('\n\n') }
}

export function brainstormPreview(text: string, limit: number) {
  const characters = Array.from(text)
  return characters.length > limit ? `${characters.slice(0, limit - 1).join('').trimEnd()}…` : text
}

function brainstormDescriptionPreview(text: string) {
  // Generated knowledge titles can themselves be entire paragraphs. Preserve
  // them in the disclosure while leaving room for the actual decision copy.
  const readable = text.replace(/《([^》\n]+)》/g, (source, title: string) => (
    Array.from(title).length > 24 ? '《历史资料》' : source
  ))
  return brainstormPreview(readable, 80)
}

export function BrainstormPrompt({ text }: { text: string }) {
  const [expandedText, setExpandedText] = useState('')
  const preview = brainstormPreview(text, 60)
  const expanded = expandedText === text
  return (
    <strong className="creation-brainstorm-prompt">
      <span>{expanded ? text : preview}</span>
      {preview !== text && (
        <button type="button" className="creation-brainstorm-copy-toggle" aria-expanded={expanded}
          onClick={() => setExpandedText(expanded ? '' : text)}>
          {expanded ? '收起完整问题' : '展开完整问题'}
        </button>
      )}
    </strong>
  )
}

export function BrainstormContext({ text, details = '' }: { text: string; details?: string }) {
  const { copy, details: evidence } = splitBrainstormCopy(text, details)
  const preview = brainstormDescriptionPreview(copy)
  const fullCopy = preview !== copy ? copy : ''
  if (!copy && !evidence) return null
  return (
    <div className="creation-brainstorm-context">
      {preview && <p className="creation-brainstorm-card__why">{preview}</p>}
      {(fullCopy || evidence) && (
        <details key={`${text}\n${details}`} className="creation-brainstorm-copy-details">
          <summary>{evidence ? '查看背景与依据' : '展开问题说明'}</summary>
          {fullCopy && <p>{fullCopy}</p>}
          {evidence && <p>{evidence}</p>}
        </details>
      )}
    </div>
  )
}

interface BrainstormChoiceProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  option: CreationBrainstormOption
}

export function BrainstormChoice({ option, ...buttonProps }: BrainstormChoiceProps) {
  const { copy, details } = splitBrainstormCopy(option.description, option.details)
  const labelPreview = brainstormPreview(option.label, 24)
  const copyPreview = brainstormDescriptionPreview(copy)
  const hasFullLabel = labelPreview !== option.label
  const hasFullCopy = copyPreview !== copy
  return (
    <>
      <button {...buttonProps}>
        <span className="creation-brainstorm-options__mark" aria-hidden>
          {buttonProps['aria-checked'] === true ? <Check size={13} /> : null}
        </span>
        <span>
          <strong><span>{labelPreview}</span>{option.recommended && <small>推荐</small>}</strong>
          {copyPreview && <small>{copyPreview}</small>}
        </span>
      </button>
      {(hasFullLabel || hasFullCopy || details) && (
        <details key={`${option.label}\n${option.description}\n${option.details || ''}`}
          className="creation-brainstorm-copy-details creation-brainstorm-choice-details">
          <summary>{hasFullLabel ? '展开完整选项' : details ? '查看选项依据' : '展开选项说明'}</summary>
          {hasFullLabel && <p className="creation-brainstorm-choice-details__label">{option.label}</p>}
          {hasFullCopy && <p>{copy}</p>}
          {details && <p>{details}</p>}
        </details>
      )}
    </>
  )
}
