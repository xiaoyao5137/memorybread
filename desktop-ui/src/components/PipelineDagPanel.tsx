import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { DagItem, DagStage, DagStageKey, PipelineDagResponse } from '../types'
import { useAppStore } from '../store/useAppStore'

interface Props {
  base: string
  isVisible: boolean
}

// DAG 布局：分叉
//   capture → timeline → ┬─ knowledge
//                        ├─ sop
//                        ├─ document
//                        └─ data
const NODE_W = 168
const NODE_H = 96
const COL_GAP = 96
const ROW_GAP = 18
const SVG_W = NODE_W * 4 + COL_GAP * 3 + 48
const SVG_H = NODE_H * 4 + ROW_GAP * 3 + 48
const PIPELINE_CENTER_Y = 24 + (NODE_H + ROW_GAP) * 1.5

const NODE_POS: Record<DagStageKey, { x: number; y: number }> = {
  capture:   { x: 24,                                  y: PIPELINE_CENTER_Y },
  timeline:  { x: 24 + (NODE_W + COL_GAP),             y: PIPELINE_CENTER_Y },
  knowledge: { x: 24 + (NODE_W + COL_GAP) * 2,         y: 24 },
  sop:       { x: 24 + (NODE_W + COL_GAP) * 2,         y: 24 + NODE_H + ROW_GAP },
  document:  { x: 24 + (NODE_W + COL_GAP) * 2,         y: 24 + (NODE_H + ROW_GAP) * 2 },
  data:      { x: 24 + (NODE_W + COL_GAP) * 2,         y: 24 + (NODE_H + ROW_GAP) * 3 },
}

const STAGE_COLOR: Record<DagStageKey, { fill: string; stroke: string; accent: string }> = {
  capture:   { fill: 'color-mix(in srgb, #007AFF 12%, var(--mb-bg-card))', stroke: '#007AFF', accent: '#007AFF' },
  timeline:  { fill: 'color-mix(in srgb, #FF9500 12%, var(--mb-bg-card))', stroke: '#FF9500', accent: '#FF9500' },
  knowledge: { fill: 'color-mix(in srgb, #AF52DE 12%, var(--mb-bg-card))', stroke: '#AF52DE', accent: '#AF52DE' },
  sop:       { fill: 'color-mix(in srgb, #34C759 12%, var(--mb-bg-card))', stroke: '#34C759', accent: '#34C759' },
  document:  { fill: 'color-mix(in srgb, #FF3B30 12%, var(--mb-bg-card))', stroke: '#FF3B30', accent: '#FF3B30' },
  data:      { fill: 'color-mix(in srgb, #008A8A 12%, var(--mb-bg-card))', stroke: '#008A8A', accent: '#008A8A' },
}

const EXTRACTOR_LABEL: Record<string, { text: string; color: string }> = {
  running: { text: '提炼中', color: '#34C759' },
  waiting: { text: '等待中', color: '#FF9500' },
  idle:    { text: '空闲',   color: 'var(--mb-text-tertiary)' },
  stalled: { text: '已停止', color: '#FF3B30' },
  paused:  { text: '已暂停', color: 'var(--mb-text-tertiary)' },
}

function emptyStage(
  key: DagStageKey,
  label: string,
  inProgressLabel: string,
  pendingLabel = '',
): DagStage {
  return {
    key,
    label,
    in_progress_label: inProgressLabel,
    pending_label: pendingLabel,
    in_progress_count: 0,
    pending_count: 0,
    completed_today: 0,
    in_progress_items: [],
    pending_items: [],
  }
}

// 后端与桌面端可能在滚动升级期间短暂版本不一致。先提供完整中文阶段，
// 再用接口数据覆盖，避免缺失阶段退回英文 key，也保证节点始终可打开抽屉。
const DEFAULT_STAGES: Record<DagStageKey, DagStage> = {
  capture: emptyStage('capture', '采集', '提炼中', '排队'),
  timeline: emptyStage('timeline', '预提炼', '提炼中', '待提炼'),
  knowledge: emptyStage('knowledge', '知识', '生成中'),
  sop: emptyStage('sop', '操作手册', '生成中'),
  document: emptyStage('document', '文档', '生成中'),
  data: emptyStage('data', '数据', '生成中'),
}

