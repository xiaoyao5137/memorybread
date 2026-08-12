import React, { useEffect, useRef } from 'react'
import {
  ArrowRight,
  Award,
  BookOpen,
  Briefcase,
  Code2,
  Flame,
  Focus,
  Moon,
  PenTool,
  Sparkles,
  type LucideIcon,
} from 'lucide-react'
import type { BreadcrumbAward } from '../types'
import './AchievementCelebration.css'

interface AchievementCelebrationProps {
  awards: BreadcrumbAward[]
  onDismiss: () => void
  onViewCards: () => void
}

const BADGE_ICONS: Record<string, LucideIcon> = {
  code: Code2,
  blueprint: PenTool,
  moon: Moon,
  focus: Focus,
  book: BookOpen,
  flame: Flame,
}

const CRUMB_COUNT = 12

const AchievementCelebration: React.FC<AchievementCelebrationProps> = ({
  awards,
  onDismiss,
  onViewCards,
}) => {
  const dialogRef = useRef<HTMLElement>(null)
  const primaryActionRef = useRef<HTMLButtonElement>(null)
  const primaryAward = awards[0]
  const primaryBadge = primaryAward?.breadcrumb
  const Icon = BADGE_ICONS[primaryBadge?.icon_key] ?? Briefcase
  const totalAwardQuantity = awards.reduce((sum, award) => sum + award.increment, 0)
  const awardNames = awards
    .map(({ breadcrumb, increment }) => (
      `「${breadcrumb.name}」${increment > 1 ? ` ×${increment}` : ''}`
    ))
    .join('、')

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    const focusFrame = window.requestAnimationFrame(() => primaryActionRef.current?.focus())
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onDismiss()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
      )
      if (focusable.length < 2) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.removeEventListener('keydown', handleKeyDown)
      previouslyFocused?.focus()
    }
  }, [onDismiss])

  if (!primaryBadge) return null

  return (
    <div className="achievement-celebration" data-testid="achievement-celebration">
      <div className="achievement-celebration__crumbs" aria-hidden="true">
        {Array.from({ length: CRUMB_COUNT }, (_, index) => <span key={index} />)}
      </div>
      <section
        aria-describedby="achievement-celebration-description"
        aria-labelledby="achievement-celebration-title"
        aria-live="polite"
        aria-modal="true"
        className="achievement-celebration__dialog"
        ref={dialogRef}
        role="dialog"
      >
        <div className="achievement-celebration__intro">
          <span className="achievement-celebration__spark" aria-hidden="true">
            <Sparkles size={18} strokeWidth={2.2} />
          </span>
          <span>{totalAwardQuantity > 1 ? `获得 ${totalAwardQuantity} 枚面包屑` : '获得新面包屑'}</span>
        </div>

        <div
          className={`achievement-celebration__card achievement-celebration__card--${primaryBadge.palette_key}`}
        >
          <span className="achievement-celebration__icon" aria-hidden="true">
            <Icon size={38} strokeWidth={2.1} />
          </span>
          <span className="achievement-celebration__card-status">
            <Award size={13} aria-hidden="true" />
            本次 +{primaryAward.increment} · 累计 ×{primaryAward.total_quantity}
          </span>
          <strong>{primaryBadge.name}</strong>
          <span>{primaryBadge.tagline}</span>
          {awards.length > 1 && (
            <small>
              本次还获得 {awards.slice(1)
                .map(({ breadcrumb, increment }) => `${breadcrumb.name} ×${increment}`)
                .join('、')}
            </small>
          )}
        </div>

        <div className="achievement-celebration__copy">
          <h2 id="achievement-celebration-title">面包屑已经烘焙完成</h2>
          <p id="achievement-celebration-description">
            你获得了{awardNames}。前往面包屑页查看详情，也可以把它佩戴到头像或悬浮球。
          </p>
        </div>

        <div className="achievement-celebration__actions">
          <button onClick={onDismiss} type="button">稍后查看</button>
          <button onClick={onViewCards} ref={primaryActionRef} type="button">
            去查收 <ArrowRight size={16} aria-hidden="true" />
          </button>
        </div>
      </section>
    </div>
  )
}

export default AchievementCelebration
