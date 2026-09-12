import React, { forwardRef, useRef, useState } from 'react'
import { nameConsultationImages } from '../utils/attachments'
import './ConsultationAttachments.css'

type Props = Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'value'> & {
  value: string
  images: Array<{ id: string; name: string; type: string }>
  onValueChange: (value: string) => void
}

export const ConsultationImageInput = forwardRef<HTMLTextAreaElement, Props>(({ images, value, onValueChange, onChange, onKeyDown, ...props }, forwardedRef) => {
  const ref = useRef<HTMLTextAreaElement | null>(null)
  const [cursor, setCursor] = useState(0)
  const [active, setActive] = useState(0)
  const [dismissed, setDismissed] = useState(false)
  const match = /@([^@\s]*)$/.exec(value.slice(0, cursor))
  const candidates = !dismissed && match ? nameConsultationImages(images).filter(item => item.type.startsWith('image/') && item.name.startsWith(match[1])) : []
  const insert = (name: string) => {
    const start = cursor - (match?.[0].length || 0)
    const text = `@${name} `
    onValueChange(value.slice(0, start) + text + value.slice(cursor))
    setDismissed(true)
    requestAnimationFrame(() => { ref.current?.focus(); ref.current?.setSelectionRange(start + text.length, start + text.length) })
  }
  return <>
    <textarea {...props} value={value} ref={node => {
      ref.current = node
      if (typeof forwardedRef === 'function') forwardedRef(node)
      else if (forwardedRef) forwardedRef.current = node
    }} onChange={event => {
      setCursor(event.target.selectionStart); setActive(0); setDismissed(false)
      onChange?.(event)
      if (!onChange) onValueChange(event.target.value)
    }} onSelect={event => setCursor(event.currentTarget.selectionStart)} onKeyDown={event => {
      if (candidates.length && !event.nativeEvent.isComposing && event.keyCode !== 229) {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault(); setActive((active + (event.key === 'ArrowDown' ? 1 : -1) + candidates.length) % candidates.length); return
        }
        if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); insert(candidates[active % candidates.length].name); return }
        if (event.key === 'Escape') { event.preventDefault(); setDismissed(true); return }
      }
      onKeyDown?.(event)
    }} />
    {candidates.length > 0 && <div className="consultation-image-mentions" role="listbox" aria-label="引用图片">
      {candidates.map((item, index) => <button type="button" role="option" aria-selected={index === active % candidates.length} key={item.id} onMouseDown={event => event.preventDefault()} onClick={() => insert(item.name)}>@{item.name}</button>)}
    </div>}
  </>
})
ConsultationImageInput.displayName = 'ConsultationImageInput'