function fmtRelTime(ms: number, nowMs: number): string {
  const diff = Math.max(0, nowMs - ms)
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s 前`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m 前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h 前`
  return `${Math.floor(diff / 86_400_000)}d 前`
}

// 兼容字段 bake_watermark_lag_ms 现在表示“最老烘焙候选已等待多久”。
// 无库存=灰色；<2h=正常；2-24h=橙色提示；>24h=红色异常。
function fmtWatermarkLag(lagMs: number): { text: string; color: string } {
  if (lagMs <= 0) return { text: '烘焙队列已清空', color: 'var(--mb-text-tertiary)' }
  const min = Math.floor(lagMs / 60_000)
  const hr = Math.floor(lagMs / 3_600_000)
  const day = Math.floor(lagMs / 86_400_000)
  let text: string
  if (lagMs < 60_000) text = '最老等待 <1m'
  else if (lagMs < 3_600_000) text = `最老等待 ${min}m`
  else if (lagMs < 86_400_000) text = `最老等待 ${hr}h ${min - hr * 60}m`
  else text = `最老等待 ${day}d ${hr - day * 24}h`
  const color = lagMs >= 86_400_000 ? '#FF3B30' : lagMs >= 7_200_000 ? '#FF9500' : '#34C759'
  return { text, color }
}

