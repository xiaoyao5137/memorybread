import { Star } from 'lucide-react'
import React from 'react'
import type { MemoryFavoriteFilter } from '../../types'

export const BakeFavoriteFilterControl: React.FC<{
  value: MemoryFavoriteFilter
  onChange: (value: MemoryFavoriteFilter) => void
}> = ({ value, onChange }) => (
  <label className="bake-form-field bake-filter-field bake-filter-field--favorite bake-filter-field--select">
    <span className="bake-filter-label">收藏状态</span>
    <select
      className="bake-input"
      value={value}
      aria-label="收藏状态"
      onChange={(event) => onChange(event.target.value as MemoryFavoriteFilter)}
    >
      <option value="all">全部</option>
      <option value="favorite">已收藏</option>
      <option value="not_favorite">未收藏</option>
    </select>
  </label>
)

export const BakeFavoriteButton: React.FC<{
  isFavorite: boolean
  busy?: boolean
  onToggle: () => void
}> = ({ isFavorite, busy = false, onToggle }) => (
  <button
    type="button"
    className={`bake-favorite-button${isFavorite ? ' bake-favorite-button--active' : ''}`}
    aria-pressed={isFavorite}
    aria-label={isFavorite ? '取消收藏' : '收藏'}
    disabled={busy}
    onClick={onToggle}
  >
    <Star size={16} strokeWidth={1.9} fill={isFavorite ? 'currentColor' : 'none'} aria-hidden="true" />
    <span>{busy ? '更新中…' : isFavorite ? '已收藏' : '收藏'}</span>
  </button>
)
