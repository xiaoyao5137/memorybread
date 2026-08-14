import React, { useMemo, useState } from 'react'
import type { BakeInventoryTrendBucket, BakeOverview } from '../../types'
import BakeMemoryGraph from './BakeMemoryGraph'
import type { MemoryGraphAssets, MemoryGraphNode } from './memoryGraph'
import { BakeCard, BakeSectionHeader } from './BakeShared'

const trendSeries = [
  { key: 'memoryCount', label: '时间线', color: '#007AFF' },
  { key: 'knowledgeCount', label: '知识', color: '#34C759' },
  { key: 'templateCount', label: '文档', color: '#FF9500' },
  { key: 'sopCount', label: '操作', color: '#AF52DE' },
  { key: 'dataCount', label: '数据', color: '#FF2D55' },
] as const

const DAY_MS = 86_400_000

const trendRangeOptions = [
  { key: 'all', label: '全部', days: null },
  { key: '7d', label: '7天', days: 7 },
  { key: '30d', label: '30天', days: 30 },
  { key: '90d', label: '90天', days: 90 },
] as const

type TrendRangeKey = typeof trendRangeOptions[number]['key']

const emptyOverviewGraphAssets: MemoryGraphAssets = {
  knowledge: [],
  documents: [],
  operations: [],
  data: [],
}

const parseLocalDate = (year: string, month: string, day: string) => (
  new Date(Number(year), Number(month) - 1, Number(day)).getTime()
)

const parseTrendLabelRange = (label: string) => {
  const match = label.match(/^(\d{4})-(\d{2})-(\d{2})(?:-(\d{4})-(\d{2})-(\d{2}))?$/)
  if (!match) return null

  const startTs = parseLocalDate(match[1], match[2], match[3])
  const endTs = match[4]
    ? parseLocalDate(match[4], match[5], match[6])
    : startTs

  if (!Number.isFinite(startTs) || !Number.isFinite(endTs)) return null
  return { startTs, endTs }
}

const startOfLocalDay = (timestamp: number) => {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return 0
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

const addLocalDays = (dayStart: number, days: number) => {
  const date = new Date(dayStart)
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days).getTime()
}