const PipelineDagPanel: React.FC<Props> = ({ base, isVisible }) => {
  const setWindowMode = useAppStore((s) => s.setWindowMode)
  const setBakeTab = useAppStore((s) => s.setBakeTab)
  const setSelectedCaptureId = useAppStore((s) => s.setSelectedCaptureId)
  const setSelectedKnowledgeId = useAppStore((s) => s.setSelectedKnowledgeId)
  const setSelectedSopId = useAppStore((s) => s.setSelectedSopId)
  const setSelectedTemplateId = useAppStore((s) => s.setSelectedTemplateId)
  const setSelectedMemoryId = useAppStore((s) => s.setSelectedMemoryId)
  const setRepositoryMemoryFocusId = useAppStore((s) => s.setRepositoryMemoryFocusId)
  const setBakeTemplateFocusId = useAppStore((s) => s.setBakeTemplateFocusId)
  const setBakeKnowledgeFocusId = useAppStore((s) => s.setBakeKnowledgeFocusId)
  const setBakeSopFocusId = useAppStore((s) => s.setBakeSopFocusId)
  const setRepositoryCaptureSourceCaptureId = useAppStore((s) => s.setRepositoryCaptureSourceCaptureId)
  const setRepositoryTab = useAppStore((s) => s.setRepositoryTab)

  const [data, setData] = useState<PipelineDagResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [drawerStage, setDrawerStage] = useState<DagStageKey | null>(null)
  const [nowMs, setNowMs] = useState<number>(() => Date.now())
  const abortRef = useRef<AbortController | null>(null)

  const load = async () => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      const res = await fetch(`${base}/api/monitor/pipeline_dag`, { signal: ctrl.signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = (await res.json()) as PipelineDagResponse
      setData(json)
      setError(null)
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    if (!isVisible) return
    load()
    const timer = window.setInterval(load, 3000)
    return () => window.clearInterval(timer)
  }, [base, isVisible])

  useEffect(() => {
    if (!isVisible) return
    const t = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [isVisible])

  useEffect(() => () => abortRef.current?.abort(), [])

  const stagesByKey = useMemo(() => {
    const map: Record<DagStageKey, DagStage> = { ...DEFAULT_STAGES }
    if (data) for (const s of data.stages) map[s.key] = s
    return map
  }, [data])

  const drawerStageData = drawerStage ? stagesByKey[drawerStage] : null

  const handleItemClick = (item: DagItem) => {
    switch (item.kind) {
      case 'capture':
        setSelectedCaptureId(String(item.id))
        setRepositoryTab('capture')
        setRepositoryCaptureSourceCaptureId(String(item.id))
        setWindowMode('knowledge')
        break
      case 'timeline':
        setSelectedMemoryId(String(item.id))
        setRepositoryMemoryFocusId(String(item.id))
        setRepositoryTab('memory')
        setWindowMode('knowledge')
        break
      case 'bake_knowledge':
        setSelectedKnowledgeId(String(item.id))
        setBakeKnowledgeFocusId(String(item.id))
        setWindowMode('bake')
        setBakeTab('knowledge')
        break
      case 'bake_sop':
        setSelectedSopId(String(item.id))
        setBakeSopFocusId(String(item.id))
        setWindowMode('bake')
        setBakeTab('sop')
        break
      case 'document':
        setSelectedTemplateId(String(item.id))
        setBakeTemplateFocusId(String(item.id))
        setWindowMode('bake')
        setBakeTab('templates')
        break
      case 'data':
        setWindowMode('bake')
        setBakeTab('data')
        break
      default:
        break
    }
    setDrawerStage(null)
  }

  // SVG 连线（capture→timeline→4 个分叉）
  const edges: { from: DagStageKey; to: DagStageKey }[] = [
    { from: 'capture', to: 'timeline' },
    { from: 'timeline', to: 'knowledge' },
    { from: 'timeline', to: 'sop' },
    { from: 'timeline', to: 'document' },
    { from: 'timeline', to: 'data' },
  ]

  return (
    <div style={{ position: 'relative' }}>
      {error && (
        <div style={{
          background: 'rgba(255,59,48,0.08)', border: '1px solid rgba(255,59,48,0.16)',
          color: 'var(--mb-danger)', borderRadius: 10, padding: '8px 12px', fontSize: 12, marginBottom: 10,
        }}>
          DAG 加载失败：{error}
        </div>
      )}

      {/* 顶部状态条 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12,
        background: 'var(--mb-bg-card)', borderRadius: 10, padding: '10px 14px',
        border: '1px solid var(--mb-border)', fontSize: 12,
      }}>
        <span style={{ color: 'var(--mb-text-secondary)' }}>提炼器：</span>
        <span style={{
          color: EXTRACTOR_LABEL[data?.extractor_status ?? 'idle']?.color ?? '#8E8E93',
          fontWeight: 600,
        }}>
          {EXTRACTOR_LABEL[data?.extractor_status ?? 'idle']?.text ?? '未知'}
        </span>
        {data?.running_bake_runs && data.running_bake_runs.length > 0 && (
          <>
            <span style={{ color: 'var(--mb-text-faint)' }}>|</span>
            {data.running_bake_runs.map((run, idx) => (
              <React.Fragment key={run.id}>
                <span style={{ color: '#FF9500', fontWeight: 600 }}>
                  批次进行中 · #{run.id}
                </span>
                <span style={{ color: 'var(--mb-text-tertiary)' }}>
                  （{run.trigger_reason}，已运行 {fmtRelTime(run.started_at, nowMs)}）
                </span>
                {idx < data.running_bake_runs.length - 1 && (
                  <span style={{ color: 'var(--mb-text-faint)' }}>·</span>
                )}
              </React.Fragment>
            ))}
          </>
        )}
        <span style={{ color: 'var(--mb-text-faint)' }}>|</span>
        {(() => {
          const lag = fmtWatermarkLag(data?.bake_watermark_lag_ms ?? 0)
          if (data?.capture_enabled === false) {
            return (
              <span
                title="自动采集与提炼已暂停；当前库存会在恢复后继续处理。"
                style={{ color: 'var(--mb-text-tertiary)', fontWeight: 600, fontVariantNumeric: 'tabular-nums', marginLeft: 'auto' }}
              >
                自动烘焙已暂停 · {lag.text}
              </span>
            )
          }
          return (
            <span
              title="最老一条实际待烘焙 timeline 的等待时长。>2h 提示积压，>24h 显示红色告警。"
              style={{ color: lag.color, fontWeight: 600, fontVariantNumeric: 'tabular-nums', marginLeft: 'auto' }}
            >
              {lag.text}
            </span>
          )
        })()}
        <span style={{ color: 'var(--mb-text-faint)' }}>|</span>
        <span style={{ color: 'var(--mb-text-faint)', fontVariantNumeric: 'tabular-nums' }}>
          3s 自动刷新
        </span>
      </div>

      {/* DAG SVG */}
      <div style={{
        background: 'var(--mb-bg-card)', borderRadius: 12, padding: 12,
        border: '1px solid var(--mb-border)', overflow: 'auto',
      }}>
        <svg width={SVG_W} height={SVG_H} style={{ display: 'block' }}>
          {/* 连线 */}
          {edges.map(({ from, to }) => {
            const a = NODE_POS[from]
            const b = NODE_POS[to]
            const x1 = a.x + NODE_W
            const y1 = a.y + NODE_H / 2
            const x2 = b.x
            const y2 = b.y + NODE_H / 2
            const midX = (x1 + x2) / 2
            const path = `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`
            const isDownstreamEdge = from !== 'capture'
            const animated = (stagesByKey[from]?.in_progress_count ?? 0) > 0
              || (stagesByKey[to]?.in_progress_count ?? 0) > 0
              || (isDownstreamEdge && !!data?.running_bake_run)
            return (
              <g key={`${from}-${to}`}>
                <path d={path} stroke="#D1D1D6" strokeWidth={2} fill="none" />
                {animated && (
                  <path
                    d={path}
                    stroke={STAGE_COLOR[to].accent}
                    strokeWidth={2}
                    fill="none"
                    strokeDasharray="6 4"
                  >
                    <animate
                      attributeName="stroke-dashoffset"
                      from="0"
                      to="-20"
                      dur="1s"
                      repeatCount="indefinite"
                    />
                  </path>
                )}
              </g>
            )
          })}

          {/* 节点 */}
          {(Object.keys(NODE_POS) as DagStageKey[]).map((key) => {
            const pos = NODE_POS[key]
            const stage = stagesByKey[key]
            const color = STAGE_COLOR[key]
            const ip = stage?.in_progress_count ?? 0
            const pd = stage?.pending_count ?? 0
            const ct = stage?.completed_today ?? 0
            const label = stage?.label ?? key
            const inProgressLabel = stage?.in_progress_label ?? '提炼中'
            const pendingLabel = stage?.pending_label ?? '排队'
            const showPending = pendingLabel.trim().length > 0
            return (
              <g
                key={key}
                transform={`translate(${pos.x}, ${pos.y})`}
                style={{ cursor: 'pointer' }}
                onClick={() => setDrawerStage(key)}
              >
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={10}
                  style={{ fill: color.fill }}
                  stroke={color.stroke}
                  strokeWidth={ip > 0 ? 2 : 1}
                />
                <text x={12} y={20} fontSize={12} fontWeight={600} style={{ fill: 'var(--mb-text-primary)' }}>
                  {label}
                </text>
                <text x={12} y={42} fontSize={11} style={{ fill: 'var(--mb-text-secondary)' }}>
                  {inProgressLabel} <tspan fill={color.accent} fontWeight={600}>{ip}</tspan>
                </text>
                {showPending && (
                  <text x={12} y={60} fontSize={11} style={{ fill: 'var(--mb-text-secondary)' }}>
                    {pendingLabel} <tspan style={{ fill: 'var(--mb-text-primary)' }} fontWeight={600}>{pd}</tspan>
                  </text>
                )}
                <text x={12} y={showPending ? 78 : 62} fontSize={11} style={{ fill: 'var(--mb-text-tertiary)' }}>
                  今日完成 <tspan fontWeight={600}>{ct}</tspan>
                </text>
              </g>
            )
          })}
        </svg>
      </div>

      {/* 抽屉 */}
      {drawerStage && drawerStageData && (
        <Drawer
          stage={drawerStageData}
          onClose={() => setDrawerStage(null)}
          onItemClick={handleItemClick}
          nowMs={nowMs}
        />
      )}
    </div>
  )
}

const Drawer: React.FC<{
  stage: DagStage
  onClose: () => void
  onItemClick: (item: DagItem) => void
  nowMs: number
}> = ({ stage, onClose, onItemClick, nowMs }) => {
  const accent = STAGE_COLOR[stage.key].accent
  const inProgressLabel = stage.in_progress_label ?? '提炼中'
  const pendingLabel = stage.pending_label ?? '排队'
  const showPending = pendingLabel.trim().length > 0
  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.25)', zIndex: 100,
        }}
      />
      <div style={{
        position: 'fixed', top: 0, right: 0, bottom: 0, width: 420,
        background: 'var(--mb-bg-page)', boxShadow: '-2px 0 12px rgba(0,0,0,0.1)',
        zIndex: 101, display: 'flex', flexDirection: 'column',
      }}>
        <div style={{
          padding: '14px 16px', borderBottom: '1px solid var(--mb-divider)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--mb-text-primary)' }}>
              {stage.label}
            </div>
            <div style={{ fontSize: 11, color: 'var(--mb-text-tertiary)', marginTop: 2 }}>
              {inProgressLabel} <span style={{ color: accent, fontWeight: 600 }}>{stage.in_progress_count}</span>
              {showPending && (
                <>
                  {' · '}
                  {pendingLabel} <span style={{ color: 'var(--mb-text-primary)', fontWeight: 600 }}>{stage.pending_count}</span>
                </>
              )}
              {' · '}
              今日 {stage.completed_today}
            </div>
          </div>
          <button
            onClick={onClose}
            style={{
              border: 'none', background: 'transparent', fontSize: 18, cursor: 'pointer',
              color: 'var(--mb-text-tertiary)', lineHeight: 1, padding: 4,
            }}
          >×</button>
        </div>

        <div style={{ flex: 1, overflow: 'auto', padding: '8px 16px 16px' }}>
          <Section
            title={inProgressLabel}
            total={stage.in_progress_count}
            items={stage.in_progress_items}
            onItemClick={onItemClick}
            nowMs={nowMs}
            emptyHint={inProgressLabel === '生成中' ? '当前没有正在生成的条目' : '当前没有正在提炼的条目'}
            accent={accent}
          />
          {showPending && (
            <Section
              title={pendingLabel}
              total={stage.pending_count}
              items={stage.pending_items}
              onItemClick={onItemClick}
              nowMs={nowMs}
              emptyHint={`${pendingLabel}为空`}
              accent={accent}
            />
          )}
        </div>
      </div>
    </>
  )
}

