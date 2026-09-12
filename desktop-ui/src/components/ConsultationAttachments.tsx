import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { invoke } from '@tauri-apps/api/core'
import type { RagContext } from '../types'
import './ConsultationAttachments.css'
import { nameConsultationImages } from '../utils/attachments'

export type ConsultationAttachment = NonNullable<RagContext['attachments']>[number] & { dataUrl?: string }

export function consultationImages(contexts: RagContext[]): ConsultationAttachment[] {
  return contexts.flatMap(context => [
    ...(context.screenshot_path ? [{ id: context.screenshot_path, name: '咨询截屏', type: 'image/jpeg', size: 0, path: context.screenshot_path }] : []),
    ...(context.attachments || []),
  ]).filter((item, index, items) => items.findIndex(other => (other.path || other.id) === (item.path || item.id)) === index)
}

function Attachment({ item, onPreview, onRemove, disabled }: {
  item: ConsultationAttachment; onPreview: (src: string) => void; onRemove?: (id: string) => void; disabled?: boolean
}) {
  const [src, setSrc] = useState(item.dataUrl || item.data_url || '')
  const [error, setError] = useState('')
  const image = item.type.startsWith('image/')
  useEffect(() => {
    let cancelled = false
    setError('')
    if (item.dataUrl || item.data_url) { setSrc(item.dataUrl || item.data_url || ''); return }
    setSrc('')
    if (image && item.path) {
      invoke<string>('read_floating_assist_image_data_url', { path: item.path }).then(value => {
        if (!cancelled) setSrc(value)
      }).catch(() => { if (!cancelled) setError('原图暂不可用') })
    }
    return () => { cancelled = true }
  }, [item.path, item.dataUrl, item.data_url, image])
  return <span className="consultation-attachment">
    {image && <button type="button" className="consultation-attachment__thumbnail" aria-label={`查看图片 ${item.name}`} disabled={!src || !!error} onClick={() => onPreview(src)}>
      {src && !error ? <img src={src} alt={item.name} onError={() => setError('原图暂不可用')} /> : <span>{error ? '失效' : '…'}</span>}
    </button>}
    <span className="consultation-attachment__name" title={error || item.name}>{item.name}{error && ` · ${error}`}</span>
    {onRemove && <button type="button" disabled={disabled} aria-label={`移除 ${item.name}`} onClick={() => onRemove(item.id)}>×</button>}
  </span>
}

export function ConsultationAttachments({ items, onPreview, onRemove, disabled }: {
  items: ConsultationAttachment[]; onPreview: (src: string) => void; onRemove?: (id: string) => void; disabled?: boolean
}) {
  if (!items.length) return null
  return <div className="consultation-attachments" aria-label={`咨询附件，共 ${items.length} 个`}>
    {nameConsultationImages(items).map(item => <Attachment key={item.id} item={item} onPreview={onPreview} onRemove={onRemove} disabled={disabled} />)}
  </div>
}

export function ConsultationImagePreview({ src, onClose }: { src: string; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.stopPropagation(); onClose() }
      if (event.key === 'Tab') { event.preventDefault(); closeRef.current?.focus() }
    }
    window.addEventListener('keydown', handleKey, true)
    return () => { window.removeEventListener('keydown', handleKey, true); previous?.focus() }
  }, [onClose])
  return createPortal(<div className="consultation-image-preview" role="dialog" aria-modal="true" aria-label="图片原图预览" onClick={onClose}>
    <button type="button" ref={closeRef} aria-label="关闭图片预览" onClick={onClose}>×</button>
    <img src={src} alt="咨询图片原图" onClick={event => event.stopPropagation()} />
  </div>, document.body)
}