const formatShortDate = (timestamp: number) => {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return '未知'
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${month}/${day}`
}

const formatFullDate = (timestamp: number) => {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return '未知日期'
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

const getBucketEndTs = (bucket: BakeInventoryTrendBucket) => (
  Number.isFinite(bucket.endTs) && bucket.endTs > 0 ? bucket.endTs : bucket.startTs
)

const getBucketStartDay = (bucket: BakeInventoryTrendBucket) => (
  parseTrendLabelRange(bucket.label)?.startTs ?? startOfLocalDay(bucket.startTs)
)

const getBucketEndDay = (bucket: BakeInventoryTrendBucket) => {
  const labelRange = parseTrendLabelRange(bucket.label)
  if (labelRange) return labelRange.endTs

  const startDay = getBucketStartDay(bucket)
  if (startDay <= 0) return 0
  const spanDays = Math.max(1, Math.round((getBucketEndTs(bucket) - bucket.startTs + 1) / DAY_MS))
  return addLocalDays(startDay, spanDays - 1)
}

const getBucketTotal = (bucket: BakeInventoryTrendBucket) => (
  trendSeries.reduce((sum, series) => sum + bucket[series.key], 0)
)

const createDailyBucket = (dayStart: number): BakeInventoryTrendBucket => ({
  label: formatFullDate(dayStart),
  startTs: dayStart,
  endTs: addLocalDays(dayStart, 1) - 1,
  memoryCount: 0,
  dataCount: 0,
  knowledgeCount: 0,
  templateCount: 0,
  sopCount: 0,
})

const addBucketCounts = (target: BakeInventoryTrendBucket, source: BakeInventoryTrendBucket) => {
  target.memoryCount += source.memoryCount
  target.dataCount += source.dataCount
  target.knowledgeCount += source.knowledgeCount
  target.templateCount += source.templateCount
  target.sopCount += source.sopCount
}

const getRecentDailyBuckets = (buckets: BakeInventoryTrendBucket[], days: number) => {
  const endDay = startOfLocalDay(Date.now())
  if (endDay <= 0) return []

  const startDay = addLocalDays(endDay, 1 - days)
  const bucketsByDay = new Map<number, BakeInventoryTrendBucket>()
  for (let index = 0; index < days; index += 1) {
    const dayStart = addLocalDays(startDay, index)
    bucketsByDay.set(dayStart, createDailyBucket(dayStart))
  }

  buckets.forEach(bucket => {
    const bucketDay = getBucketStartDay(bucket)
    const target = bucketsByDay.get(bucketDay)
    if (target) addBucketCounts(target, bucket)
  })

  return Array.from(bucketsByDay.values())
}

const getDisplayBuckets = (buckets: BakeInventoryTrendBucket[], range: TrendRangeKey) => {
  const option = trendRangeOptions.find(item => item.key === range) ?? trendRangeOptions[0]
  if (!option.days || buckets.length === 0) return buckets
  return getRecentDailyBuckets(buckets, option.days)
}

const getAxisTickIndexes = (bucketCount: number) => {
  if (bucketCount <= 0) return new Set<number>()
  if (bucketCount <= 7) {
    return new Set(Array.from({ length: bucketCount }, (_, index) => index))
  }

  const maxTicks = 6
  const step = Math.ceil((bucketCount - 1) / (maxTicks - 1))
  const indexes = new Set<number>([0, bucketCount - 1])
  for (let index = step; index < bucketCount - 1; index += step) {
    indexes.add(index)
  }
  return indexes
}

const getAxisLabelLines = (bucket: BakeInventoryTrendBucket) => {
  const startDay = getBucketStartDay(bucket)
  const endDay = getBucketEndDay(bucket)
  if (startDay <= 0 || endDay <= startDay) return [formatShortDate(bucket.startTs)]
  return [formatShortDate(startDay), formatShortDate(endDay)]
}

const getTooltipTitle = (bucket: BakeInventoryTrendBucket) => {
  const startDay = getBucketStartDay(bucket)
  const endDay = getBucketEndDay(bucket)
  if (startDay <= 0 || endDay <= startDay) return formatFullDate(bucket.startTs)
  return `${formatFullDate(startDay)} 至 ${formatFullDate(endDay)}`
}

const InventoryTrendChart: React.FC<{ overview: BakeOverview }> = ({ overview }) => {
  const [range, setRange] = useState<TrendRangeKey>('all')
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const buckets = useMemo(
    () => getDisplayBuckets(overview.inventoryTrend, range),
    [overview.inventoryTrend, range],
  )
  const maxValue = Math.max(1, ...buckets.flatMap(bucket => trendSeries.map(series => bucket[series.key])))
  const width = 720
  const height = 248
  const padding = { top: 20, right: 18, bottom: 52, left: 36 }
  const chartWidth = width - padding.left - padding.right
  const chartHeight = height - padding.top - padding.bottom
  const getX = (index: number) => padding.left + (buckets.length <= 1 ? chartWidth / 2 : (index / (buckets.length - 1)) * chartWidth)
  const getY = (value: number) => padding.top + chartHeight - (value / maxValue) * chartHeight
  const tickIndexes = useMemo(() => getAxisTickIndexes(buckets.length), [buckets.length])
  const hoveredBucket = hoverIndex !== null ? buckets[hoverIndex] : null
  const hoveredX = hoverIndex !== null ? getX(hoverIndex) : 0
  const tooltipLeft = `${Math.min(86, Math.max(6, (hoveredX / width) * 100))}%`
  const tooltipPlacement = hoverIndex !== null && hoverIndex <= 1
    ? 'start'
    : hoverIndex !== null && hoverIndex >= buckets.length - 2
      ? 'end'
      : 'middle'

  const getNearestBucketIndex = (svgX: number) => {
    if (buckets.length <= 1) return 0
    const clampedX = Math.min(padding.left + chartWidth, Math.max(padding.left, svgX))
    const ratio = (clampedX - padding.left) / chartWidth
    return Math.min(buckets.length - 1, Math.max(0, Math.round(ratio * (buckets.length - 1))))
  }

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement> | React.MouseEvent<HTMLDivElement>) => {
    if (buckets.length === 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    if (rect.width <= 0) return
    const svgX = ((event.clientX - rect.left) / rect.width) * width
    setHoverIndex(getNearestBucketIndex(svgX))
  }

  return (
    <BakeCard>
      <BakeSectionHeader
        title="记忆新增趋势"
        right={(
          <div className="bake-trend-header-tools">
            <div className="bake-trend-range" role="radiogroup" aria-label="趋势时间范围">
              {trendRangeOptions.map(option => (
                <button
                  key={option.key}
                  type="button"
                  className={`bake-btn bake-btn--compact ${range === option.key ? 'bake-btn--active' : ''}`.trim()}
                  aria-pressed={range === option.key}
                  onClick={() => {
                    setRange(option.key)
                    setHoverIndex(null)
                  }}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <div className="bake-trend-legend">
              {trendSeries.map(series => (
                <span key={series.key} className="bake-trend-legend__item">
                  <span className="bake-trend-legend__dot" style={{ background: series.color }} />
                  {series.label}
                </span>
              ))}
            </div>
          </div>
        )}
      />

      {overview.inventoryTrend.length === 0 ? (
        <div className="bake-trend-empty">暂无可展示的新增时间分布</div>
      ) : (
        <div
          className="bake-trend-chart"
          role="img"
          aria-label="记忆新增数量趋势图"
          onPointerMove={handlePointerMove}
          onMouseMove={handlePointerMove}
          onPointerLeave={() => setHoverIndex(null)}
          onMouseLeave={() => setHoverIndex(null)}
        >
          <svg
            viewBox={`0 0 ${width} ${height}`}
            preserveAspectRatio="none"
          >
            {[0, 0.5, 1].map(tick => {
              const y = padding.top + chartHeight * tick
              const label = Math.round(maxValue * (1 - tick))
              return (
                <g key={tick}>
                  <line x1={padding.left} y1={y} x2={width - padding.right} y2={y} className="bake-trend-chart__grid" />
                  <text x={padding.left - 10} y={y + 4} textAnchor="end" className="bake-trend-chart__axis">{label}</text>
                </g>
              )
            })}

            {trendSeries.map(series => {
              const points = buckets.map((bucket, index) => `${getX(index)},${getY(bucket[series.key])}`).join(' ')
              return (
                <g key={series.key}>
                  <polyline points={points} fill="none" stroke={series.color} strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
                  {buckets.map((bucket, index) => (
                    <circle
                      key={`${series.key}-${bucket.label}`}
                      className="bake-trend-chart__point"
                      cx={getX(index)}
                      cy={getY(bucket[series.key])}
                      r={hoverIndex === index ? 5 : 3.5}
                      fill={series.color}
                    />
                  ))}
                </g>
              )
            })}

            {hoveredBucket && (
              <g>
                <rect
                  x={Math.max(padding.left, hoveredX - 14)}
                  y={padding.top}
                  width={Math.min(28, width - padding.right - Math.max(padding.left, hoveredX - 14))}
                  height={chartHeight}
                  className="bake-trend-chart__hover-band"
                />
                <line
                  x1={hoveredX}
                  y1={padding.top}
                  x2={hoveredX}
                  y2={padding.top + chartHeight}
                  className="bake-trend-chart__hover-line"
                />
                {trendSeries.map(series => (
                  <circle
                    key={`hover-${series.key}`}
                    cx={hoveredX}
                    cy={getY(hoveredBucket[series.key])}
                    r="5.4"
                    fill={series.color}
                    className="bake-trend-chart__hover-point"
                  />
                ))}
              </g>
            )}

            {buckets.map((bucket, index) => {
              if (!tickIndexes.has(index)) return null
              const lines = getAxisLabelLines(bucket)
              return (
                <g key={bucket.label}>
                  <line x1={getX(index)} y1={padding.top + chartHeight} x2={getX(index)} y2={padding.top + chartHeight + 5} className="bake-trend-chart__tick" />
                  <text x={getX(index)} y={height - 28} textAnchor="middle" className="bake-trend-chart__axis">
                    {lines.map((line, lineIndex) => (
                      <tspan key={line} x={getX(index)} dy={lineIndex === 0 ? 0 : 12}>{line}</tspan>
                    ))}
                  </text>
                </g>
              )
            })}

            {buckets.map((bucket, index) => {
              const bandWidth = buckets.length <= 1 ? chartWidth : chartWidth / (buckets.length - 1)
              const x = buckets.length <= 1 ? padding.left : getX(index) - bandWidth / 2
              return (
                <rect
                  key={`hit-${bucket.label}`}
                  x={Math.max(padding.left, x)}
                  y={padding.top}
                  width={Math.min(bandWidth, width - padding.right - Math.max(padding.left, x))}
                  height={chartHeight}
                  className="bake-trend-chart__hit-area"
                />
              )
            })}
          </svg>

          {hoveredBucket && (
            <div
              className={`bake-trend-tooltip bake-trend-tooltip--${tooltipPlacement}`}
              style={{ left: tooltipLeft }}
            >
              <div className="bake-trend-tooltip__title">{getTooltipTitle(hoveredBucket)}</div>
              <div className="bake-trend-tooltip__total">新增合计 {getBucketTotal(hoveredBucket)}</div>
              {trendSeries.map(series => (
                <div key={series.key} className="bake-trend-tooltip__row">
                  <span className="bake-trend-tooltip__label">
                    <span className="bake-trend-legend__dot" style={{ background: series.color }} />
                    {series.label}
                  </span>
                  <span className="bake-trend-tooltip__value">{hoveredBucket[series.key]}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </BakeCard>
  )
}

const BakeOverviewTab: React.FC<{
  overview: BakeOverview
  graphAssets?: MemoryGraphAssets
  graphLoading?: boolean
  graphError?: string | null
  onRetryGraph?: () => void
  onOpenGraphNode?: (node: MemoryGraphNode) => void
  onSearchGraph?: (query: string) => Promise<MemoryGraphAssets>
  graphDefaultDateRange?: { fromMs: number; toMs: number }
}> = ({
  overview,
  graphAssets = emptyOverviewGraphAssets,
  graphLoading,
  graphError,
  onRetryGraph,
  onOpenGraphNode,
  onSearchGraph,
  graphDefaultDateRange,
}) => {

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div className="bake-grid-overview">
        <BakeCard><div className="bake-muted">时间线</div><div className="bake-stat-value">{overview.memoryCount}</div></BakeCard>
        <BakeCard><div className="bake-muted">文档</div><div className="bake-stat-value">{overview.templateCount}</div></BakeCard>
        <BakeCard><div className="bake-muted">知识</div><div className="bake-stat-value">{overview.knowledgeCount}</div></BakeCard>
        <BakeCard><div className="bake-muted">操作</div><div className="bake-stat-value">{overview.sopCount}</div></BakeCard>
        <BakeCard><div className="bake-muted">数据</div><div className="bake-stat-value">{overview.dataCount}</div></BakeCard>
      </div>

      <InventoryTrendChart overview={overview} />

      <BakeMemoryGraph
        assets={graphAssets}
        loading={graphLoading}
        error={graphError}
        mode="overview"
        onRetry={onRetryGraph}
        onOpenNode={onOpenGraphNode}
        onSearchAssets={onSearchGraph}
        defaultDateRange={graphDefaultDateRange}
        defaultScopeLabel="今日"
      />
    </div>
  )
}

export default BakeOverviewTab