const Section: React.FC<{
  title: string
  total: number
  items: DagItem[]
  onItemClick: (item: DagItem) => void
  nowMs: number
  emptyHint: string
  accent: string
}> = ({ title, total, items, onItemClick, nowMs, emptyHint, accent }) => (
  <div style={{ marginTop: 12 }}>
    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--mb-text-secondary)', marginBottom: 8 }}>
      {title} <span style={{ color: accent }}>({total})</span>
      {total > items.length && (
        <span style={{ marginLeft: 6, fontWeight: 400, color: 'var(--mb-text-faint)' }}>
          仅显示前 {items.length} 条
        </span>
      )}
    </div>
    {items.length === 0 ? (
      <div style={{ fontSize: 12, color: 'var(--mb-text-faint)', padding: '8px 0' }}>{emptyHint}</div>
    ) : (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {items.map((item) => (
          <button
            key={`${item.kind}-${item.id}`}
            onClick={() => onItemClick(item)}
            style={{
              textAlign: 'left', background: 'var(--mb-bg-page)', border: 'none',
              borderRadius: 8, padding: '8px 10px', cursor: 'pointer',
              display: 'flex', flexDirection: 'column', gap: 2,
            }}
          >
            <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--mb-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {item.title}
            </div>
            <div style={{ fontSize: 10, color: 'var(--mb-text-tertiary)', display: 'flex', gap: 8 }}>
              {item.subtitle && <span>{item.subtitle}</span>}
              <span style={{ marginLeft: 'auto' }}>
                {item.started_at_ms
                  ? `已提炼 ${Math.floor(Math.max(0, nowMs - item.started_at_ms) / 1000)}s`
                  : item.ts > 0 ? fmtRelTime(item.ts, nowMs) : ''}
              </span>
            </div>
          </button>
        ))}
      </div>
    )}
  </div>
)

export default PipelineDagPanel
