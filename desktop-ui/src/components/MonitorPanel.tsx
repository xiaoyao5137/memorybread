import React, { useEffect, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import type { CaptureHealth, ExtractionLive, MonitorOverview, SystemResources } from '../types'
import { useAppStore } from '../store/useAppStore'
import { toUserFacingError } from '../utils/userFacingError'
import PipelineDagPanel from './PipelineDagPanel'
import './MonitorPanel.css'

const API = 'http://localhost:7070'

const EMPTY_OVERVIEW: MonitorOverview = {
  db_size_bytes: 0,
  capture_total_count: 0,
  service_health: {
    status: 'down',
    mode: 'unknown',
    full_dispatch_ready: false,
    background_processor_running: false,
    critical_checks_passed: false,
    embedding_ok: false,
    issues: [],
    updated_at_ms: null,
  },
  token_usage: {
    total_period: 0,
    total_today: 0,
    by_model: [],
    by_caller: [],
    trend: [],
    trend_by_model: [],
    bake_distribution: {
      sample_count: 0,
      truncated_count: 0,
      input_over_20k_count: 0,
      input: { p50: 0, p90: 0, p95: 0, p99: 0, max: 0, buckets: [] },
      output: { p50: 0, p90: 0, p95: 0, p99: 0, max: 0, buckets: [] },
    },
  },
  ocr_backfill: {
    submitted_total: 0,
    completed_total: 0,
    succeeded_total: 0,
    failed_total: 0,
    timed_out_total: 0,
    empty_total: 0,
    skipped_offline_total: 0,
    skipped_backpressure_total: 0,
    queued_count: 0,
    in_progress_count: 0,
    backlog_count: 0,
    period_completed: 0,
    period_succeeded: 0,
    period_failed: 0,
    period_timed_out: 0,
    period_empty: 0,
    period_skipped_offline: 0,
    period_skipped_backpressure: 0,
    period_success_rate: 0,
    period_throughput_per_min: 0,
    avg_latency_ms: 0,
    last_submitted_at_ms: null,
    last_completed_at_ms: null,
    recent: [],
  },
  capture_flow: {
    today_count: 0,
    period_count: 0,
    eligible_count: 0,
    vectorized_count: 0,
    vectorization_rate: 0,
    knowledge_generated_count: 0,
    knowledge_generation_rate: 0,
    knowledge_linked_count: 0,
    knowledge_rate: 0,
    by_hour: [],
    by_app: [],
    recent: [],
  },
  knowledge_flow: {
    today_count: 0,
    period_count: 0,
    capture_enabled: true,
    pending_extraction_count: 0,
    oldest_pending_extraction_at_ms: null,
    extraction_stalled: false,
    pending_bake_count: 0,
    oldest_pending_bake_at_ms: null,
    bake_retry_pending_count: 0,
    bake_fresh_pending_count: 0,
    bake_retry_ready_count: 0,
    bake_retry_delayed_count: 0,
    bake_actionable_count: 0,
    bake_retry_exhausted_count: 0,
    bake_timeout_failure_count: 0,
    bake_truncated_failure_count: 0,
    bake_other_failure_count: 0,
    bake_upstream_failure_count: 0,
    bake_recent_no_progress_count: 0,
    bake_next_retry_at_ms: null,
    bake_recommended_retry_after_ms: 0,
    bake_scheduler_mismatch: false,
    running_bake_count: 0,
    stale_bake_run_count: 0,
    bake_drain_rate_per_hour: 0,
    bake_eta_ms: null,
    by_time: [],
    recent: [],
    extracting: [],
    last_extraction_at_ms: null,
    extractor_status: 'idle',
  },
  operation_flow: {
    period_audited_count: 0,
    eligible_count: 0,
    needs_enrichment_count: 0,
    rejected_count: 0,
    eligible_rate: 0,
    pending_replay_count: 0,
    zero_eligible_alert: false,
  },
  rag_sessions: {
    today_count: 0,
    period_count: 0,
    avg_latency_ms: 0,
    recent: [],
  },
  task_executions: {
    total: 0,
    success: 0,
    failed: 0,
    success_rate: 0,
    recent: [],
  },
}

const CALLER_LABELS: Record<string, string> = {
  rag: '记忆问答', task: '定时任务', knowledge: '知识提炼', creation: '内容创作',
}
const CALLER_COLORS: Record<string, string> = {
  rag: '#007AFF', task: '#34C759', knowledge: '#AF52DE', creation: '#FF9500',
}
const STATUS_COLOR: Record<string, string> = {
  success: '#34C759', failed: '#FF3B30', running: '#FF9500',
}
const EVENT_COLOR: Record<string, string> = {
  load_done: '#34C759', load_start: '#FF9500', unload: '#6E6E73', load_failed: '#FF3B30',
}
const EVENT_LABEL: Record<string, string> = {
  load_done: '加载完成', load_start: '加载中', unload: '已卸载', load_failed: '加载失败',
}

function formatLlmModelName(model?: string | null): string {
  const name = (model || '').trim()
  if (!name || name === 'unavailable') return '模型状态待确认'
  if (/embed|bge|vector/i.test(name)) return 'MBEMB V1.0'
  if (/plus|opus/i.test(name)) return 'MBCD Plus v1.0'
  return 'MBEM v1.0'
}

function fmt(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

function fmtBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

function fmtTs(ms: number): string {
  return new Date(ms).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function fmtAxisTs(ms: number): string {
  return new Date(ms).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

type TimeUnit = 'minute' | 'hour' | 'day'
type OverviewRange = { amount: number; unit: TimeUnit }
type SystemRange = '1h' | '6h' | '24h' | '1d'

const UNIT_TO_MS: Record<TimeUnit, number> = {
  minute: 60 * 1000,
  hour: 60 * 60 * 1000,
  day: 24 * 60 * 60 * 1000,
}

const OVERVIEW_QUICK_RANGES: { label: string; range: OverviewRange }[] = [
  { label: '1小时', range: { amount: 1, unit: 'hour' } },
  { label: '6小时', range: { amount: 6, unit: 'hour' } },
  { label: '24小时', range: { amount: 24, unit: 'hour' } },
  { label: '7天', range: { amount: 7, unit: 'day' } },
  { label: '30天', range: { amount: 30, unit: 'day' } },
]

function overviewRangeToMs(range: OverviewRange): number {
  const safeAmount = Number.isFinite(range.amount) ? range.amount : 6
  return Math.max(1, safeAmount) * UNIT_TO_MS[range.unit]
}

function isSameOverviewRange(a: OverviewRange, b: OverviewRange): boolean {
  return a.amount === b.amount && a.unit === b.unit
}

function getNiceBucketMs(rangeMs: number): number {
  const buckets = [
    60 * 1000,
    5 * 60 * 1000,
    15 * 60 * 1000,
    60 * 60 * 1000,
    3 * 60 * 60 * 1000,
    6 * 60 * 60 * 1000,
    12 * 60 * 60 * 1000,
    24 * 60 * 60 * 1000,
  ]
  const wanted = Math.max(60 * 1000, rangeMs / 80)
  return buckets.find((bucket) => bucket >= wanted) ?? buckets[buckets.length - 1]
}

function fmtDuration(ms: number): string {
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  if (ms % day === 0) return `${ms / day} 天`
  if (ms % hour === 0) return `${ms / hour} 小时`
  if (ms % minute === 0) return `${ms / minute} 分钟`
  return `${Math.round(ms / minute)} 分钟`
}

function getOverviewBucketLabel(range: OverviewRange): string {
  return `约 ${fmtDuration(getNiceBucketMs(overviewRangeToMs(range)))}`
}

function getOverviewRangeLabel(range: OverviewRange): string {
  return `最近 ${fmtDuration(overviewRangeToMs(range))}`
}

function getTrendTitle(title: string, bucketLabel: string): string {
  return `${title}（${bucketLabel}）`
}

function getSystemBucketLabel(range: SystemRange): string {
  if (range === '1h') return '约 1 分钟'
  if (range === '6h') return '约 3 分钟'
  if (range === '24h' || range === '1d') return '约 1 分钟'
  return '约 1 分钟'
}

function fmtOverviewAxisTs(ms: number, range: OverviewRange): string {
  const date = new Date(Number(ms))
  if (Number.isNaN(date.getTime())) return '—'
  if (overviewRangeToMs(range) <= 24 * 60 * 60 * 1000) {
    return date.toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }
  return date.toLocaleDateString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
  })
}

function fmtSystemAxisTs(ms: number, range: SystemRange): string {
  const date = new Date(Number(ms))
  if (Number.isNaN(date.getTime())) return '—'
  if (range === '24h' || range === '1d') {
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function fmtMs(ms: number | null): string {
  if (!ms) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${ms}ms`
}

function fmtRatePerMin(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0/min'
  if (value < 1) return `${value.toFixed(2)}/min`
  if (value < 10) return `${value.toFixed(1)}/min`
  return `${Math.round(value)}/min`
}

const OCR_STATUS_META: Record<string, { label: string; color: string }> = {
  success: { label: '成功', color: '#34C759' },
  empty: { label: '空文本', color: 'var(--mb-text-tertiary)' },
  failed: { label: '失败', color: '#FF3B30' },
  timeout: { label: '超时', color: '#FF9500' },
  skipped_offline: { label: '离线跳过', color: 'var(--mb-text-secondary)' },
  skipped_backpressure: { label: '队列限流', color: '#FF9500' },
}

function fmtElapsed(deltaMs: number): string {
  if (deltaMs < 0) deltaMs = 0
  const totalSec = Math.floor(deltaMs / 1000)
  if (totalSec < 60) return `${totalSec}s`
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  if (m < 60) return `${m}m${s.toString().padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h${(m % 60).toString().padStart(2, '0')}m`
}

// ── 迷你折线图（纯 SVG）────────────────────────────────────────────────────
function sampleLineData<T extends { ts: number }>(data: T[], maxPoints = 120): T[] {
  if (data.length <= maxPoints) return data
  const lastIndex = data.length - 1
  const sampled: T[] = []
  for (let i = 0; i < maxPoints; i += 1) {
    const index = Math.round((i / Math.max(maxPoints - 1, 1)) * lastIndex)
    const point = data[index]
    if (!point || sampled[sampled.length - 1]?.ts === point.ts) continue
    sampled.push(point)
  }
  const tail = data[lastIndex]
  if (sampled[sampled.length - 1]?.ts !== tail.ts) sampled.push(tail)
  return sampled
}

type LinePoint = { ts: number; value: number }
type PlottedLinePoint = LinePoint & { x: number; y: number }
type MultiLineSeries = {
  label: string
  color: string
  data: LinePoint[]
  valueFormatter?: (value: number) => string
}

export const SparkLine: React.FC<{
  data?: LinePoint[]
  series?: MultiLineSeries[]
  color?: string
  height?: number
  valueFormatter?: (value: number) => string
  axisFormatter?: (ts: number) => string
  xDomain?: { start: number; end: number }
  bucketMs?: number
  fillMissingWithZero?: boolean
  detailFormatter?: (point: LinePoint) => string
}> = ({
  data,
  series,
  color = '#007AFF',
  height = 40,
  valueFormatter = (value) => String(value),
  axisFormatter = fmtAxisTs,
  xDomain,
  bucketMs,
  fillMissingWithZero = false,
  detailFormatter,
}) => {
  const [hoverX, setHoverX] = useState<number | null>(null)

  const sourceSeries = (series && series.length > 0)
    ? series
    : (data && data.length > 0 ? [{ label: '当前序列', color, data, valueFormatter }] : [])
  const rawDomainStart = xDomain?.start ?? sourceSeries.find((item) => item.data.length > 0)?.data[0]?.ts ?? Date.now()
  const lastSourceSeries = [...sourceSeries].reverse().find((item) => item.data.length > 0)
  const rawDomainEnd = xDomain?.end ?? lastSourceSeries?.data[lastSourceSeries.data.length - 1]?.ts ?? rawDomainStart
  const domainStart = Math.min(rawDomainStart, rawDomainEnd)
  const domainEnd = Math.max(rawDomainStart, rawDomainEnd)
  const bucket = Math.max(1, bucketMs ?? 0)
  const fillTimeline = fillMissingWithZero && bucketMs && xDomain
  const normalizedSeries = sourceSeries
    .map(item => {
      if (!fillTimeline) return { ...item, data: sampleLineData(item.data) }
      const values = new Map(item.data.map((point) => [Math.floor((point.ts - domainStart) / bucket) * bucket + domainStart, point.value]))
      const filled: LinePoint[] = []
      for (let ts = domainStart; ts <= domainEnd + 1; ts += bucket) {
        filled.push({ ts, value: values.get(ts) ?? 0 })
        if (filled.length > 360) break
      }
      if (filled[filled.length - 1]?.ts !== domainEnd) {
        filled.push({ ts: domainEnd, value: values.get(domainEnd) ?? 0 })
      }
      return { ...item, data: sampleLineData(filled) }
    })
    .filter(item => item.data.length > 0)

  if (!normalizedSeries.length) return null

  const baseData = normalizedSeries[0].data
  const w = 200
  const h = height
  const pad = 8
  const allValues = normalizedSeries.flatMap(item => item.data.map(d => d.value))
  const max = Math.max(...allValues, 1)
  const min = Math.min(...allValues, 0)
  const range = Math.max(max - min, 1)
  const axisValueFormatter = normalizedSeries[0].valueFormatter || valueFormatter
  const domainRange = Math.max(domainEnd - domainStart, 1)
  const xForTs = (ts: number) => pad + ((ts - domainStart) / domainRange) * (w - pad * 2)
  const clampChartX = (x: number) => Math.min(w - pad, Math.max(pad, x))

  const seriesPoints = normalizedSeries.map(item => {
    const points = item.data.map((d) => {
      const x = xForTs(d.ts)
      const y = h - pad - ((d.value - min) / range) * (h - pad * 2)
      return { ...d, x, y }
    })
    return {
      ...item,
      points,
      pts: points.map(p => `${p.x},${p.y}`).join(' '),
      area: `${pad},${h - pad} ${points.map(p => `${p.x},${p.y}`).join(' ')} ${w - pad},${h - pad}`,
    }
  })

  const nearestPointByX = <T extends { x: number }>(points: T[], targetX: number): T | null => {
    if (points.length === 0) return null
    return points.reduce((nearest, point) => (
      Math.abs(point.x - targetX) < Math.abs(nearest.x - targetX) ? point : nearest
    ), points[0])
  }
  const hoverAxisPoint = hoverX !== null ? nearestPointByX(seriesPoints[0].points, hoverX) : null
  const hoverTargetX = hoverAxisPoint?.x ?? null
  const hoverPoints = hoverTargetX !== null
    ? seriesPoints.map(item => nearestPointByX(item.points, hoverTargetX))
    : []
  const visibleHoverPoints = hoverPoints.reduce<Array<{ point: PlottedLinePoint; seriesIndex: number }>>((points, point, seriesIndex) => {
    if (point) points.push({ point, seriesIndex })
    return points
  }, [])
  const latestPoints = seriesPoints.reduce<PlottedLinePoint[]>((points, item) => {
    const point = item.points[item.points.length - 1]
    if (point) points.push(point)
    return points
  }, [])
  const hoverAxisTs = hoverAxisPoint?.ts ?? baseData[baseData.length - 1]?.ts ?? 0

  const handlePointerMove = (event: React.PointerEvent<SVGSVGElement> | React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (rect.width <= 0) return
    setHoverX(clampChartX(((event.clientX - rect.left) / rect.width) * w))
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'stretch', gap: 6 }}>
        <div style={{ width: 34, display: 'flex', flexDirection: 'column', justifyContent: 'space-between', fontSize: 9, color: 'var(--mb-text-faint)', textAlign: 'right', paddingTop: 2, paddingBottom: 22 }}>
          <span>{axisValueFormatter(max)}</span>
          <span>{axisValueFormatter((max + min) / 2)}</span>
          <span>{axisValueFormatter(min)}</span>
        </div>
        <div style={{
          flex: 1,
          border: '1px solid rgba(99,99,102,0.10)',
          borderRadius: 10,
          padding: '8px 8px 4px',
          background: 'linear-gradient(180deg, var(--mb-bg-card), var(--mb-bg-elevated)), repeating-linear-gradient(0deg, transparent 0 23px, rgba(142,142,147,0.11) 24px), repeating-linear-gradient(90deg, transparent 0 39px, rgba(142,142,147,0.09) 40px)',
          boxShadow: 'var(--mb-highlight-inset)',
        }}>
          <svg
            width="100%"
            viewBox={`0 0 ${w} ${h}`}
            preserveAspectRatio="none"
            style={{ display: 'block', overflow: 'visible' }}
            onPointerMove={handlePointerMove}
            onMouseMove={handlePointerMove}
            onPointerLeave={() => setHoverX(null)}
            onMouseLeave={() => setHoverX(null)}
          >
            <defs>
              {seriesPoints.map((item, seriesIndex) => (
                <linearGradient key={`${seriesIndex}-${item.color}`} id={`grad-${seriesIndex}-${item.color.replace('#', '')}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={item.color} stopOpacity="0.22" />
                  <stop offset="100%" stopColor={item.color} stopOpacity="0.01" />
                </linearGradient>
              ))}
            </defs>
            {seriesPoints.map((item, idx) => (
              <g key={`${idx}-${item.label}-${item.color}`}>
                {idx === 0 && <polygon points={item.area} fill={`url(#grad-${idx}-${item.color.replace('#', '')})`} />}
                <polyline points={item.pts} fill="none" stroke={item.color} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
              </g>
            ))}
            {baseData.map((_, i) => {
              const x = xForTs(baseData[i].ts)
              return (
                <rect
                  key={i}
                  x={x - 6}
                  y={0}
                  width={12}
                  height={h}
                  fill="transparent"
                  onFocus={() => setHoverX(x)}
                />
              )
            })}
            {visibleHoverPoints.length > 0 && (
              <line x1={visibleHoverPoints[0].point.x} y1={pad} x2={visibleHoverPoints[0].point.x} y2={h - pad} stroke="#AEAEB2" strokeOpacity="0.35" strokeDasharray="2 2" />
            )}
            {visibleHoverPoints.map(({ point, seriesIndex }) => (
              <circle key={`${point.ts}-${seriesIndex}`} cx={point.x} cy={point.y} r={3} fill={seriesPoints[seriesIndex].color} />
            ))}
          </svg>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4, fontSize: 10, color: 'var(--mb-text-faint)' }}>
            <span>{axisFormatter(domainStart)}</span>
            <span>{axisFormatter(domainStart + domainRange / 2)}</span>
            <span>{axisFormatter(domainEnd)}</span>
          </div>
        </div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginTop: 6, minHeight: 16 }}>
        {normalizedSeries.length === 1
          ? (() => {
              const singleData = normalizedSeries[0].data
              const singleHoverPoint = hoverPoints[0] ?? null
              return singleHoverPoint
                ? (detailFormatter ? detailFormatter(singleHoverPoint) : `${fmtTs(singleHoverPoint.ts)} · ${valueFormatter(singleHoverPoint.value)}`)
                : (detailFormatter
                    ? detailFormatter(singleData[singleData.length - 1])
                    : `最近: ${fmtTs(singleData[singleData.length - 1].ts)} · ${valueFormatter(singleData[singleData.length - 1].value)}`)
            })()
          : `${hoverTargetX !== null ? fmtTs(hoverAxisTs) : `最近: ${fmtTs(hoverAxisTs)}`} · ${normalizedSeries.map((item, index) => {
              const point = (hoverTargetX !== null ? hoverPoints[index] : latestPoints[index]) ?? item.data[item.data.length - 1]
              const formatter = item.valueFormatter || valueFormatter
              return `${item.label} ${formatter(point.value)}`
            }).join(' · ')}`}
      </div>
    </div>
  )
}

