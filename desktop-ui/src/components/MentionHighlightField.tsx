import React, { forwardRef, useMemo, useRef } from 'react'
import './MentionHighlightField.css'

interface MentionHighlightBaseProps {
  value: string
  mentionLabels: string[]
}

type MentionHighlightTextareaProps = MentionHighlightBaseProps
  & Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'value'>

type MentionHighlightInputProps = MentionHighlightBaseProps
  & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value'>

const MENTION_NAME_CHAR_SOURCE = 'A-Za-z0-9_\\u4e00-\\u9fa5-'

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const highlightedMentionText = (value: string, mentionLabels: string[], multiline: boolean) => {
  const knownMentions = Array.from(new Set(
    mentionLabels
      .map(label => label.trim())
      .filter(Boolean)
      .map(label => label.startsWith('@') ? label : `@${label}`),
  )).sort((left, right) => right.length - left.length)

  const knownPattern = knownMentions.length
    ? `(?:${knownMentions.map(escapeRegExp).join('|')})(?![${MENTION_NAME_CHAR_SOURCE}])|`
    : ''
  const matcher = new RegExp(`(${knownPattern}@[${MENTION_NAME_CHAR_SOURCE}]{2,40})`, 'g')
  const content: React.ReactNode[] = []
  let cursor = 0

  value.replace(matcher, (mention, _group, offset: number) => {
    if (offset > cursor) content.push(value.slice(cursor, offset))
    content.push(<mark className="mention-highlight-field__mention" key={`${offset}-${mention}`}>{mention}</mark>)
    cursor = offset + mention.length
    return mention
  })
  if (cursor < value.length) content.push(value.slice(cursor))
  if (multiline) content.push('\n')
  return content
}

export const MentionHighlightTextarea = forwardRef<HTMLTextAreaElement, MentionHighlightTextareaProps>(({
  value,
  mentionLabels,
  className = '',
  onScroll,
  ...props
}, ref) => {
  const mirrorRef = useRef<HTMLDivElement>(null)
  const highlighted = useMemo(
    () => highlightedMentionText(value, mentionLabels, true),
    [mentionLabels, value],
  )

  return (
    <div className="mention-highlight-field is-multiline">
      <div ref={mirrorRef} className="mention-highlight-field__mirror" aria-hidden="true">{highlighted}</div>
      <textarea
        {...props}
        ref={ref}
        value={value}
        className={`mention-highlight-field__control ${className}`.trim()}
        onScroll={(event) => {
          if (mirrorRef.current) {
            mirrorRef.current.scrollTop = event.currentTarget.scrollTop
            mirrorRef.current.scrollLeft = event.currentTarget.scrollLeft
          }
          onScroll?.(event)
        }}
      />
    </div>
  )
})

MentionHighlightTextarea.displayName = 'MentionHighlightTextarea'

export const MentionHighlightInput = forwardRef<HTMLInputElement, MentionHighlightInputProps>(({
  value,
  mentionLabels,
  className = '',
  onScroll,
  ...props
}, ref) => {
  const mirrorRef = useRef<HTMLDivElement>(null)
  const highlighted = useMemo(
    () => highlightedMentionText(value, mentionLabels, false),
    [mentionLabels, value],
  )

  return (
    <div className="mention-highlight-field is-single-line">
      <div ref={mirrorRef} className="mention-highlight-field__mirror" aria-hidden="true">{highlighted}</div>
      <input
        {...props}
        ref={ref}
        value={value}
        className={`mention-highlight-field__control ${className}`.trim()}
        onScroll={(event) => {
          if (mirrorRef.current) mirrorRef.current.scrollLeft = event.currentTarget.scrollLeft
          onScroll?.(event)
        }}
      />
    </div>
  )
})

MentionHighlightInput.displayName = 'MentionHighlightInput'
