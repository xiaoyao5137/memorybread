import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Cloud, HardDrive } from 'lucide-react'
import './ModelSelect.css'

export interface ModelSelectOption {
  id: string
  name: string
  shortName?: string
  description?: string
  remote?: boolean
}

interface ModelSelectProps {
  className?: string
  label?: string
  value: string
  options: readonly ModelSelectOption[]
  disabled?: boolean
  remoteAllowed: boolean
  onChange: (modelId: string) => void
  renderIcon?: (option: ModelSelectOption) => React.ReactNode
  title?: string
}

const getDisabledReason = (option: ModelSelectOption, remoteAllowed: boolean) =>
  option.remote && !remoteAllowed ? '登录且有可用 Credit 后可选' : ''

const ModelSelect: React.FC<ModelSelectProps> = ({
  className,
  label,
  value,
  options,
  disabled = false,
  remoteAllowed,
  onChange,
  renderIcon,
  title,
}) => {
  const [open, setOpen] = useState(false)
  const [dropUpward, setDropUpward] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const activeOption = useMemo(
    () => options.find(option => option.id === value) || options[0],
    [options, value],
  )

  const toggleOpen = () => {
    if (!open && rootRef.current) {
      // 同时考虑视口和可滚动祖先的可见边界，避免分栏改为上下布局后菜单被面板裁切。
      const rect = rootRef.current.getBoundingClientRect()
      const estimatedMenuHeight = Math.min(options.length, 4) * 54 + 16
      let visibleTop = 0
      let visibleBottom = window.innerHeight
      let ancestor = rootRef.current.parentElement
      while (ancestor) {
        const style = window.getComputedStyle(ancestor)
        const clipsVertically = /(auto|scroll|hidden|clip)/.test(`${style.overflow} ${style.overflowY}`)
        if (clipsVertically) {
          const ancestorRect = ancestor.getBoundingClientRect()
          visibleTop = Math.max(visibleTop, ancestorRect.top)
          visibleBottom = Math.min(visibleBottom, ancestorRect.bottom)
        }
        ancestor = ancestor.parentElement
      }
      const spaceBelow = visibleBottom - rect.bottom
      const spaceAbove = rect.top - visibleTop
      setDropUpward(spaceBelow < estimatedMenuHeight && spaceAbove > spaceBelow)
    }
    setOpen(!open)
  }

  useEffect(() => {
    if (!open) return

    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  const handleChoose = (option: ModelSelectOption) => {
    if (disabled || getDisabledReason(option, remoteAllowed)) return
    onChange(option.id)
    setOpen(false)
  }

  const optionIcon = (option: ModelSelectOption) => (
    renderIcon?.(option) || (option.remote ? <Cloud size={16} /> : <HardDrive size={16} />)
  )

  return (
    <div
      className={`model-select${className ? ` ${className}` : ''}${open && dropUpward ? ' model-select--drop-upward' : ''}`}
      ref={rootRef}
    >
      {label && <span className="model-select__label">{label}</span>}
      <button
        type="button"
        className="model-select__trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={title || label || '选择模型'}
        title={title}
        onClick={toggleOpen}
      >
        <span className="model-select__icon" aria-hidden="true">
          {activeOption && optionIcon(activeOption)}
        </span>
        <span className="model-select__value">{activeOption?.name || '选择模型'}</span>
        <ChevronDown className="model-select__chevron" size={16} aria-hidden="true" />
      </button>

      {open && (
        <div className="model-select__menu" role="listbox" aria-label={title || label || '选择模型'}>
          {options.map(option => {
            const reason = getDisabledReason(option, remoteAllowed)
            const selected = option.id === value
            return (
              <button
                type="button"
                key={option.id}
                className={`model-select__option${selected ? ' model-select__option--selected' : ''}`}
                role="option"
                aria-selected={selected}
                aria-disabled={Boolean(reason)}
                disabled={Boolean(reason)}
                onClick={() => handleChoose(option)}
              >
                <span className="model-select__option-icon" aria-hidden="true">
                  {optionIcon(option)}
                </span>
                <span className="model-select__option-copy">
                  <span className="model-select__option-name">{option.name}</span>
                  <span className="model-select__option-desc">
                    {reason || option.description || (option.remote ? '云端模型' : '本地模型')}
                  </span>
                </span>
                {selected && <Check className="model-select__check" size={16} aria-hidden="true" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default ModelSelect