// ── 柱状图（纯 SVG）────────────────────────────────────────────────────────
const BarChart: React.FC<{
  data: { label: string; value: number; color?: string }[]
  height?: number
  valueFormatter?: (value: number) => string
}> = ({ data, height = 80, valueFormatter = (value) => String(value) }) => {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  if (!data.length) return null
  const max = Math.max(...data.map(d => d.value), 1)
  const mid = max / 2
  const hoverItem = hoverIndex !== null ? data[hoverIndex] : null
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'stretch', gap: 6, height }}>
        <div style={{ width: 34, display: 'flex', flexDirection: 'column', justifyContent: 'space-between', fontSize: 9, color: 'var(--mb-text-faint)', textAlign: 'right', paddingTop: 2, paddingBottom: 18 }}>
          <span>{valueFormatter(max)}</span>
          <span>{valueFormatter(mid)}</span>
          <span>{valueFormatter(0)}</span>
        </div>
        <div style={{ flex: 1, display: 'flex', alignItems: 'flex-end', gap: 4, height }}>
          {data.map((d, i) => (
            <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
              <div
                role="img"
                tabIndex={0}
                aria-label={`${d.label || '当前分组'}：${valueFormatter(d.value)}`}
                style={{
                  width: '100%', borderRadius: '3px 3px 0 0',
                  height: Math.max((d.value / max) * (height - 20), 2),
                  background: d.color || '#007AFF',
                  opacity: hoverIndex === i ? 1 : 0.85,
                  outlineOffset: 2,
                }}
                title={`${d.label}: ${valueFormatter(d.value)}`}
                onMouseEnter={() => setHoverIndex(i)}
                onMouseLeave={() => setHoverIndex(null)}
                onFocus={() => setHoverIndex(i)}
                onBlur={() => setHoverIndex(null)}
              />
              <span style={{ fontSize: 9, color: 'var(--mb-text-faint)', textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', width: '100%' }}>
                {d.label}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginTop: 6, minHeight: 16 }}>
        {hoverItem ? `${hoverItem.label || '当前'} · ${valueFormatter(hoverItem.value)}` : '悬停柱子可查看具体值'}
      </div>
    </div>
  )
}

const TokenMetricDistributionPanel: React.FC<{
  title: string
  accent: string
  metric: MonitorOverview['token_usage']['bake_distribution']['input']
}> = ({ title, accent, metric }) => (
  <section className="monitor-token-distribution__metric" aria-label={`${title} token 分布`}>
    <div className="monitor-token-distribution__metric-head">
      <span>{title}</span>
      <span>P50 {fmt(metric.p50)} · P95 {fmt(metric.p95)} · P99 {fmt(metric.p99)}</span>
    </div>
    <BarChart
      data={metric.buckets.map((bucket) => ({
        label: bucket.label,
        value: bucket.count,
        color: accent,
      }))}
      height={104}
      valueFormatter={(value) => `${Math.round(value)} 次`}
    />
    <div className="monitor-token-distribution__max">最大值 {fmt(metric.max)} tokens</div>
  </section>
)

const BakeTokenDistributionCard: React.FC<{
  distribution: MonitorOverview['token_usage']['bake_distribution']
  rangeLabel: string
}> = ({ distribution, rangeLabel }) => (
  <div style={cardStyle}>
    <div className="monitor-token-distribution__head">
      <div>
        <span style={{ ...sectionTitle, marginBottom: 3 }}>烘焙提炼 Token 分布（{rangeLabel}）</span>
        <div className="monitor-token-distribution__summary">
          有效样本 {distribution.sample_count} 次 · 输入 ≥20K {distribution.input_over_20k_count} 次
          {distribution.truncated_count > 0 ? ` · 长度截断 ${distribution.truncated_count} 次` : ''}
        </div>
      </div>
      <div className="monitor-token-distribution__legend" aria-label="Token 分布图例">
        <span><i style={{ background: '#FF9500' }} />输入</span>
        <span><i style={{ background: '#32ADE6' }} />输出</span>
      </div>
    </div>
    {distribution.sample_count > 0 ? (
      <div className="monitor-token-distribution__grid">
        <TokenMetricDistributionPanel title="输入 Prompt" accent="#FF9500" metric={distribution.input} />
        <TokenMetricDistributionPanel title="模型输出" accent="#32ADE6" metric={distribution.output} />
      </div>
    ) : (
      <div className="monitor-token-distribution__empty">当前时间范围内暂无烘焙提炼样本</div>
    )}
  </div>
)

const TrendHeader: React.FC<{
  title: string
  rangeLabel: string
  bucketLabel: string
}> = ({ title, rangeLabel, bucketLabel }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
    <span style={{ ...sectionTitle, marginBottom: 0 }}>{title}（{rangeLabel}）</span>
    <span style={{ fontSize: 11, color: 'var(--mb-text-faint)' }}>采样粒度 {bucketLabel}</span>
  </div>
)

const ChartLegend: React.FC<{
  items: { label: string; color: string }[]
}> = ({ items }) => {
  if (!items.length) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
      {items.map((item, index) => (
        <span key={`${index}-${item.color}-${item.label}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: item.color, display: 'inline-block' }} />
          {item.label}
        </span>
      ))}
    </div>
  )
}

// ── 统计卡片 ────────────────────────────────────────────────────────────────
const StatCard: React.FC<{
  label: string; value: string; sub?: string; color: string
}> = ({ label, value, sub, color }) => (
  <div
    className="monitor-stat-card"
    style={{ background: `${color}10`, borderColor: `${color}20` }}
  >
    <div className="monitor-stat-card__label" title={label}>{label}</div>
    <div className="monitor-stat-card__value" style={fontSize_20_weight_700(color)}>{value}</div>
    {sub && <div className="monitor-stat-card__sub" title={sub}>{sub}</div>}
  </div>
)

function fontSize_20_weight_700(color: string): React.CSSProperties {
  return { fontSize: 20, fontWeight: 700, color, lineHeight: 1 }
}

type ExtractorStatus = 'running' | 'waiting' | 'idle' | 'stalled' | 'paused'

const EXTRACTOR_STATUS_META: Record<ExtractorStatus, { label: string; color: string; dot: string }> = {
  running: { label: '运行中', color: '#34C759', dot: '#34C759' },
  waiting: { label: '等待片段成熟', color: '#FF9500', dot: '#FF9500' },
  idle:    { label: '空闲', color: 'var(--mb-text-tertiary)', dot: '#8E8E93' },
  stalled: { label: '提炼器无响应', color: '#FF3B30', dot: '#FF3B30' },
  paused:  { label: '已暂停', color: 'var(--mb-text-tertiary)', dot: '#8E8E93' },
}

function fmtRelativeTs(ms: number | null | undefined): string {
  if (!ms) return '尚无记录'
  const diff = Date.now() - ms
  if (diff < 0) return '刚刚'
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s} 秒前`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

function fmtDurationMs(ms: number): string {
  const value = Math.max(0, ms)
  if (value < 60_000) return `${Math.floor(value / 1000)} 秒`
  if (value < 3_600_000) return `${Math.floor(value / 60_000)} 分钟`
  if (value < 86_400_000) {
    const hours = Math.floor(value / 3_600_000)
    const minutes = Math.floor((value % 3_600_000) / 60_000)
    return `${hours} 小时${minutes > 0 ? ` ${minutes} 分钟` : ''}`
  }
  const days = Math.floor(value / 86_400_000)
  const hours = Math.floor((value % 86_400_000) / 3_600_000)
  return `${days} 天${hours > 0 ? ` ${hours} 小时` : ''}`
}

function oldestQueueAge(oldestAtMs: number | null | undefined): number {
  return oldestAtMs ? Math.max(0, Date.now() - oldestAtMs) : 0
}

const ExtractionQueueCard: React.FC<{
  pending: number
  status: ExtractorStatus
  extractingCount: number
  lastExtractionAtMs: number | null | undefined
  oldestPendingAtMs: number | null | undefined
  stalled: boolean
}> = ({ pending, status, extractingCount, lastExtractionAtMs, oldestPendingAtMs, stalled }) => {
  const meta = EXTRACTOR_STATUS_META[status] ?? EXTRACTOR_STATUS_META.idle
  const oldestAge = oldestQueueAge(oldestPendingAtMs)
  const baseColor = status === 'paused'
    ? '#8E8E93'
    : oldestAge >= 86_400_000
    ? '#FF3B30'
    : stalled || pending > 0 ? '#FF9500' : '#8E8E93'

  let sub: string
  if (status === 'running') {
    sub = extractingCount > 0
      ? `正在提炼 ${extractingCount} 条 · 上次 ${fmtRelativeTs(lastExtractionAtMs)}`
      : `刚刚完成 · 上次 ${fmtRelativeTs(lastExtractionAtMs)}`
  } else if (status === 'waiting') {
    sub = '等待片段积累足够（≥3 条且静默 ≥10 分钟）后批量提炼'
  } else if (status === 'stalled') {
    sub = '后台提炼未启动，请检查本机服务'
  } else if (stalled) {
    sub = pending > 0
      ? `提炼停摆：最老已等待 ${fmtDurationMs(oldestAge)}，请检查本机服务`
      : '提炼停摆，请检查本机服务'
  } else if (status === 'paused') {
    sub = pending > 0
      ? `自动提炼已暂停 · 保留 ${pending} 条待恢复处理`
      : '自动提炼已暂停'
  } else {
    sub = pending > 0
      ? `最老已等待 ${fmtDurationMs(oldestAge)}`
      : '当前无待提炼内容'
  }

  return (
    <div
      className="monitor-stat-card"
      title="全量库存：已采集但尚未整理成时间线的内容，不受页面趋势时间范围影响。"
      style={{ background: `${baseColor}10`, borderColor: `${baseColor}20` }}
    >
      <div className="monitor-stat-card__header">
        <span className="monitor-stat-card__label" title="时间线等待队列">时间线等待队列</span>
        <span
          className="monitor-stat-card__status"
          style={{ background: `${meta.color}18`, color: meta.color }}
        >
          <span style={{
            width: 6, height: 6, borderRadius: '50%', background: meta.dot,
            animation: status === 'running' ? 'pulse 1.4s ease-in-out infinite' : undefined,
          }} />
          {meta.label}
        </span>
      </div>
      <div className="monitor-stat-card__value" style={fontSize_20_weight_700(baseColor)}>{fmt(pending)}</div>
      <div className="monitor-stat-card__sub" title={sub}>{sub}</div>
    </div>
  )
}

export const BakeQueueCard: React.FC<{
  pending: number
  oldestPendingAtMs: number | null | undefined
  runningCount: number
  staleRunCount: number
  retryExhaustedCount: number
  freshPendingCount: number
  operationReplayCount: number
  retryReadyCount: number
  retryDelayedCount: number
  noProgressCount: number
  nextRetryAtMs: number | null | undefined
  stalled: boolean
  paused: boolean
  drainRatePerHour: number
  etaMs: number | null | undefined
}> = ({
  pending,
  oldestPendingAtMs,
  runningCount,
  staleRunCount,
  retryExhaustedCount,
  freshPendingCount,
  operationReplayCount,
  retryReadyCount,
  retryDelayedCount,
  noProgressCount,
  nextRetryAtMs,
  stalled,
  paused,
  drainRatePerHour,
  etaMs,
}) => {
  const oldestAge = oldestQueueAge(oldestPendingAtMs)
  const color = paused
    ? '#8E8E93'
    : staleRunCount > 0 || stalled
    ? '#FF3B30'
    : pending > 0 ? '#FF9500' : '#8E8E93'
  const drainParts = [
    drainRatePerHour > 0 ? `消化 ${fmt(Math.round(drainRatePerHour))}/h` : '',
    etaMs ? `预计清空 ${fmtDurationMs(etaMs)}` : '',
  ]
  const sub = paused
    ? `自动烘焙已暂停 · 保留 ${pending} 条待恢复处理`
    : pending > 0
    ? [
        `最老已等待 ${fmtDurationMs(oldestAge)}`,
        `运行 ${runningCount}`,
        ...drainParts,
      ].join(' · ')
    : `当前无待烘焙内容 · 运行 ${runningCount}`
  const retryParts = [
    freshPendingCount > 0 ? `新任务 ${freshPendingCount}` : '',
    operationReplayCount > 0 ? `历史回放 ${operationReplayCount}` : '',
    retryReadyCount > 0 ? `可重试 ${retryReadyCount}` : '',
    retryDelayedCount > 0 ? `退避中 ${retryDelayedCount}` : '',
    retryExhaustedCount > 0 ? `需处理 ${retryExhaustedCount}` : '',
    noProgressCount > 0 ? `近 5 批空跑 ${noProgressCount}` : '',
    nextRetryAtMs && retryDelayedCount > 0 ? `下次 ${fmtTs(nextRetryAtMs)}` : '',
  ].filter(Boolean)
  const detail = retryParts.length > 0 ? `${sub} · ${retryParts.join(' · ')}` : sub
  return (
    <div
      className="monitor-stat-card"
      title="当前库存按新任务、历史回放和重试分层展示；终态失败不计入等待总数。"
      style={{ background: `${color}10`, borderColor: `${color}20` }}
    >
      <div className="monitor-stat-card__label">烘焙等待队列</div>
      <div className="monitor-stat-card__value" style={fontSize_20_weight_700(color)}>{fmt(pending)}</div>
      <div className="monitor-stat-card__sub" title={detail}>{detail}</div>
    </div>
  )
}

const InferenceQueueCard: React.FC<{
  queue: ExtractionLive['inference_queue'] | undefined
}> = ({ queue }) => {
  if (!queue?.available) {
    return <StatCard label="模型推理队列" value="—" sub="队列状态接口不可用" color="#8E8E93" />
  }
  const color = queue.oldest_wait_ms >= 600_000
    ? '#FF3B30'
    : queue.queued_total > 0 ? '#FF9500' : '#34C759'
  const sub = `运行 ${queue.running_total}/${queue.max_concurrency}（烘焙 ${queue.running_p2 ?? 0}） · P0 ${queue.queued_p0} / P1 ${queue.queued_p1} / P2 ${queue.queued_p2}`
  return (
    <StatCard
      label="模型推理队列"
      value={fmt(queue.queued_total)}
      sub={queue.oldest_wait_ms > 0 ? `${sub} · 最老 ${fmtDurationMs(queue.oldest_wait_ms)}` : sub}
      color={color}
    />
  )
}

export const PipelineBacklogAlert: React.FC<{
  capturePending: number
  captureOldestAtMs: number | null | undefined
  bakePending: number
  bakeOldestAtMs: number | null | undefined
  staleBakeRuns: number
  bakeStalled: boolean
  recentBakeFailures: number
  recentBakeDeferred: number
  recentBakeRuns: number
  recentNoProgress: number
  schedulerMismatch: boolean
  captureEnabled: boolean
  inferenceQueue: ExtractionLive['inference_queue'] | undefined
  enablingCapture?: boolean
  enableCaptureError?: string | null
  onEnableCapture?: () => void
}> = ({
  capturePending,
  captureOldestAtMs,
  bakePending,
  bakeOldestAtMs,
  staleBakeRuns,
  bakeStalled,
  recentBakeFailures,
  recentBakeDeferred,
  recentBakeRuns,
  recentNoProgress,
  schedulerMismatch,
  captureEnabled,
  inferenceQueue,
  enablingCapture = false,
  enableCaptureError = null,
  onEnableCapture,
}) => {
  if (!captureEnabled) {
    return (
      <div className="monitor-capture-paused" role="status">
        <div className="monitor-capture-paused__copy">
          <strong>自动采集与提炼已暂停</strong>
          <span>
            时间线队列 {capturePending} 条、烘焙队列 {bakePending} 条将在恢复后继续处理，暂停时长不计为流水线故障。
          </span>
          <small>开启后，记忆面包会在本机采集允许的工作内容并自动整理；你可以随时从菜单栏再次暂停。</small>
          {enableCaptureError && <span className="monitor-capture-paused__error" role="alert">{enableCaptureError}</span>}
        </div>
        {onEnableCapture && (
          <button type="button" disabled={enablingCapture} onClick={onEnableCapture}>
            {enablingCapture ? '正在开启…' : '开启自动采集与提炼'}
          </button>
        )}
      </div>
    )
  }
  const issues: string[] = []
  const captureAge = oldestQueueAge(captureOldestAtMs)
  const bakeAge = oldestQueueAge(bakeOldestAtMs)
  if (capturePending > 0 && captureAge >= 7_200_000) {
    issues.push(`时间线队列 ${capturePending} 条，最老等待 ${fmtDurationMs(captureAge)}`)
  }
  if (staleBakeRuns > 0) issues.push(`${staleBakeRuns} 个烘焙批次运行超过 35 分钟`)
  if (bakeStalled) {
    issues.push(`烘焙队列 ${bakePending} 条，最老等待 ${fmtDurationMs(bakeAge)}，水位超过 15 分钟未推进`)
  }
  if (bakeStalled && recentBakeRuns >= 3 && recentBakeFailures + recentBakeDeferred >= 3) {
    issues.push(`最近 ${recentBakeRuns} 批中失败 ${recentBakeFailures}、延后 ${recentBakeDeferred}`)
  }
  if (schedulerMismatch) {
    issues.push(`队列仍有可执行候选，但最近 5 批有 ${recentNoProgress} 批未取得进展`)
  }
  if (inferenceQueue?.available && inferenceQueue.oldest_wait_ms >= 600_000) {
    issues.push(`模型推理队列最老等待 ${fmtDurationMs(inferenceQueue.oldest_wait_ms)}`)
  }
  if (issues.length === 0) return null
  const critical = captureAge >= 86_400_000
    || (bakeStalled && bakeAge >= 86_400_000)
    || staleBakeRuns > 0
    || (inferenceQueue?.oldest_wait_ms ?? 0) >= 1_800_000
  const color = critical ? '#FF3B30' : '#FF9500'
  return (
    <div style={{
      marginBottom: 10,
      padding: '10px 12px',
      borderRadius: 10,
      background: `${color}10`,
      border: `1px solid ${color}30`,
      color,
      fontSize: 12,
      fontWeight: 600,
    }}>
      流水线积压告警：{issues.join('；')}
    </div>
  )
}

const BakeFailureAlert: React.FC<{
  retryPending: number
  exhausted: number
  timeoutFailures: number
  truncatedFailures: number
  upstreamFailures: number
  otherFailures: number
}> = ({ retryPending, exhausted, timeoutFailures, truncatedFailures, upstreamFailures, otherFailures }) => {
  if (retryPending === 0 && exhausted === 0) return null
  const parts: string[] = []
  if (retryPending > 0) parts.push(`${retryPending} 条正在退避重试`)
  if (exhausted > 0) parts.push(`${exhausted} 条达到最大尝试次数`)
  const reasons = [
    timeoutFailures > 0 ? `超时 ${timeoutFailures}` : '',
    truncatedFailures > 0 ? `输出异常 ${truncatedFailures}` : '',
    upstreamFailures > 0 ? `上游 5xx ${upstreamFailures}` : '',
    otherFailures > 0 ? `其他 ${otherFailures}` : '',
  ].filter(Boolean)
  if (reasons.length > 0) parts.push(`错误分布：${reasons.join('、')}`)
  const color = exhausted > 0 ? '#FF3B30' : '#FF9500'
  return (
    <div style={{
      marginBottom: 10,
      padding: '10px 12px',
      borderRadius: 10,
      background: `${color}10`,
      border: `1px solid ${color}30`,
      color,
      fontSize: 12,
      fontWeight: 600,
    }}>
      候选处理状态：{parts.join('；')}。
    </div>
  )
}

const OcrBackfillCard: React.FC<{
  metrics: MonitorOverview['ocr_backfill']
  rangeLabel: string
}> = ({ metrics, rangeLabel }) => {
  const totalProblem = metrics.period_failed
    + metrics.period_timed_out
    + metrics.period_empty
    + metrics.period_skipped_offline
    + metrics.period_skipped_backpressure
  const statusColor = metrics.backlog_count > 10
    ? '#FF9500'
    : totalProblem > 0
      ? '#FF9500'
      : '#34C759'

  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', alignItems: 'flex-start', marginBottom: 10 }}>
        <div>
          <div style={{ ...sectionTitle, marginBottom: 3 }}>后台文字识别</div>
          <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>{rangeLabel} · 仅在无法读取正文时使用截图识别，并在后台逐条补全文本</div>
        </div>
        <div style={{
          fontSize: 11, color: statusColor, background: `${statusColor}14`,
          border: `1px solid ${statusColor}24`, borderRadius: 6, padding: '3px 8px', fontWeight: 600,
        }}>
          积压 {metrics.backlog_count}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(108px, 1fr))', gap: 8, marginBottom: 10 }}>
        <div style={{ background: 'rgba(0,122,255,0.08)', borderRadius: 8, padding: '8px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>吞吐</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#007AFF', marginTop: 2 }}>{fmtRatePerMin(metrics.period_throughput_per_min)}</div>
          <div style={{ fontSize: 10, color: 'var(--mb-text-tertiary)', marginTop: 2 }}>完成 {fmt(metrics.period_completed)}</div>
        </div>
        <div style={{ background: 'rgba(52,199,89,0.08)', borderRadius: 8, padding: '8px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>成功率</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#34C759', marginTop: 2 }}>{metrics.period_success_rate.toFixed(0)}%</div>
          <div style={{ fontSize: 10, color: 'var(--mb-text-tertiary)', marginTop: 2 }}>成功 {fmt(metrics.period_succeeded)}</div>
        </div>
        <div style={{ background: 'rgba(255,149,0,0.08)', borderRadius: 8, padding: '8px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>队列</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#FF9500', marginTop: 2 }}>{fmt(metrics.queued_count)}</div>
          <div style={{ fontSize: 10, color: 'var(--mb-text-tertiary)', marginTop: 2 }}>执行中 {fmt(metrics.in_progress_count)}</div>
        </div>
        <div style={{ background: 'rgba(94,92,230,0.08)', borderRadius: 8, padding: '8px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>平均耗时</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#5E5CE6', marginTop: 2 }}>{fmtMs(metrics.avg_latency_ms)}</div>
          <div style={{ fontSize: 10, color: 'var(--mb-text-tertiary)', marginTop: 2 }}>上次 {fmtRelativeTs(metrics.last_completed_at_ms)}</div>
        </div>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: metrics.recent.length > 0 ? 10 : 0 }}>
        {[
          { label: '失败', value: metrics.period_failed, color: '#FF3B30' },
          { label: '超时', value: metrics.period_timed_out, color: '#FF9500' },
          { label: '空文本', value: metrics.period_empty, color: 'var(--mb-text-tertiary)' },
          { label: '离线跳过', value: metrics.period_skipped_offline, color: 'var(--mb-text-secondary)' },
          { label: '队列限流', value: metrics.period_skipped_backpressure, color: '#FF9500' },
          { label: '累计完成', value: metrics.completed_total, color: '#007AFF' },
        ].map((item) => (
          <span key={item.label} style={{
            fontSize: 11, color: item.color, background: `${item.color}10`,
            borderRadius: 999, padding: '2px 8px',
          }}>{item.label} {fmt(item.value)}</span>
        ))}
      </div>

      {metrics.recent.length > 0 && (
        <div>
          <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginBottom: 5 }}>最近文字识别</div>
          {metrics.recent.map((item, index) => {
            const meta = OCR_STATUS_META[item.status] ?? { label: item.status, color: 'var(--mb-text-secondary)' }
            return (
              <div key={`${item.ts}-${index}`} style={{
                display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center',
                padding: '5px 0', borderTop: index === 0 ? '1px solid rgba(0,0,0,0.05)' : 'none',
              }}>
                <span style={{ fontSize: 11, color: 'var(--mb-text-tertiary)' }}>{fmtTs(item.ts)}</span>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, color: meta.color }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: meta.color }} />
                  {meta.label} · {fmtMs(item.latency_ms)}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

const RuntimeBreakdownCard: React.FC<{
  items: SystemResources['model_runtime_breakdown']
}> = ({ items }) => {
  const visible = items.slice(0, 6)
  return (
    <div style={cardStyle}>
      <div style={sectionTitle}>模型运行时拆分</div>
      {visible.length === 0 ? (
        <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>暂无拆分数据</div>
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          {visible.map((item, index) => {
            const color = getStableSeriesColor(item.key, index)
            return (
              <div key={item.key} style={{
                border: `1px solid ${color}20`,
                background: `${color}10`,
                borderRadius: 10,
                padding: '10px 12px',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--mb-text-primary)' }}>{item.label}</div>
                    <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginTop: 3 }}>
                      {formatCoverageText(item.coverage_note, item.coverage_status)}
                    </div>
                  </div>
                  <div style={{ textAlign: 'right', flexShrink: 0 }}>
                    <div style={{ fontSize: 15, fontWeight: 700, color }}>{item.mem_process_mb.toLocaleString()} MB</div>
                    <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginTop: 3 }}>
                      CPU {item.cpu_percent.toFixed(1)}% · {item.process_count} 个进程
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── 总览内容 ─────────────────────────────────────────────────────────────────
const OverviewContent: React.FC<{
  data: MonitorOverview
  range: OverviewRange
  liveData: ExtractionLive | null
  nowMs: number
  enablingCapture: boolean
  enableCaptureError: string | null
  onEnableCapture: () => void
  captureHealth: CaptureHealth | null
}> = ({ data, range, liveData, nowMs, enablingCapture, enableCaptureError, onEnableCapture, captureHealth }) => {
  const token_usage = {
    ...EMPTY_OVERVIEW.token_usage,
    ...(data?.token_usage ?? {}),
    by_model: data?.token_usage?.by_model ?? [],
    by_caller: data?.token_usage?.by_caller ?? [],
    trend: data?.token_usage?.trend ?? [],
    trend_by_model: data?.token_usage?.trend_by_model ?? [],
    bake_distribution: {
      ...EMPTY_OVERVIEW.token_usage.bake_distribution,
      ...(data?.token_usage?.bake_distribution ?? {}),
      input: {
        ...EMPTY_OVERVIEW.token_usage.bake_distribution.input,
        ...(data?.token_usage?.bake_distribution?.input ?? {}),
        buckets: data?.token_usage?.bake_distribution?.input?.buckets ?? [],
      },
      output: {
        ...EMPTY_OVERVIEW.token_usage.bake_distribution.output,
        ...(data?.token_usage?.bake_distribution?.output ?? {}),
        buckets: data?.token_usage?.bake_distribution?.output?.buckets ?? [],
      },
    },
  }
  const capture_flow = {
    ...EMPTY_OVERVIEW.capture_flow,
    ...(data?.capture_flow ?? {}),
    by_hour: data?.capture_flow?.by_hour ?? [],
    by_app: data?.capture_flow?.by_app ?? [],
    recent: data?.capture_flow?.recent ?? [],
  }
  const ocr_backfill = {
    ...EMPTY_OVERVIEW.ocr_backfill,
    ...(data?.ocr_backfill ?? {}),
    recent: data?.ocr_backfill?.recent ?? [],
  }
  const rag_sessions = {
    ...EMPTY_OVERVIEW.rag_sessions,
    ...(data?.rag_sessions ?? {}),
    recent: data?.rag_sessions?.recent ?? [],
  }
  // 实时状态采用 3s 轮询的 liveData；范围内聚合和最近记录沿用 overview，保持筛选口径一致。
  const knowledge_flow = {
    ...EMPTY_OVERVIEW.knowledge_flow,
    ...(data?.knowledge_flow ?? {}),
    by_time: data?.knowledge_flow?.by_time ?? [],
    recent: data?.knowledge_flow?.recent ?? liveData?.recent ?? [],
    extracting: liveData?.extracting ?? data?.knowledge_flow?.extracting ?? [],
    last_extraction_at_ms: liveData?.last_extraction_at_ms
      ?? data?.knowledge_flow?.last_extraction_at_ms
      ?? null,
    extractor_status: liveData?.extractor_status
      ?? data?.knowledge_flow?.extractor_status
      ?? 'idle',
    capture_enabled: liveData?.capture_enabled
      ?? data?.knowledge_flow?.capture_enabled
      ?? true,
    pending_extraction_count: liveData?.pending_extraction_count
      ?? data?.knowledge_flow?.pending_extraction_count
      ?? 0,
    oldest_pending_extraction_at_ms: liveData?.oldest_pending_extraction_at_ms
      ?? data?.knowledge_flow?.oldest_pending_extraction_at_ms
      ?? null,
    extraction_stalled: data?.knowledge_flow?.extraction_stalled ?? false,
    pending_bake_count: liveData?.pending_bake_count
      ?? data?.knowledge_flow?.pending_bake_count
      ?? 0,
    oldest_pending_bake_at_ms: liveData?.oldest_pending_bake_at_ms
      ?? data?.knowledge_flow?.oldest_pending_bake_at_ms
      ?? null,
    bake_retry_pending_count: liveData?.bake_retry_pending_count
      ?? data?.knowledge_flow?.bake_retry_pending_count
      ?? 0,
    bake_fresh_pending_count: liveData?.bake_fresh_pending_count
      ?? data?.knowledge_flow?.bake_fresh_pending_count
      ?? 0,
    bake_retry_ready_count: liveData?.bake_retry_ready_count
      ?? data?.knowledge_flow?.bake_retry_ready_count
      ?? 0,
    bake_retry_delayed_count: liveData?.bake_retry_delayed_count
      ?? data?.knowledge_flow?.bake_retry_delayed_count
      ?? 0,
    bake_actionable_count: liveData?.bake_actionable_count
      ?? data?.knowledge_flow?.bake_actionable_count
      ?? 0,
    bake_retry_exhausted_count: liveData?.bake_retry_exhausted_count
      ?? data?.knowledge_flow?.bake_retry_exhausted_count
      ?? 0,
    bake_timeout_failure_count: liveData?.bake_timeout_failure_count
      ?? data?.knowledge_flow?.bake_timeout_failure_count
      ?? 0,
    bake_truncated_failure_count: liveData?.bake_truncated_failure_count
      ?? data?.knowledge_flow?.bake_truncated_failure_count
      ?? 0,
    bake_other_failure_count: liveData?.bake_other_failure_count
      ?? data?.knowledge_flow?.bake_other_failure_count
      ?? 0,
    bake_upstream_failure_count: liveData?.bake_upstream_failure_count
      ?? data?.knowledge_flow?.bake_upstream_failure_count
      ?? 0,
    bake_recent_no_progress_count: liveData?.bake_recent_no_progress_count
      ?? data?.knowledge_flow?.bake_recent_no_progress_count
      ?? 0,
    bake_next_retry_at_ms: liveData?.bake_next_retry_at_ms
      ?? data?.knowledge_flow?.bake_next_retry_at_ms
      ?? null,
    bake_recommended_retry_after_ms: liveData?.bake_recommended_retry_after_ms
      ?? data?.knowledge_flow?.bake_recommended_retry_after_ms
      ?? 0,
    bake_scheduler_mismatch: liveData?.bake_scheduler_mismatch
      ?? data?.knowledge_flow?.bake_scheduler_mismatch
      ?? false,
    running_bake_count: liveData?.running_bake_count
      ?? data?.knowledge_flow?.running_bake_count
      ?? 0,
    stale_bake_run_count: liveData?.stale_bake_run_count
      ?? data?.knowledge_flow?.stale_bake_run_count
      ?? 0,
    bake_drain_rate_per_hour: liveData?.bake_drain_rate_per_hour
      ?? data?.knowledge_flow?.bake_drain_rate_per_hour
      ?? 0,
    bake_eta_ms: liveData?.bake_eta_ms
      ?? data?.knowledge_flow?.bake_eta_ms
      ?? null,
  }
  const task_executions = {
    ...EMPTY_OVERVIEW.task_executions,
    ...(data?.task_executions ?? {}),
    recent: data?.task_executions?.recent ?? [],
  }
  const operation_flow = {
    ...EMPTY_OVERVIEW.operation_flow,
    ...(data?.operation_flow ?? {}),
  }
  const serviceHealth = liveData?.service_health ?? data?.service_health ?? EMPTY_OVERVIEW.service_health
  const tokenTrendSeries = token_usage.trend_by_model.length > 0
    ? token_usage.trend_by_model.map((item, index) => ({
        label: formatLlmModelName(item.model),
        color: getStableSeriesColor(`token-${item.model}`, index),
        data: item.trend.map((point) => ({ ts: point.ts, value: point.tokens })),
        valueFormatter: (value: number) => `${fmt(value)} tokens`,
      }))
    : [{
        label: '总量',
        color: '#007AFF',
        data: token_usage.trend.map((point) => ({ ts: point.ts, value: point.tokens })),
        valueFormatter: (value: number) => `${fmt(value)} tokens`,
      }]
  const hasTokenTrend = tokenTrendSeries.some((item) => item.data.length > 0)
  const overviewRangeMs = overviewRangeToMs(range)
  const chartDomain = { start: nowMs - overviewRangeMs, end: nowMs }
  const rangeLabel = getOverviewRangeLabel(range)
  const bucketLabel = getOverviewBucketLabel(range)
  const captureBlackout = captureHealth?.content_blackout_ms ?? null
  const latestCaptureAttempt = captureHealth?.recent[0]
  const captureIsInactive = latestCaptureAttempt?.outcome === 'privacy_skipped'
    || (latestCaptureAttempt?.outcome === 'degraded'
      && ['system_idle', 'display_asleep'].includes(latestCaptureAttempt.reason))
  const captureBlackoutExceeded = Boolean(
    captureHealth?.capture_enabled
    && !captureIsInactive
    && captureBlackout != null
    && captureBlackout > captureHealth.max_active_blackout_ms,
  )
  const skippedAttemptCount = captureHealth?.outcome_counts
    .filter(item => item.outcome !== 'captured' && item.outcome !== 'continuity')
    .reduce((sum, item) => sum + item.count, 0) ?? 0
  return (
    <>
      <ServiceHealthBanner health={serviceHealth} />
      <PipelineBacklogAlert
        capturePending={knowledge_flow.pending_extraction_count}
        captureOldestAtMs={knowledge_flow.oldest_pending_extraction_at_ms}
        bakePending={knowledge_flow.pending_bake_count}
        bakeOldestAtMs={knowledge_flow.oldest_pending_bake_at_ms}
        staleBakeRuns={knowledge_flow.stale_bake_run_count}
        bakeStalled={liveData?.bake_stalled ?? false}
        recentBakeFailures={liveData?.recent_bake_failed_count ?? 0}
        recentBakeDeferred={liveData?.recent_bake_deferred_count ?? 0}
        recentBakeRuns={liveData?.recent_bake_run_count ?? 0}
        recentNoProgress={knowledge_flow.bake_recent_no_progress_count ?? 0}
        schedulerMismatch={knowledge_flow.bake_scheduler_mismatch ?? false}
        captureEnabled={knowledge_flow.capture_enabled}
        inferenceQueue={liveData?.inference_queue}
        enablingCapture={enablingCapture}
        enableCaptureError={enableCaptureError}
        onEnableCapture={onEnableCapture}
      />
      <BakeFailureAlert
        retryPending={knowledge_flow.bake_retry_pending_count}
        exhausted={knowledge_flow.bake_retry_exhausted_count}
        timeoutFailures={knowledge_flow.bake_timeout_failure_count}
        truncatedFailures={knowledge_flow.bake_truncated_failure_count}
        upstreamFailures={knowledge_flow.bake_upstream_failure_count ?? 0}
        otherFailures={knowledge_flow.bake_other_failure_count}
      />
      {operation_flow.zero_eligible_alert && (
        <div style={{
          ...cardStyle,
          borderColor: 'rgba(255, 59, 48, 0.35)',
          background: 'rgba(255, 59, 48, 0.08)',
          color: 'var(--mb-text-primary)',
          marginBottom: 14,
        }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#FF3B30' }}>操作记忆准入连续为 0</div>
          <div style={{ marginTop: 5, fontSize: 12, color: 'var(--mb-text-secondary)' }}>
            当前范围已审计 {fmt(operation_flow.period_audited_count)} 条候选，请检查动作证据、结果归因和 OCR 回填。
          </div>
        </div>
      )}
      <div className="monitor-metric-grid">
        <StatCard label="Token 用量" value={fmt(token_usage.total_period)}
          sub={`今日 ${fmt(token_usage.total_today)}`} color="#007AFF" />
        <StatCard label="采集记录" value={fmt(capture_flow.period_count)}
          sub={`可处理 ${fmt(capture_flow.eligible_count)} · 今日 ${capture_flow.today_count}`} color="#34C759" />
        <StatCard label="总采集数" value={fmt(data?.capture_total_count ?? 0)}
          sub={`数据库 ${fmtBytes(data?.db_size_bytes ?? 0)}`} color="#32ADE6" />
        <StatCard label="采集实际间隔" value={captureHealth ? `${captureHealth.effective_interval_secs}s` : '—'}
          sub={captureHealth
            ? `设置 ${captureHealth.configured_interval_secs}s${captureHealth.pressure_degraded ? ' · 压力降级中' : ''}`
            : '等待运行状态'} color={captureHealth?.pressure_degraded ? '#FF9500' : '#34C759'} />
        <StatCard label="最近有效关键帧" value={captureHealth?.last_captured_at_ms ? fmtTs(captureHealth.last_captured_at_ms) : '暂无'}
          sub={captureHealth?.last_continuity_at_ms ? `连续性 ${fmtTs(captureHealth.last_continuity_at_ms)}` : '暂无连续性复用'} color="#007AFF" />
        <StatCard label="当前内容空窗" value={captureBlackout == null ? '暂无数据' : fmtDurationMs(captureBlackout)}
          sub={`近 6 小时跳过/降级 ${fmt(skippedAttemptCount)} 次`} color={captureBlackoutExceeded ? '#FF3B30' : '#34C759'} />
        <StatCard label="知识提炼" value={fmt(knowledge_flow.period_count)}
          sub={`今日 ${fmt(knowledge_flow.today_count)}`} color="#BF5AF2" />
        <StatCard label="操作候选准入率" value={`${(operation_flow.eligible_rate * 100).toFixed(0)}%`}
          sub={`准入 ${fmt(operation_flow.eligible_count)}/${fmt(operation_flow.period_audited_count)}`} color="#30B0C7" />
        <StatCard label="操作证据待补齐" value={fmt(operation_flow.needs_enrichment_count)}
          sub={`已拒绝 ${fmt(operation_flow.rejected_count)}`} color="#FF9500" />
        <StatCard label="操作历史回放" value={fmt(operation_flow.pending_replay_count)}
          sub="占用既有烘焙配额，不追加模型调用" color="#5E5CE6" />
        <ExtractionQueueCard
          pending={knowledge_flow.pending_extraction_count}
          status={knowledge_flow.extractor_status}
          extractingCount={knowledge_flow.extracting.length}
          lastExtractionAtMs={knowledge_flow.last_extraction_at_ms}
          oldestPendingAtMs={knowledge_flow.oldest_pending_extraction_at_ms}
          stalled={knowledge_flow.extraction_stalled}
        />
        <BakeQueueCard
          pending={knowledge_flow.pending_bake_count}
          oldestPendingAtMs={knowledge_flow.oldest_pending_bake_at_ms}
          runningCount={knowledge_flow.running_bake_count}
          staleRunCount={knowledge_flow.stale_bake_run_count}
          retryExhaustedCount={knowledge_flow.bake_retry_exhausted_count}
          freshPendingCount={knowledge_flow.bake_fresh_pending_count ?? 0}
          operationReplayCount={operation_flow.pending_replay_count ?? 0}
          retryReadyCount={knowledge_flow.bake_retry_ready_count ?? 0}
          retryDelayedCount={knowledge_flow.bake_retry_delayed_count ?? 0}
          noProgressCount={knowledge_flow.bake_recent_no_progress_count ?? 0}
          nextRetryAtMs={knowledge_flow.bake_next_retry_at_ms}
          stalled={liveData?.bake_stalled ?? false}
          paused={!knowledge_flow.capture_enabled}
          drainRatePerHour={knowledge_flow.bake_drain_rate_per_hour}
          etaMs={knowledge_flow.bake_eta_ms}
        />
        <InferenceQueueCard queue={liveData?.inference_queue} />
        <StatCard label="待识别截图" value={fmt(ocr_backfill.backlog_count)}
          sub={`队列 ${fmt(ocr_backfill.queued_count)} · 执行中 ${fmt(ocr_backfill.in_progress_count)}`} color="#FF9500" />
        <StatCard label="文字识别成功率" value={`${ocr_backfill.period_success_rate.toFixed(0)}%`}
          sub={`吞吐 ${fmtRatePerMin(ocr_backfill.period_throughput_per_min)} · 超时 ${fmt(ocr_backfill.period_timed_out)}`} color="#007AFF" />
        <StatCard label="向量化率" value={`${(capture_flow.vectorization_rate * 100).toFixed(0)}%`}
          sub={`已入索引 ${fmt(capture_flow.vectorized_count)}/${fmt(capture_flow.eligible_count)}`} color="#5E5CE6" />
        <StatCard label="知识化率" value={`${(capture_flow.knowledge_generation_rate * 100).toFixed(0)}%`}
          sub={`已生成知识 ${fmt(capture_flow.knowledge_generated_count)}`} color="#AF52DE" />
        <StatCard label="知识挂载率" value={`${(capture_flow.knowledge_rate * 100).toFixed(0)}%`}
          sub={`已关联采集内容 ${fmt(capture_flow.knowledge_linked_count)}`} color="#FF9500" />
      </div>

      <div style={cardStyle}>
        <TrendHeader title="Token 用量趋势" rangeLabel={rangeLabel} bucketLabel={bucketLabel} />
        {hasTokenTrend ? (
          <>
            <ChartLegend items={tokenTrendSeries.filter((item) => item.data.length > 0).map((item) => ({
              label: item.label,
              color: item.color,
            }))} />
            <SparkLine
              series={tokenTrendSeries}
              height={50}
              valueFormatter={(value) => `${fmt(value)} tokens`}
              axisFormatter={(ts) => fmtOverviewAxisTs(ts, range)}
              xDomain={chartDomain}
              bucketMs={getNiceBucketMs(overviewRangeMs)}
              fillMissingWithZero
              detailFormatter={(point) => {
                const item = token_usage.trend.find((entry) => Math.abs(entry.ts - point.ts) <= getNiceBucketMs(overviewRangeMs) / 2)
                return item
                  ? `${fmtTs(item.ts)} · ${fmt(item.tokens)} tokens · ${item.calls} 次`
                  : `${fmtTs(point.ts)} · ${fmt(point.value)} tokens · ${point.value > 0 ? '调用数未记录' : '0 次'}`
              }}
            />
            {tokenTrendSeries.filter((item) => item.data.length > 0).every((item) => item.data.length === 1) && (
              <div style={{ color: 'var(--mb-text-faint)', fontSize: 11, marginTop: 6 }}>当前时间范围内仅 1 个统计点。</div>
            )}
          </>
        ) : <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>暂无趋势数据</div>}
      </div>

      <BakeTokenDistributionCard
        distribution={token_usage.bake_distribution}
        rangeLabel={rangeLabel}
      />

      <div style={cardStyle}>
        <TrendHeader title="知识提炼趋势" rangeLabel={rangeLabel} bucketLabel={bucketLabel} />
        {knowledge_flow.by_time.length > 0 ? (
          <>
            <ChartLegend items={[
              { label: '已提炼', color: '#BF5AF2' },
            ]} />
            <SparkLine
              series={[
                {
                  label: '已提炼',
                  color: '#BF5AF2',
                  data: knowledge_flow.by_time.map((t) => ({ ts: t.ts, value: t.count })),
                  valueFormatter: (value) => `${value} 条`,
                },
              ]}
              height={50}
              axisFormatter={(ts) => fmtOverviewAxisTs(ts, range)}
              xDomain={chartDomain}
              bucketMs={getNiceBucketMs(overviewRangeMs)}
              fillMissingWithZero
              detailFormatter={(point) => `${fmtTs(point.ts)} · ${point.value} 条知识`}
            />
            {knowledge_flow.by_time.length === 1 && (
              <div style={{ color: 'var(--mb-text-faint)', fontSize: 11, marginTop: 6 }}>当前时间范围内仅 1 个统计点。</div>
            )}
            <div style={{ color: 'var(--mb-text-tertiary)', fontSize: 11, marginTop: 6 }}>
              当前库存：时间线 {fmt(knowledge_flow.pending_extraction_count)} 条 · 烘焙 {fmt(knowledge_flow.pending_bake_count)} 条
            </div>
          </>
        ) : <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>暂无知识趋势数据</div>}
      </div>

      <OcrBackfillCard metrics={ocr_backfill} rangeLabel={rangeLabel} />

      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <div style={{ ...cardStyle, flex: 1 }}>
          <div style={sectionTitle}>模型用量</div>
          {token_usage.by_model.length === 0
            ? <div style={{ color: 'var(--mb-text-faint)', fontSize: 12 }}>暂无数据</div>
            : token_usage.by_model.map((m, i) => {
              const color = getStableSeriesColor(`token-${m.model}`, i)
              const pct = token_usage.total_period > 0 ? (m.total / token_usage.total_period * 100).toFixed(0) : '0'
              return (
                <div key={i} style={{ marginBottom: 8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 3 }}>
                    <span style={{ color, fontWeight: 500 }}>{formatLlmModelName(m.model)}</span>
                    <span style={{ color: 'var(--mb-text-secondary)' }}>{fmt(m.total)} · {m.calls}次 ({pct}%)</span>
                  </div>
                  <div style={{ color: 'var(--mb-text-tertiary)', fontSize: 10, marginBottom: 4 }}>
                    Prompt {fmt(m.prompt)} · Completion {fmt(m.completion)}
                  </div>
                  <div style={{ height: 4, borderRadius: 2, background: 'var(--mb-border-strong)' }}>
                    <div style={{ height: '100%', borderRadius: 2, background: color, width: `${pct}%` }} />
                  </div>
                </div>
              )
            })}
          {token_usage.by_model.some((m) => m.model === 'unavailable') && (
            <div style={{ color: 'var(--mb-text-faint)', fontSize: 10, lineHeight: 1.4 }}>
              “模型不可用/未记录”表示调用失败或记录时未拿到具体模型名。
            </div>
          )}
        </div>
        <div style={{ ...cardStyle, flex: 1 }}>
          <div style={sectionTitle}>按来源分布</div>
          {token_usage.by_caller.length === 0
            ? <div style={{ color: 'var(--mb-text-faint)', fontSize: 12 }}>暂无数据</div>
            : token_usage.by_caller.map((c, i) => {
              const color = CALLER_COLORS[c.caller] || '#6E6E73'
              const pct = token_usage.total_period > 0 ? (c.total / token_usage.total_period * 100).toFixed(0) : '0'
              return (
                <div key={i} style={{ marginBottom: 8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 3 }}>
                    <span style={{ color, fontWeight: 500 }}>{CALLER_LABELS[c.caller] || c.caller}</span>
                    <span style={{ color: 'var(--mb-text-secondary)' }}>{fmt(c.total)} · {c.calls}次</span>
                  </div>
                  <div style={{ height: 4, borderRadius: 2, background: 'var(--mb-border-strong)' }}>
                    <div style={{ height: '100%', borderRadius: 2, background: color, width: `${pct}%` }} />
                  </div>
                </div>
              )
            })}
        </div>
      </div>

      <div style={cardStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
          <span style={sectionTitle as any}>采集流水（今日按小时）</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, justifyContent: 'flex-end' }}>
            <span style={{ fontSize: 11, color: '#5E5CE6' }}>向量化率 {(capture_flow.vectorization_rate * 100).toFixed(0)}%</span>
            <span style={{ fontSize: 11, color: '#AF52DE' }}>知识化率 {(capture_flow.knowledge_generation_rate * 100).toFixed(0)}%</span>
            <span style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>知识挂载率 {(capture_flow.knowledge_rate * 100).toFixed(0)}%</span>
          </div>
        </div>
        {capture_flow.by_hour.length > 0
          ? <BarChart data={Array.from({ length: 24 }, (_, h) => ({
              label: h % 4 === 0 ? String(h) : '',
              value: capture_flow.by_hour.find(b => b.hour === h)?.count || 0,
              color: '#34C759',
            }))} height={70} valueFormatter={(value) => `${value} 条`} />
          : <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>今日暂无采集数据</div>}
        {capture_flow.by_app.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginBottom: 6 }}>应用分布（Top 8）</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {capture_flow.by_app.map((a, i) => (
                <div key={i} style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10,
                  background: 'rgba(52,199,89,0.1)', color: '#34C759' }}>{a.app} {a.count}</div>
              ))}
            </div>
          </div>
        )}
        {capture_flow.recent.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginBottom: 6 }}>最近采集记录</div>
            {capture_flow.recent.map((c, i) => (
              <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0',
                borderBottom: i < capture_flow.recent.length - 1 ? '1px solid rgba(0,0,0,0.05)' : 'none' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, color: 'var(--mb-text-primary)' }}>{c.app_name || '上下文缺失'}</div>
                  <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 2 }}>{fmtTs(c.ts)}</div>
                </div>
                <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.win_title || '无窗口标题'}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={cardStyle}>
        <div style={sectionTitle}>最近问答记录</div>
        {rag_sessions.recent.length === 0
          ? <div style={{ color: 'var(--mb-text-faint)', fontSize: 12 }}>暂无问答记录</div>
          : rag_sessions.recent.map((s, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0',
              borderBottom: i < rag_sessions.recent.length - 1 ? '1px solid rgba(0,0,0,0.05)' : 'none' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: 'var(--mb-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.query}</div>
                <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 2 }}>{fmtTs(s.ts)}</div>
              </div>
              <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                {s.latency_ms && <span style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>{fmtMs(s.latency_ms)}</span>}
                <span style={{ fontSize: 11, padding: '1px 6px', borderRadius: 4,
                  background: 'rgba(0,122,255,0.08)', color: '#007AFF' }}>{s.context_count} 条</span>
              </div>
            </div>
          ))}
      </div>

      {captureHealth && (
        <div style={cardStyle}>
          <div style={sectionTitle}>采集可靠性</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
            {captureHealth.outcome_counts.length > 0 ? captureHealth.outcome_counts.map(item => (
              <span key={`${item.outcome}-${item.reason}`} style={{
                fontSize: 11,
                color: item.outcome === 'failed' ? 'var(--mb-danger)' : item.outcome === 'degraded' ? 'var(--mb-warning-text)' : 'var(--mb-text-secondary)',
                background: item.outcome === 'failed' ? 'var(--mb-danger-soft)' : item.outcome === 'degraded' ? 'var(--mb-warning-soft)' : 'var(--mb-bg-inset)',
                borderRadius: 999,
                padding: '4px 8px',
              }}>{item.reason} · {item.count}</span>
            )) : <span style={{ color: 'var(--mb-text-tertiary)', fontSize: 12 }}>暂无采集尝试记录</span>}
          </div>
          {captureHealth.recent.slice(0, 8).map((item, index) => (
            <div key={`${item.observed_at}-${index}`} style={{
              display: 'grid',
              gridTemplateColumns: '140px 110px minmax(0, 1fr)',
              gap: 10,
              padding: '7px 0',
              fontSize: 12,
              borderTop: index === 0 ? 'none' : '1px solid rgba(0,0,0,0.05)',
            }}>
              <span style={{ color: 'var(--mb-text-tertiary)' }}>{fmtTs(item.observed_at)}</span>
              <span style={{ color: item.outcome === 'failed' ? '#C62828' : '#3A3A3C' }}>{item.outcome}</span>
              <span style={{ color: '#636366', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {item.is_private ? '隐私活动（内容未记录）' : [item.app_name, item.win_title].filter(Boolean).join(' · ') || item.reason}
                {` · ${item.reason}`}
              </span>
            </div>
          ))}
        </div>
      )}

      <div style={cardStyle}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
          <span style={sectionTitle as any}>最近知识提炼记录</span>
          {knowledge_flow.extracting.length > 0 && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 11, padding: '1px 6px', borderRadius: 4,
              background: 'rgba(52,199,89,0.12)', color: '#34C759', fontWeight: 600,
            }}>
              <span style={{
                width: 6, height: 6, borderRadius: '50%', background: '#34C759',
                animation: 'pulse 1.4s ease-in-out infinite',
              }} />
              提炼中 {knowledge_flow.extracting.length}
            </span>
          )}
        </div>
        {knowledge_flow.extracting.map((c, i) => (
          <div
            key={`extracting-${c.id}`}
            style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0',
              borderBottom: (i < knowledge_flow.extracting.length - 1 || knowledge_flow.recent.length > 0)
                ? '1px solid rgba(0,0,0,0.05)' : 'none',
              background: 'rgba(52,199,89,0.06)',
              marginLeft: -12, marginRight: -12, paddingLeft: 12, paddingRight: 12,
            }}
          >
            <span style={{
              width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
              background: '#34C759', animation: 'pulse 1.4s ease-in-out infinite',
            }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12, color: 'var(--mb-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.win_title || c.app_name || '采集内容'}
              </div>
              <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 2 }}>{fmtTs(c.ts)}</div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2, flexShrink: 0 }}>
              <span style={{ fontSize: 11, padding: '1px 6px', borderRadius: 4, background: 'rgba(52,199,89,0.12)', color: '#34C759', fontWeight: 600 }}>
                提炼中
              </span>
              {c.group_started_at_ms > 0 && (
                <span style={{ fontSize: 10, color: '#34C759', fontVariantNumeric: 'tabular-nums' }}>
                  已提炼 {fmtElapsed(nowMs - c.group_started_at_ms)}
                </span>
              )}
            </div>
          </div>
        ))}
        {knowledge_flow.recent.length === 0 && knowledge_flow.extracting.length === 0
          ? <div style={{ color: 'var(--mb-text-faint)', fontSize: 12 }}>暂无知识记录</div>
          : knowledge_flow.recent.map((item, i) => (
            <div key={item.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0',
              borderBottom: i < knowledge_flow.recent.length - 1 ? '1px solid rgba(0,0,0,0.05)' : 'none' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: 'var(--mb-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.summary || '无摘要'}</div>
                <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 2 }}>{fmtTs(item.ts)}</div>
              </div>
              <div style={{ display: 'flex', gap: 6, flexShrink: 0, alignItems: 'center' }}>
                {!!item.app_name && <span style={{ fontSize: 11, color: 'var(--mb-text-secondary)', maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.app_name}</span>}
                <span style={{ fontSize: 11, padding: '1px 6px', borderRadius: 4, background: 'rgba(191,90,242,0.10)', color: '#BF5AF2' }}>{item.category}</span>
              </div>
            </div>
          ))}
      </div>

      <div style={{ ...cardStyle, marginBottom: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <span style={sectionTitle as any}>定时任务执行记录</span>
          <span style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>成功 {task_executions.success} / 失败 {task_executions.failed}</span>
        </div>
        {task_executions.recent.length === 0
          ? <div style={{ color: 'var(--mb-text-faint)', fontSize: 12 }}>暂无执行记录</div>
          : task_executions.recent.map((e, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0',
              borderBottom: i < task_executions.recent.length - 1 ? '1px solid rgba(0,0,0,0.05)' : 'none' }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                background: STATUS_COLOR[e.status] || '#AEAEB2' }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: 'var(--mb-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.task_name}</div>
                <div style={{ fontSize: 11, color: 'var(--mb-text-faint)', marginTop: 2 }}>{fmtTs(e.started_at)}</div>
              </div>
              <div style={{ display: 'flex', gap: 6, flexShrink: 0, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
                {e.latency_ms && <span>{fmtMs(e.latency_ms)}</span>}
                {e.knowledge_count && <span>{e.knowledge_count} 条知识</span>}
              </div>
            </div>
          ))}
      </div>
    </>
  )
}

