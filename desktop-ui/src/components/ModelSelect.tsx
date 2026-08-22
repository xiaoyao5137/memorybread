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
  label?: string
  value: string
  options: readonly ModelSelectOption[]
  disabled?: boolean
  remoteAllowed: boolean
  onChange: (modelId: string) => void
  title?: string
}

const getDisabledReason = (option: ModelSelectOption, remoteAllowed: boolean) =>
  option.remote && !remoteAllowed ? '登录且有可用 Credit 后可选' : ''

const ModelSelect: React.FC<ModelSelectProps> = ({
  label,
  value,
  options,
  disabled = false,
  remoteAllowed,
  onChange,
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
      // 展开前根据剩余空间决定向上还是向下弹出，避免列表被视口底部截断
      const rect = rootRef.current.getBoundingClientRect()
      const estimatedMenuHeight = Math.min(options.length, 4) * 54 + 16
      const spaceBelow = window.innerHeight - rect.bottom
      setDropUpward(spaceBelow < estimatedMenuHeight && rect.top > spaceBelow)
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

  return (
    <div
      className={`model-select${open && dropUpward ? ' model-select--drop-upward' : ''}`}
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
          {activeOption?.remote ? <Cloud size={15} /> : <HardDrive size={15} />}
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
                  {option.remote ? <Cloud size={16} /> : <HardDrive size={16} />}
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
