import { Check, ChevronDown, Plus } from 'lucide-react'
import React, { useEffect, useId, useRef, useState } from 'react'

export const documentCategoryOptions = [
  { value: 'general_document', label: '通用文档' },
  { value: 'work_summary', label: '工作总结' },
  { value: 'weekly_report', label: '周报' },
  { value: 'monthly_report', label: '月报' },
  { value: 'meeting_minutes', label: '会议纪要' },
  { value: 'project_plan', label: '项目方案' },
  { value: 'research_report', label: '研究报告' },
  { value: 'guide', label: '说明文档' },
  { value: 'article', label: '文章' },
] as const

export const documentCategoryLabel = (value?: string) => {
  const trimmed = value?.trim() || ''
  const known = documentCategoryOptions.find(option => option.value === trimmed || option.label === trimmed)
  if (known) return known.label
  return trimmed && /[\u3400-\u9fff]/.test(trimmed) ? trimmed : '其他文档'
}

const BakeDocumentCategoryPicker: React.FC<{
  value: string
  onChange: (value: string) => void
  ariaLabel?: string
}> = ({ value, onChange, ariaLabel = '文档分类' }) => {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const listboxId = useId()
  const currentLabel = documentCategoryLabel(value)
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const filteredOptions = documentCategoryOptions.filter(option => (
    !normalizedQuery
    || option.label.toLocaleLowerCase('zh-CN').includes(normalizedQuery)
    || option.value.toLocaleLowerCase('zh-CN').includes(normalizedQuery)
  ))
  const exactOption = documentCategoryOptions.find(option => (
    option.label.toLocaleLowerCase('zh-CN') === normalizedQuery
    || option.value.toLocaleLowerCase('zh-CN') === normalizedQuery
  ))
  const canCreateCustom = Boolean(query.trim() && !exactOption)

  useEffect(() => {
    if (!open) return
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', closeOnOutsideClick)
    searchRef.current?.focus()
    return () => document.removeEventListener('mousedown', closeOnOutsideClick)
  }, [open])

  const selectCategory = (nextValue: string) => {
    onChange(nextValue)
    setQuery('')
    setOpen(false)
    window.requestAnimationFrame(() => triggerRef.current?.focus())
  }

  const commitQuery = () => {
    if (exactOption) selectCategory(exactOption.value)
    else if (query.trim()) selectCategory(query.trim())
  }

  return (
    <div className={`bake-category-picker ${open ? 'bake-category-picker--open' : ''}`} ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="bake-category-picker__trigger"
        role="combobox"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={listboxId}
        onClick={() => setOpen(previous => !previous)}
      >
        <span>{currentLabel}</span>
        <ChevronDown aria-hidden="true" size={16} strokeWidth={2.2} />
      </button>
      {open && (
        <div className="bake-category-picker__popover">
          <input
            ref={searchRef}
            className="bake-category-picker__search"
            value={query}
            aria-label="搜索或自定义文档分类"
            placeholder="搜索，或输入新的分类名称"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                commitQuery()
              } else if (event.key === 'Escape') {
                event.preventDefault()
                setOpen(false)
                triggerRef.current?.focus()
              }
            }}
          />
          <div className="bake-category-picker__hint">常用分类</div>
          <div className="bake-category-picker__options" id={listboxId} role="listbox" aria-label="文档分类选项">
            {filteredOptions.map(option => {
              const selected = option.value === value || option.label === value
              return (
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  key={option.value}
                  onClick={() => selectCategory(option.value)}
                >
                  <span>{option.label}</span>
                  {selected && <Check aria-hidden="true" size={15} strokeWidth={2.4} />}
                </button>
              )
            })}
            {filteredOptions.length === 0 && !canCreateCustom && (
              <div className="bake-category-picker__empty">没有匹配的常用分类</div>
            )}
          </div>
          {canCreateCustom && (
            <button
              type="button"
              className="bake-category-picker__custom"
              onClick={() => selectCategory(query.trim())}
            >
              <Plus aria-hidden="true" size={16} />
              <span>使用自定义分类</span>
              <strong>{query.trim()}</strong>
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export default BakeDocumentCategoryPicker