const SERVICE_HEALTH_META: Record<string, { title: string; color: string; bg: string; border: string }> = {
  ok: { title: '关键服务正常', color: '#248A3D', bg: 'rgba(52,199,89,0.10)', border: 'rgba(52,199,89,0.24)' },
  degraded: { title: '关键服务降级', color: '#B26A00', bg: 'rgba(255,149,0,0.13)', border: 'rgba(255,149,0,0.28)' },
  down: { title: '关键服务不可用', color: '#C52828', bg: 'rgba(255,59,48,0.12)', border: 'rgba(255,59,48,0.28)' },
}

const formatServiceMode = (mode?: string) => {
  if (mode === 'full') return '完整能力'
  if (mode === 'basic_ipc') return '基础能力'
  if (mode === 'limited') return '受限模式'
  if (mode === 'dry_run') return '演练模式'
  if (mode === 'starting') return '启动中'
  return '状态待确认'
}

const ServiceHealthBanner: React.FC<{ health: MonitorOverview['service_health'] }> = ({ health }) => {
  const meta = SERVICE_HEALTH_META[health.status] ?? SERVICE_HEALTH_META.down
  if (health.status === 'ok') return null
  const issues = health.issues?.length ? health.issues : ['后台整理能力暂未就绪，部分内容可能稍后才能完成提炼']
  return (
    <div style={{
      ...cardStyle,
      borderColor: meta.border,
      background: meta.bg,
      marginBottom: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: meta.color, flexShrink: 0 }} />
        <span style={{ fontSize: 13, fontWeight: 700, color: meta.color }}>{meta.title}</span>
        <span style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>{formatServiceMode(health.mode)}</span>
      </div>
      <div style={{ fontSize: 12, color: '#3A3A3C', lineHeight: 1.6 }}>
        {issues.slice(0, 3).map((issue, idx) => (
          <div key={idx}>{toUserFacingError(issue, '后台能力暂未就绪，请稍后重试')}</div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
        <HealthChip ok={health.critical_checks_passed} label="本地分析" />
        <HealthChip ok={health.embedding_ok} label="语义索引" />
        <HealthChip ok={health.full_dispatch_ready} label="任务分发" />
        <HealthChip ok={health.background_processor_running} label="后台整理" />
      </div>
    </div>
  )
}

const HealthChip: React.FC<{ ok: boolean; label: string }> = ({ ok, label }) => (
  <span style={{
    fontSize: 11,
    padding: '2px 7px',
    borderRadius: 6,
    background: ok ? 'rgba(52,199,89,0.12)' : 'rgba(255,59,48,0.12)',
    color: ok ? '#248A3D' : '#C52828',
    fontWeight: 600,
  }}>
    {label} {ok ? '正常' : '异常'}
  </span>
)

// ── 系统资源内容 ──────────────────────────────────────────────────────────────
const formatCoverageText = (note?: string | null, status?: string | null) => {
  if (status === 'exact') return '覆盖完整'
  if (status === 'partial') return '部分识别'
  if (status === 'unavailable') return '未识别到进程'
  if (note) return '覆盖状态待确认'
  return '状态未知'
}

const formatProcessNames = (names?: string[] | null) => {
  if (!names || names.length === 0) return null
  return `${names.length} 个本地进程`
}

const MODEL_TYPE_META: Record<string, { label: string; color: string; bg: string; border: string }> = {
  llm: { label: '内容分析', color: '#007AFF', bg: 'rgba(0,122,255,0.12)', border: 'rgba(0,122,255,0.18)' },
  embedding: { label: '语义索引', color: '#AF52DE', bg: 'rgba(175,82,222,0.12)', border: 'rgba(175,82,222,0.18)' },
  ocr: { label: '文字识别', color: '#34C759', bg: 'rgba(52,199,89,0.12)', border: 'rgba(52,199,89,0.18)' },
  asr: { label: '语音识别', color: '#FF9500', bg: 'rgba(255,149,0,0.12)', border: 'rgba(255,149,0,0.18)' },
  vlm: { label: '图像理解', color: '#FF2D55', bg: 'rgba(255,45,85,0.12)', border: 'rgba(255,45,85,0.18)' },
}

const getModelTypeMeta = (type?: string | null) => MODEL_TYPE_META[type || ''] || {
  label: '模型',
  color: '#FF2D55',
  bg: 'rgba(255,45,85,0.12)',
  border: 'rgba(255,45,85,0.18)',
}

const normalizeModelName = (rawName?: string | null) => {
  if (!rawName) return ''
  const name = rawName
    .replace(/^RAG Embedding · /, '')
    .replace(/^RAG LLM · /, '')
    .replace(/^Sidecar Embedding · /, '')
    .replace(/^Knowledge Extractor · /, '')
    .trim()
  return formatLlmModelName(name)
}

const getLatestModelStates = (events: SystemResources['model_events']) => {
  const latest = new Map<string, SystemResources['model_events'][number]>()
  for (const event of events) {
    const rawName = event.model_name || ''
    const normalizedName = normalizeModelName(rawName)
    const names = [rawName, normalizedName].filter(Boolean)
    names.forEach((name) => {
      if (!latest.has(name)) latest.set(name, event)
    })
  }
  return latest
}

const getCurrentModelNames = (events: SystemResources['model_events']) => {
  const latest = new Map<string, SystemResources['model_events'][number]>()
  for (const event of events) {
    const normalizedName = normalizeModelName(event.model_name)
    if (!normalizedName) continue
    if (!latest.has(normalizedName)) latest.set(normalizedName, event)
  }
  return Array.from(latest.entries())
    .sort((a, b) => b[1].ts - a[1].ts)
    .map(([name]) => name)
}

const renderProcessTags = (
  names?: string[] | null,
  events: SystemResources['model_events'] = [],
  selectedName?: string | null,
  onSelect?: (name: string | null) => void,
) => {
  if (!names || names.length === 0) return null
  const latestStates = getLatestModelStates(events)
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
      {names.map((name) => {
        const latest = latestStates.get(name)
        const meta = getModelTypeMeta(latest?.model_type)
        const active = selectedName === name
        const stateLabel = latest ? (EVENT_LABEL[latest.event_type] || latest.event_type) : null
        return (
          <button
            key={name}
            onClick={() => onSelect?.(active ? null : name)}
            style={{
              fontSize: 10,
              lineHeight: 1,
              padding: '5px 8px',
              borderRadius: 999,
              background: active ? meta.color : meta.bg,
              color: active ? 'white' : meta.color,
              border: `1px solid ${meta.border}`,
              whiteSpace: 'nowrap',
              cursor: 'pointer',
            }}
            title={stateLabel ? `${meta.label} · ${stateLabel}` : meta.label}
          >
            {stateLabel ? `${name} · ${stateLabel}` : name}
          </button>
        )
      })}
    </div>
  )
}

const MODEL_SERIES_COLORS = ['#FF3B30', '#FF2D55', '#FF9500', '#AF52DE', '#34C759', '#32ADE6']

const getStableSeriesColor = (key: string, fallbackIndex: number) => {
  let hash = 0
  for (let i = 0; i < key.length; i += 1) hash = ((hash << 5) - hash) + key.charCodeAt(i)
  const index = Math.abs(hash || fallbackIndex) % MODEL_SERIES_COLORS.length
  return MODEL_SERIES_COLORS[index]
}

const toTrendLine = (
  series: SystemResources['trends']['model_cpu_series'][number],
  index: number,
  formatter: (value: number) => string,
) => ({
  label: series.label,
  color: getStableSeriesColor(series.key, index),
  data: series.points,
  formatter,
  detailSuffix: formatCoverageText(series.coverage_note, series.coverage_status),
})

const SystemContent: React.FC<{ data: SystemResources | null; range: SystemRange }> = ({ data, range }) => {
  if (!data) return <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '24px 0' }}>暂无数据</div>
  const { latest, disk_trend, model_events, trends } = data
  const runtimeBreakdown = data.model_runtime_breakdown ?? []
  const knowledgeEvents = data.knowledge_events ?? []
  const gpuTrend = data.gpu_trend ?? []
  const modelGpuTrend = data.model_gpu_trend ?? []
  const currentModelNames = getCurrentModelNames(model_events)
  const system = latest.system
  const suite = latest.suite
  const model = latest.model
  const systemMemSub = system ? `${Math.round(system.mem_percent)}% · ${system.mem_used_mb.toLocaleString()} / ${system.mem_total_mb.toLocaleString()} MB` : ''
  const modelCpuLines = [
    { label: '系统', color: '#FF9500', data: trends.system_cpu, formatter: (value: number) => `${value.toFixed(1)}%` },
    { label: '整套软件', color: '#AF52DE', data: trends.suite_cpu, formatter: (value: number) => `${value.toFixed(1)}%` },
    ...((trends.model_cpu_series ?? []).map((series, index) => toTrendLine(series, index, (value) => `${value.toFixed(1)}%`))),
  ]
  const modelMemLines = [
    { label: '系统', color: '#007AFF', data: trends.system_mem, formatter: (value: number) => `${value.toFixed(1)}%` },
    { label: '整套软件', color: '#BF5AF2', data: trends.suite_mem, formatter: (value: number) => `${value.toFixed(0)} MB` },
    ...((trends.model_mem_series ?? []).map((series, index) => toTrendLine(series, index, (value) => `${value.toFixed(0)} MB`))),
    ...((trends.model_estimated_mem_series ?? []).map((series, index) => toTrendLine(series, index + 20, (value) => `${value.toFixed(0)} MB`))),
  ]
  const eventPageSize = 10
  const [eventPage, setEventPage] = useState(1)
  const [selectedModelName, setSelectedModelName] = useState<string | null>(null)
  const filteredModelEvents = selectedModelName
    ? model_events.filter((event) => event.model_name === selectedModelName || normalizeModelName(event.model_name) === selectedModelName)
    : model_events
  const totalEventPages = Math.max(1, Math.ceil(filteredModelEvents.length / eventPageSize))
  const pagedModelEvents = filteredModelEvents.slice((eventPage - 1) * eventPageSize, eventPage * eventPageSize)

  useEffect(() => {
    setEventPage(1)
  }, [range, model_events.length, selectedModelName])

  const renderTrendCard = (
    title: string,
    series: { ts: number; value: number }[],
    color: string,
    formatter: (value: number) => string,
    emptyText = '暂无数据',
  ) => (
    <div style={cardStyle}>
      <div style={sectionTitle}>{getTrendTitle(title, getSystemBucketLabel(range))}</div>
      {series.length > 0
        ? <>
            <SparkLine
              data={series}
              color={color}
              height={50}
              valueFormatter={formatter}
              axisFormatter={(ts) => fmtSystemAxisTs(ts, range)}
            />
            {series.length === 1 && <div style={{ color: 'var(--mb-text-faint)', fontSize: 11, marginTop: 6 }}>当前范围仅 1 个采样点。</div>}
          </>
        : <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>{emptyText}</div>}
    </div>
  )

  const renderDualTrendCard = (
    title: string,
    lines: Array<{
      label: string
      color: string
      data: { ts: number; value: number }[]
      formatter: (value: number) => string
      detailSuffix?: string | null
    }>,
    emptyText = '暂无数据',
  ) => {
    const available = lines.filter(line => line.data.length > 0)
    if (available.length === 0) {
      return (
        <div style={cardStyle}>
          <div style={sectionTitle}>{getTrendTitle(title, getSystemBucketLabel(range))}</div>
          <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>{emptyText}</div>
        </div>
      )
    }

    return (
      <div style={cardStyle}>
        <div style={sectionTitle}>{getTrendTitle(title, getSystemBucketLabel(range))}</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
          {available.map((line, index) => (
            <span key={`${index}-${line.label}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: line.color, display: 'inline-block' }} />
              {line.label}
            </span>
          ))}
        </div>
        <SparkLine
          series={available.map((line) => ({
            label: line.label,
            color: line.color,
            data: line.data,
            valueFormatter: line.formatter,
          }))}
          height={54}
          axisFormatter={(ts) => fmtSystemAxisTs(ts, range)}
          detailFormatter={(point) => {
            const labels = available.map((line) => {
              const current = line.data.find((item) => item.ts === point.ts)
              if (!current) return null
              const suffix = line.detailSuffix ? `（${line.detailSuffix}）` : ''
              return `${line.label}${suffix} ${line.formatter(current.value)}`
            }).filter(Boolean)
            return `${fmtTs(point.ts)} · ${labels.join(' · ')}`
          }}
        />
        {available.every((line) => line.data.length === 1) && (
          <div style={{ color: 'var(--mb-text-faint)', fontSize: 11, marginTop: 6 }}>当前范围仅 1 个采样点。</div>
        )}
      </div>
    )
  }

  return (
    <>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
        {system && (
          <>
            <StatCard label="系统 CPU" value={`${system.cpu_total.toFixed(1)}%`} color="#FF9500" />
            <StatCard label="系统内存" value={`${system.mem_percent.toFixed(1)}%`}
              sub={systemMemSub} color="#007AFF" />
            {(system.gpu_percent != null || system.gpu_name) && (
              <StatCard
                label="系统 GPU"
                value={system.gpu_percent != null ? `${system.gpu_percent.toFixed(1)}%` : '已检测'}
                sub={system.gpu_total_label || system.gpu_name || 'GPU'}
                color="#34C759"
              />
            )}
          </>
        )}
        {suite && (
          <>
            <StatCard label="整套 CPU" value={`${suite.cpu_percent.toFixed(1)}%`}
              sub={`${suite.process_count} 个进程`} color="#AF52DE" />
            <StatCard label="整套内存" value={`${suite.mem_process_mb} MB`}
              sub={formatCoverageText(suite.coverage_note, suite.coverage_status)} color="#BF5AF2" />
          </>
        )}
        {model && (
          <>
            <StatCard label="模型 CPU" value={`${model.cpu_percent.toFixed(1)}%`}
              sub={formatProcessNames(model.process_names) || `${model.process_count} 个进程`} color="#FF3B30" />
            <StatCard label="模型内存" value={`${model.mem_process_mb} MB`}
              sub={formatProcessNames(model.process_names) || formatCoverageText(model.coverage_note, model.coverage_status)} color="#FF2D55" />
          </>
        )}
        <StatCard label="数据库大小" value={fmtBytes(data.db_size_bytes)} color="#32ADE6" />
      </div>

      {model && (
        <div style={{
          background: 'rgba(255,45,85,0.05)',
          border: '1px solid rgba(255,45,85,0.10)',
          borderRadius: 10,
          padding: '8px 10px',
          marginBottom: 10,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 11, color: 'var(--mb-text-secondary)', marginBottom: 2 }}>当前模型</div>
            {selectedModelName && (
              <button
                onClick={() => setSelectedModelName(null)}
                style={{
                  fontSize: 11,
                  padding: '2px 8px',
                  borderRadius: 999,
                  border: '1px solid var(--mb-border-strong)',
                  background: 'var(--mb-bg-input)',
                  color: 'var(--mb-text-secondary)',
                  cursor: 'pointer',
                }}
              >清除高亮</button>
            )}
          </div>
          {renderProcessTags(currentModelNames, model_events, selectedModelName, setSelectedModelName) || <div style={{ fontSize: 11, color: 'var(--mb-text-faint)' }}>暂无模型名称</div>}
        </div>
      )}

      {renderDualTrendCard('CPU 趋势（系统 vs 整套软件 vs 模型分线）', modelCpuLines, '暂无 CPU 趋势数据')}
      {renderDualTrendCard('内存趋势（系统 vs 整套软件 vs 模型分线）', modelMemLines, '暂无内存趋势数据')}
      <RuntimeBreakdownCard items={runtimeBreakdown} />

      <div style={cardStyle}>
        <div style={sectionTitle}>{getTrendTitle('GPU 趋势（系统 vs 模型）', getSystemBucketLabel(range))}</div>
        {(gpuTrend.length > 0 || modelGpuTrend.length > 0) ? (
          <>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
              {gpuTrend.length > 0 && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#34C759', display: 'inline-block' }} />
                  系统
                </span>
              )}
              {modelGpuTrend.length > 0 && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#FF2D55', display: 'inline-block' }} />
                  模型
                </span>
              )}
            </div>
            <SparkLine
              series={[
                ...(gpuTrend.length > 0 ? [{ label: '系统', color: '#34C759', data: gpuTrend, valueFormatter: (value: number) => `${value.toFixed(1)}%` }] : []),
                ...(modelGpuTrend.length > 0 ? [{ label: '模型', color: '#FF2D55', data: modelGpuTrend, valueFormatter: (value: number) => `${value.toFixed(1)}%` }] : []),
              ]}
              height={50}
              axisFormatter={(ts) => fmtSystemAxisTs(ts, range)}
            />
            <div style={{ marginTop: 8, fontSize: 11, color: 'var(--mb-text-secondary)' }}>
              GPU 总体：{system?.gpu_total_label || system?.gpu_name || '未检测'}
            </div>
            {gpuTrend.length <= 1 && modelGpuTrend.length <= 1 && (
              <div style={{ color: 'var(--mb-text-faint)', fontSize: 11, marginTop: 6 }}>当前范围仅 1 个 GPU 采样点。</div>
            )}
          </>
        ) : (
          <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>
            {system?.gpu_name ? `已检测到 ${system.gpu_name}，但当前范围内暂无 GPU 利用率采样。` : '当前设备未返回 GPU 利用率数据'}
          </div>
        )}
      </div>

      {knowledgeEvents.length > 0 && (
        <div style={cardStyle}>
          <div style={sectionTitle}>知识提炼触发与资源时间轴</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
            {knowledgeEvents.map((item, index) => (
              <span key={`${item.ts}-${index}`} style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'rgba(191,90,242,0.10)', color: '#BF5AF2' }}>
                {fmtAxisTs(item.ts)} · {item.count} 条
              </span>
            ))}
          </div>
          <div style={{ fontSize: 11, color: 'var(--mb-text-faint)' }}>结合系统 / 整套软件 / 模型趋势，可观察知识提炼触发时段与资源波动关系。</div>
        </div>
      )}

      <div style={cardStyle}>
        <div style={sectionTitle}>磁盘 IO（MB）</div>
        {disk_trend.length > 1 ? (
          <>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 11, color: '#34C759', marginBottom: 4 }}>读取</div>
                <SparkLine
                  data={disk_trend.map(p => ({ ts: p.ts, value: p.read_mb }))}
                  color="#34C759"
                  height={40}
                  valueFormatter={(value) => `${value.toFixed(2)} MB`}
                  axisFormatter={(ts) => fmtSystemAxisTs(ts, range)}
                />
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 11, color: '#FF3B30', marginBottom: 4 }}>写入</div>
                <SparkLine
                  data={disk_trend.map(p => ({ ts: p.ts, value: p.write_mb }))}
                  color="#FF3B30"
                  height={40}
                  valueFormatter={(value) => `${value.toFixed(2)} MB`}
                  axisFormatter={(ts) => fmtSystemAxisTs(ts, range)}
                />
              </div>
            </div>
          </>
        ) : <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '12px 0' }}>暂无数据</div>}
      </div>

      <div style={{ ...cardStyle, marginBottom: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
          <span style={sectionTitle as any}>模型加载/卸载事件</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {selectedModelName && <span style={{ fontSize: 11, color: '#FF2D55' }}>已筛选：{selectedModelName}</span>}
            {filteredModelEvents.length > eventPageSize && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <button
                  onClick={() => setEventPage((page) => Math.max(1, page - 1))}
                  disabled={eventPage === 1}
                  style={{ fontSize: 11, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--mb-border-strong)', background: eventPage === 1 ? 'var(--mb-bg-inset)' : 'var(--mb-bg-input)', color: 'var(--mb-text-secondary)', cursor: eventPage === 1 ? 'default' : 'pointer' }}
                >上一页</button>
                <span style={{ fontSize: 11, color: 'var(--mb-text-secondary)' }}>{eventPage} / {totalEventPages}</span>
                <button
                  onClick={() => setEventPage((page) => Math.min(totalEventPages, page + 1))}
                  disabled={eventPage === totalEventPages}
                  style={{ fontSize: 11, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--mb-border-strong)', background: eventPage === totalEventPages ? 'var(--mb-bg-inset)' : 'var(--mb-bg-input)', color: 'var(--mb-text-secondary)', cursor: eventPage === totalEventPages ? 'default' : 'pointer' }}
                >下一页</button>
              </div>
            )}
          </div>
        </div>
        {filteredModelEvents.length === 0
          ? <div style={{ color: 'var(--mb-text-faint)', fontSize: 12 }}>暂无事件</div>
          : pagedModelEvents.map((e, i) => {
            const active = selectedModelName === e.model_name || selectedModelName === normalizeModelName(e.model_name)
            const meta = getModelTypeMeta(e.model_type)
            return (
              <div key={`${e.ts}-${e.model_name}-${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0',
                borderBottom: i < pagedModelEvents.length - 1 ? '1px solid rgba(0,0,0,0.05)' : 'none',
                background: active ? meta.bg : 'transparent',
                borderRadius: 8,
                paddingLeft: 6,
                paddingRight: 6 }}>
                <span style={{ fontSize: 10, padding: '1px 6px', borderRadius: 4, flexShrink: 0,
                  background: `${EVENT_COLOR[e.event_type] || '#6E6E73'}18`,
                  color: EVENT_COLOR[e.event_type] || '#6E6E73' }}>
                  {EVENT_LABEL[e.event_type] || e.event_type}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ fontSize: 12, color: 'var(--mb-text-primary)' }}>{normalizeModelName(e.model_name) || e.model_name}</span>
                  <span style={{ fontSize: 11, color: meta.color, marginLeft: 6 }}>{meta.label}</span>
                </div>
                <div style={{ flexShrink: 0, fontSize: 11, color: 'var(--mb-text-secondary)', textAlign: 'right' }}>
                  {e.duration_ms && <span>{fmtMs(e.duration_ms)} · </span>}
                  {e.memory_mb && <span>{e.memory_mb} MB · </span>}
                  <span>{fmtTs(e.ts)}</span>
                </div>
              </div>
            )
          })}
      </div>
    </>
  )
}

const ErrorNotice: React.FC<{ message: string }> = ({ message }) => (
  <div style={{
    background: 'rgba(255,59,48,0.08)',
    border: '1px solid rgba(255,59,48,0.16)',
    color: '#C62828',
    borderRadius: 10,
    padding: '10px 12px',
    marginBottom: 10,
    fontSize: 12,
    lineHeight: 1.5,
  }}>
    运行数据加载失败：{toUserFacingError(message, '暂时无法读取运行数据')}
  </div>
)

const OverviewRangeControl: React.FC<{
  value: OverviewRange
  onChange: (next: OverviewRange) => void
}> = ({ value, onChange }) => {
  const updateAmount = (raw: string) => {
    const parsed = Math.max(1, Math.min(90, Math.round(Number(raw) || 1)))
    onChange({ ...value, amount: parsed })
  }
  return (
    <div className="monitor-range-control">
      <div className="monitor-range-control__custom">
        <span className="monitor-range-control__label">最近</span>
        <input
          aria-label="时间范围数值"
          type="number"
          min={1}
          max={90}
          value={value.amount}
          onChange={(event) => updateAmount(event.target.value)}
          className="monitor-range-control__amount"
        />
        <select
          aria-label="时间范围单位"
          value={value.unit}
          onChange={(event) => onChange({ ...value, unit: event.target.value as TimeUnit })}
          className="monitor-range-control__unit"
        >
          <option value="minute">分钟</option>
          <option value="hour">小时</option>
          <option value="day">天</option>
        </select>
      </div>
      <div className="monitor-range-control__presets" role="group" aria-label="快捷时间范围">
        {OVERVIEW_QUICK_RANGES.map(({ label, range }) => {
          const active = isSameOverviewRange(value, range)
          return (
            <button key={label} onClick={() => onChange(range)} style={{
              background: active ? '#007AFF' : 'var(--mb-bg-input)',
              color: active ? 'white' : 'var(--mb-text-secondary)',
            }}>{label}</button>
          )
        })}
      </div>
    </div>
  )
}

// ── 主组件 ──────────────────────────────────────────────────────────────────
const MonitorPanel: React.FC = () => {
  const { apiBaseUrl } = useAppStore()
  const base = apiBaseUrl || API

  const [tab, setTab] = useState<'overview' | 'dag' | 'system'>('overview')
  const [isVisible, setIsVisible] = useState(() => document.visibilityState === 'visible')
  const systemAbortRef = useRef<AbortController | null>(null)
  const [data, setData] = useState<MonitorOverview | null>(null)
  const [liveData, setLiveData] = useState<ExtractionLive | null>(null)
  const [captureHealth, setCaptureHealth] = useState<CaptureHealth | null>(null)
  const [nowMs, setNowMs] = useState<number>(() => Date.now())
  const [sysData, setSysData] = useState<SystemResources | null>(null)
  const [range, setRange] = useState<OverviewRange>({ amount: 6, unit: 'hour' })
  const [sysRange, setSysRange] = useState<SystemRange>('6h')
  const [loadingOverview, setLoadingOverview] = useState(false)
  const [loadingSystem, setLoadingSystem] = useState(false)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [systemError, setSystemError] = useState<string | null>(null)
  const [enablingCapture, setEnablingCapture] = useState(false)
  const [enableCaptureError, setEnableCaptureError] = useState<string | null>(null)

  const load = async () => {
    setLoadingOverview(true)
    setOverviewError(null)
    try {
      const res = await fetch(`${base}/api/monitor/overview?range_ms=${overviewRangeToMs(range)}`)
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const json = await res.json()
      setData(json)
      setNowMs(Date.now())
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setData(null)
      setOverviewError(message)
      console.error(e)
    } finally {
      setLoadingOverview(false)
    }
  }

  const loadLive = async () => {
    try {
      const res = await fetch(`${base}/api/monitor/extraction_live?range_ms=${overviewRangeToMs(range)}`)
      if (!res.ok) return
      const json = (await res.json()) as ExtractionLive
      setLiveData(json)
    } catch {
      // 静默失败：保留上一次 liveData，避免短暂网络抖动导致 UI 闪烁
    }
  }

  const loadCaptureHealth = async () => {
    try {
      const res = await fetch(`${base}/api/monitor/capture-health`)
      if (!res.ok) return
      setCaptureHealth((await res.json()) as CaptureHealth)
    } catch {
      // 保留上一次状态，避免本机服务短暂重启导致可靠性卡片闪烁。
    }
  }

  const loadSys = async () => {
    systemAbortRef.current?.abort()
    const controller = new AbortController()
    systemAbortRef.current = controller
    setLoadingSystem(true)
    setSystemError(null)
    try {
      const res = await fetch(`${base}/api/monitor/system?range=${sysRange}`, {
        signal: controller.signal,
      })
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const json = await res.json()
      setSysData(json)
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        return
      }
      const message = e instanceof Error ? e.message : String(e)
      setSysData(null)
      setSystemError(message)
      console.error(e)
    } finally {
      if (systemAbortRef.current === controller) {
        systemAbortRef.current = null
      }
      setLoadingSystem(false)
    }
  }

  const enableCapture = async () => {
    setEnablingCapture(true)
    setEnableCaptureError(null)
    try {
      const response = await fetch(`${base}/api/runtime/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ capture_enabled: true }),
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const status = await response.json() as { capture_enabled?: boolean }
      if (status.capture_enabled !== true) throw new Error('本地服务未确认开启状态')
      await invoke('set_capture_menu_state', { enabled: true }).catch(() => {})
      await Promise.all([load(), loadLive(), loadCaptureHealth()])
    } catch (error) {
      setEnableCaptureError(toUserFacingError(error, '开启失败，请确认本机服务正常后重试'))
    } finally {
      setEnablingCapture(false)
    }
  }

  useEffect(() => {
    if (tab === 'overview') {
      load()
      loadCaptureHealth()
    }
  }, [base, range, tab])
  useEffect(() => { if (tab === 'system' && isVisible) loadSys() }, [base, sysRange, tab, isVisible])

  useEffect(() => {
    const handleVisibilityChange = () => setIsVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange)
  }, [])

  useEffect(() => {
    if (tab !== 'system' || !isVisible) return
    const timer = window.setInterval(() => {
      loadSys()
    }, 30000)
    return () => window.clearInterval(timer)
  }, [tab, base, sysRange, isVisible])

  useEffect(() => {
    if (tab !== 'overview' || !isVisible) return
    const timer = window.setInterval(() => {
      load()
      loadCaptureHealth()
    }, 15000)
    return () => window.clearInterval(timer)
  }, [tab, base, range, isVisible])

  // 实时提炼状态：3s 高频轮询 /api/monitor/extraction_live，
  // 体积约为 overview 的 1/30，在切回 tab/恢复可见时立即拉一次
  useEffect(() => {
    if (tab !== 'overview' || !isVisible) return
    loadLive()
    const timer = window.setInterval(() => {
      loadLive()
    }, 3000)
    return () => window.clearInterval(timer)
  }, [tab, base, range, isVisible])

  // 1s 计时器：让「提炼中」行的「已提炼 Xs」逐秒跳。
  // 仅在 overview tab 可见 且 当前确实有提炼中条目时启用，避免空转。
  const hasExtracting = (liveData?.extracting?.length ?? 0) > 0
  useEffect(() => {
    if (tab !== 'overview' || !isVisible || !hasExtracting) return
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [tab, isVisible, hasExtracting])

  useEffect(() => () => {
    systemAbortRef.current?.abort()
  }, [])

  if ((loadingOverview && !data && tab === 'overview' && !overviewError)
    || (loadingSystem && !sysData && tab === 'system' && !systemError)) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100%', color: 'var(--mb-text-faint)', fontSize: 13 }}>加载中...</div>
  )

  return (
    <div className="monitor-panel">

      {/* Header */}
      <div className="monitor-toolbar">
        <div className="monitor-toolbar__tabs">
          {(['overview', 'system', 'dag'] as const).map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              fontSize: 12, padding: '4px 10px', borderRadius: 7, border: 'none', cursor: 'pointer',
              background: tab === t ? '#007AFF' : 'var(--mb-bg-input)',
              color: tab === t ? 'white' : 'var(--mb-text-secondary)',
              fontWeight: tab === t ? 600 : 400,
            }}>{t === 'overview' ? '总览' : t === 'system' ? '系统资源' : '提炼流程'}</button>
          ))}
        </div>
        <div className="monitor-toolbar__actions">
          {tab === 'overview' && <OverviewRangeControl value={range} onChange={setRange} />}
          {tab === 'system' && ([
            { value: '1h', label: '1h' },
            { value: '6h', label: '6h' },
            { value: '24h', label: '24h' },
            { value: '1d', label: '今天' },
          ] as const).map(({ value, label }) => (
            <button key={value} onClick={() => setSysRange(value)} style={{
              fontSize: 11, padding: '3px 8px', borderRadius: 6, border: 'none', cursor: 'pointer',
              background: sysRange === value ? '#007AFF' : 'var(--mb-bg-input)',
              color: sysRange === value ? 'white' : 'var(--mb-text-secondary)',
            }}>{label}</button>
          ))}
          <button onClick={tab === 'overview'
            ? () => { load(); loadCaptureHealth() }
            : tab === 'system' ? loadSys : () => {}} style={{
            fontSize: 11, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--mb-border-strong)',
            background: 'var(--mb-bg-input)', color: 'var(--mb-text-secondary)', cursor: 'pointer',
            visibility: tab === 'dag' ? 'hidden' : 'visible',
          }}>刷新</button>
        </div>
      </div>

      {tab === 'overview' && overviewError && <ErrorNotice message={`${overviewError}，请检查本机服务连接地址或确认应用服务已启动`} />}
      {tab === 'system' && systemError && <ErrorNotice message={`${systemError}，请检查本机服务连接地址或确认应用服务已启动`} />}

      {tab === 'overview' && data && (
        <OverviewContent
          data={data}
          range={range}
          liveData={liveData}
          nowMs={nowMs}
          enablingCapture={enablingCapture}
          enableCaptureError={enableCaptureError}
          onEnableCapture={() => void enableCapture()}
          captureHealth={captureHealth}
        />
      )}
      {tab === 'overview' && !data && !overviewError && !loadingOverview && (
        <div style={{ color: 'var(--mb-text-faint)', fontSize: 12, textAlign: 'center', padding: '24px 0' }}>暂无运行数据</div>
      )}
      {tab === 'system' && <SystemContent data={sysData} range={sysRange} />}
      {tab === 'dag' && <PipelineDagPanel base={base} isVisible={isVisible} />}

    </div>
  )
}

const cardStyle: React.CSSProperties = {
  background: 'linear-gradient(180deg, var(--mb-bg-card) 0%, var(--mb-bg-elevated) 100%)',
  borderRadius: 12,
  padding: '13px 14px',
  border: '1px solid rgba(60,60,67,0.10)',
  boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 12px 28px rgba(30,41,59,0.05)',
  marginBottom: 10,
}

const sectionTitle: React.CSSProperties = {
  fontSize: 12, fontWeight: 700, color: 'var(--mb-text-primary)', marginBottom: 10, display: 'block',
}

export default MonitorPanel
