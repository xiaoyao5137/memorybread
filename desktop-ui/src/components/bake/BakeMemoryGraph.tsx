import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Focus, Maximize2, Minimize2, Network, RotateCcw, Search, X } from 'lucide-react'
import {
  buildMemoryGraph,
  createMemoryGraphLayout,
  memoryGraphKindMeta,
  memoryGraphRelationMeta,
  scopeMemoryGraphToDateRange,
  sliceMemoryGraph,
  type MemoryGraphAssets,
  type MemoryGraphEdge,
  type MemoryGraphNode,
  type MemoryGraphNodeKind,
  type MemoryGraphPosition,
} from './memoryGraph'
import TutorialLink, { TUTORIAL_URLS } from '../TutorialLink'

const VIEWBOX_WIDTH = 1000
const VIEWBOX_HEIGHT = 620
const VIEWBOX_MIN_WIDTH = 480
const VIEWBOX_MIN_HEIGHT = 380

const allKinds: MemoryGraphNodeKind[] = ['knowledge', 'document', 'operation', 'data']

const nodeShape = (node: MemoryGraphNode, radius: number) => {
  const meta = memoryGraphKindMeta[node.kind]
  if (meta.shape === 'rounded') {
    return <rect x={-radius} y={-radius * 0.82} width={radius * 2} height={radius * 1.64} rx={8} />
  }
  if (meta.shape === 'diamond') {
    return <path d={`M 0 ${-radius} L ${radius} 0 L 0 ${radius} L ${-radius} 0 Z`} />
  }
  if (meta.shape === 'hexagon') {
    return <path d={`M ${-radius * 0.86} ${-radius * 0.5} L 0 ${-radius} L ${radius * 0.86} ${-radius * 0.5} L ${radius * 0.86} ${radius * 0.5} L 0 ${radius} L ${-radius * 0.86} ${radius * 0.5} Z`} />
  }
  return <circle r={radius} />
}

const relationAriaLabel = (edge: MemoryGraphEdge, nodeById: Map<string, MemoryGraphNode>) => {
  const source = nodeById.get(edge.source)?.label ?? edge.source
  const target = nodeById.get(edge.target)?.label ?? edge.target
  return `${source} 与 ${target}：${memoryGraphRelationMeta[edge.relationType].label}，${edge.evidence}`
}

const clampScale = (scale: number) => Math.min(2.8, Math.max(0.45, scale))

export const getMemoryGraphWheelScale = (currentScale: number, deltaY: number) => (
  clampScale(currentScale * Math.exp(-deltaY * 0.003))
)

// 根据节点包围圈计算“铺满画布”的初始变换，避免窄列中图谱被整体缩小到不可读。
const computeFitTransform = (
  positions: Record<string, MemoryGraphPosition>,
  viewWidth: number,
  viewHeight: number,
) => {
  const points = Object.values(positions)
  if (points.length === 0) return { x: 0, y: 0, scale: 1 }
  const padding = 86
  const minX = Math.min(...points.map(point => point.x))
  const maxX = Math.max(...points.map(point => point.x))
  const minY = Math.min(...points.map(point => point.y))
  const maxY = Math.max(...points.map(point => point.y))
  const contentWidth = Math.max(120, maxX - minX + padding * 2)
  const contentHeight = Math.max(120, maxY - minY + padding * 2)
  // 适配变换单独限幅：上限 1.6 避免少量节点被过度放大，下限 0.45 保留手动缩放空间。
  const scale = Math.min(1.6, Math.max(0.45, Math.min(viewWidth / contentWidth, viewHeight / contentHeight)))
  const centerX = (minX + maxX) / 2
  const centerY = (minY + maxY) / 2
  return {
    x: viewWidth / 2 - centerX * scale,
    y: viewHeight / 2 - centerY * scale,
    scale,
  }
}

// 估算标签宽度（CJK 按字号等宽，ASCII 按 0.62 估），用于重叠检测。
const estimateLabelWidth = (text: string) => Array.from(text).reduce(
  (width, char) => width + (/[\u2E80-\u9FFF\uFF00-\uFFEF\u3000-\u303F]/.test(char) ? 10 : 6.2),
  8,
)

type LabelRect = { left: number; right: number; top: number; bottom: number }

const rectsOverlap = (a: LabelRect, b: LabelRect) => (
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
)

const normalizeSearchText = (value: string) => value
  .normalize('NFKC')
  .toLocaleLowerCase()
  .replace(/\s+/g, '')

const nodeMatchesSearch = (node: MemoryGraphNode, query: string) => normalizeSearchText([
  node.label,
  node.summary,
  ...node.concepts,
].join(' ')).includes(query)

const BakeMemoryGraph: React.FC<{
  assets: MemoryGraphAssets
  focusNodeId?: string | null
  loading?: boolean
  error?: string | null
  mode?: 'overview' | 'dock'
  onRetry?: () => void
  onClose?: () => void
  onOpenNode?: (node: MemoryGraphNode) => void
  onSearchAssets?: (query: string) => Promise<MemoryGraphAssets>
  defaultDateRange?: { fromMs: number; toMs: number }
  defaultScopeLabel?: string
}> = ({
  assets,
  focusNodeId,
  loading = false,
  error,
  mode = 'overview',
  onRetry,
  onClose,
  onOpenNode,
  onSearchAssets,
  defaultDateRange,
  defaultScopeLabel,
}) => {
  const [fullscreen, setFullscreen] = useState(false)
  const [enabledKinds, setEnabledKinds] = useState<Set<MemoryGraphNodeKind>>(() => new Set(allKinds))
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(focusNodeId ?? null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchedAssets, setSearchedAssets] = useState<MemoryGraphAssets | null>(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 })
  const [positions, setPositions] = useState<Record<string, MemoryGraphPosition>>({})
  const [viewBox, setViewBox] = useState({ width: VIEWBOX_WIDTH, height: VIEWBOX_HEIGHT })
  const svgRef = useRef<SVGSVGElement | null>(null)
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const fullscreenButtonRef = useRef<HTMLButtonElement | null>(null)
  const closeButtonRef = useRef<HTMLButtonElement | null>(null)
  const searchRequestRef = useRef(0)
  const gestureRef = useRef<{
    kind: 'pan' | 'node'
    pointerId: number
    nodeId?: string
    startClientX: number
    startClientY: number
    startX: number
    startY: number
  } | null>(null)

  const normalizedSearchQuery = useMemo(() => normalizeSearchText(searchQuery), [searchQuery])
  const completeGraph = useMemo(() => buildMemoryGraph(searchedAssets ?? assets), [assets, searchedAssets])
  const graph = useMemo(() => (
    defaultDateRange && !normalizedSearchQuery
      ? scopeMemoryGraphToDateRange(completeGraph, defaultDateRange)
      : completeGraph
  ), [completeGraph, defaultDateRange, normalizedSearchQuery])
  const searchMatchIds = useMemo(() => new Set(
    normalizedSearchQuery
      ? graph.nodes
        .filter(node => searchedAssets !== null || nodeMatchesSearch(node, normalizedSearchQuery))
        .map(node => node.id)
      : [],
  ), [graph.nodes, normalizedSearchQuery, searchedAssets])
  const searchedGraph = useMemo(() => {
    if (!normalizedSearchQuery) return graph
    const includedNodeIds = new Set(searchMatchIds)
    graph.edges.forEach(edge => {
      if (searchMatchIds.has(edge.source) || searchMatchIds.has(edge.target)) {
        includedNodeIds.add(edge.source)
        includedNodeIds.add(edge.target)
      }
    })
    const nodes = graph.nodes.filter(node => includedNodeIds.has(node.id))
    const edges = graph.edges.filter(edge => includedNodeIds.has(edge.source) && includedNodeIds.has(edge.target))
    return {
      ...graph,
      nodes,
      edges,
      eligibleNodeCount: nodes.length,
    }
  }, [graph, normalizedSearchQuery, searchMatchIds])
  const slicedGraph = useMemo(() => sliceMemoryGraph(searchedGraph, {
    focusNodeId,
    maxNodes: fullscreen ? 88 : mode === 'overview' ? 64 : 48,
    preferHeat: mode === 'overview',
  }), [focusNodeId, fullscreen, mode, searchedGraph])
  const visibleNodes = useMemo(() => slicedGraph.nodes.filter(node => enabledKinds.has(node.kind)), [enabledKinds, slicedGraph.nodes])
  const visibleNodeIds = useMemo(() => new Set(visibleNodes.map(node => node.id)), [visibleNodes])
  const visibleEdges = useMemo(() => slicedGraph.edges.filter(edge => (
    visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)
  )), [slicedGraph.edges, visibleNodeIds])
  const nodeById = useMemo(() => new Map(visibleNodes.map(node => [node.id, node])), [visibleNodes])
  const edgeById = useMemo(() => new Map(visibleEdges.map(edge => [edge.id, edge])), [visibleEdges])
  const selectedNode = selectedNodeId ? nodeById.get(selectedNodeId) ?? null : null
  const selectedEdge = selectedEdgeId ? edgeById.get(selectedEdgeId) ?? null : null
  const neighborIds = useMemo(() => {
    if (!selectedNodeId) return new Set<string>()
    const neighbors = new Set<string>([selectedNodeId])
    visibleEdges.forEach(edge => {
      if (edge.source === selectedNodeId) neighbors.add(edge.target)
      if (edge.target === selectedNodeId) neighbors.add(edge.source)
    })
    return neighbors
  }, [selectedNodeId, visibleEdges])
  const kindCounts = useMemo(() => allKinds.reduce<Record<MemoryGraphNodeKind, number>>((counts, kind) => {
    counts[kind] = slicedGraph.nodes.filter(node => node.kind === kind).length
    return counts
  }, { knowledge: 0, document: 0, operation: 0, data: 0 }), [slicedGraph.nodes])

  // 标签互相压盖或与节点形状重叠时，优先保留选中/焦点/邻居节点的标签。
  const hiddenLabelIds = useMemo(() => {
    const hidden = new Set<string>()
    if (visibleNodes.length === 0) return hidden
    const occupied: LabelRect[] = visibleNodes.reduce<LabelRect[]>((rects, node) => {
      const position = positions[node.id]
      if (position) rects.push({ left: position.x - 17, right: position.x + 17, top: position.y - 17, bottom: position.y + 17 })
      return rects
    }, [])
    const priority = (node: MemoryGraphNode) => {
      if (node.id === selectedNodeId || node.id === focusNodeId) return 0
      return neighborIds.has(node.id) ? 1 : 2
    }
    const ordered = [...visibleNodes].sort((a, b) => (
      priority(a) - priority(b)
      || b.heatScore - a.heatScore
      || a.id.localeCompare(b.id)
    ))
    ordered.forEach(node => {
      const position = positions[node.id]
      if (!position) return
      const label = node.label.length > 12 ? `${node.label.slice(0, 12)}…` : node.label
      const width = estimateLabelWidth(label)
      const radius = node.id === focusNodeId ? 19 : node.id === selectedNodeId ? 18 : 15
      const rect: LabelRect = {
        left: position.x - width / 2,
        right: position.x + width / 2,
        top: position.y + radius + 5,
        bottom: position.y + radius + 25,
      }
      if (occupied.some(other => rectsOverlap(rect, other))) {
        hidden.add(node.id)
        return
      }
      occupied.push(rect)
    })
    return hidden
  }, [visibleNodes, positions, selectedNodeId, focusNodeId, neighborIds])

  const layoutSignature = `${slicedGraph.graphVersion}:${visibleNodes.map(node => node.id).join(',')}:${focusNodeId ?? ''}:${fullscreen}:${normalizedSearchQuery}:${viewBox.width}x${viewBox.height}`

  // viewBox 跟随画布容器宽高比，避免固定 1000x620 在窄列 dock 中被等比压缩到不可读。
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(entries => {
      const rect = entries[entries.length - 1]?.contentRect
      if (!rect || rect.width < 120 || rect.height < 120) return
      const aspect = rect.width / rect.height
      setViewBox(current => {
        const width = Math.round(Math.min(1600, Math.max(VIEWBOX_MIN_WIDTH, rect.width)))
        const height = Math.round(Math.min(1400, Math.max(VIEWBOX_MIN_HEIGHT, width / aspect)))
        if (current.width === width && current.height === height) return current
        return { width, height }
      })
    })
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const nextPositions = createMemoryGraphLayout(visibleNodes, visibleEdges, {
      width: viewBox.width,
      height: viewBox.height,
      focusNodeId,
    })
    setPositions(nextPositions)
    // 初始视图自动铺满画布，替代原先恒定的 identity 变换。
    setTransform(computeFitTransform(nextPositions, viewBox.width, viewBox.height))
  // layoutSignature intentionally captures the stable graph membership and focus.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutSignature])

  useEffect(() => {
    if (focusNodeId && slicedGraph.nodes.some(node => node.id === focusNodeId)) {
      setSelectedNodeId(focusNodeId)
      setSelectedEdgeId(null)
    }
  }, [focusNodeId, slicedGraph.nodes])

  useEffect(() => {
    if (!normalizedSearchQuery) return
    const firstMatch = slicedGraph.nodes.find(node => searchMatchIds.has(node.id))
    setSelectedNodeId(firstMatch?.id ?? null)
    setSelectedEdgeId(null)
  }, [normalizedSearchQuery, searchMatchIds, slicedGraph.nodes])

  useEffect(() => {
    const requestId = searchRequestRef.current + 1
    searchRequestRef.current = requestId
    setSearchedAssets(null)
    if (mode !== 'overview' || !normalizedSearchQuery || !onSearchAssets) {
      setSearchLoading(false)
      return undefined
    }

    setSearchLoading(true)
    const timer = window.setTimeout(() => {
      void onSearchAssets(searchQuery.trim())
        .then((nextAssets) => {
          if (searchRequestRef.current === requestId) setSearchedAssets(nextAssets)
        })
        .catch(() => {
          // 本地候选仍可搜索；远端搜索失败时保留即时筛选结果。
        })
        .finally(() => {
          if (searchRequestRef.current === requestId) setSearchLoading(false)
        })
    }, 220)

    return () => window.clearTimeout(timer)
  }, [mode, normalizedSearchQuery, onSearchAssets, searchQuery])

  useEffect(() => {
    if (mode !== 'dock') return undefined
    if (!fullscreen) closeButtonRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || fullscreen) return
      event.preventDefault()
      onClose?.()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [fullscreen, mode, onClose])

  useEffect(() => {
    if (!fullscreen) return undefined
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setFullscreen(false)
      window.requestAnimationFrame(() => fullscreenButtonRef.current?.focus())
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [fullscreen])

  const handlePointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const gesture = gestureRef.current
    if (!gesture || gesture.pointerId !== event.pointerId) return
    const dx = ((event.clientX - gesture.startClientX) / Math.max(1, svgRef.current?.getBoundingClientRect().width ?? 1)) * viewBox.width
    const dy = ((event.clientY - gesture.startClientY) / Math.max(1, svgRef.current?.getBoundingClientRect().height ?? 1)) * viewBox.height
    if (gesture.kind === 'pan') {
      setTransform(current => ({ ...current, x: gesture.startX + dx, y: gesture.startY + dy }))
      return
    }
    if (gesture.nodeId) {
      setPositions(current => ({
        ...current,
        [gesture.nodeId!]: {
          x: gesture.startX + dx / transform.scale,
          y: gesture.startY + dy / transform.scale,
        },
      }))
    }
  }

  const endGesture = (event: React.PointerEvent<SVGSVGElement>) => {
    if (gestureRef.current?.pointerId !== event.pointerId) return
    gestureRef.current = null
    if (svgRef.current?.hasPointerCapture(event.pointerId)) svgRef.current.releasePointerCapture(event.pointerId)
  }

  // 缩放改用画布容器上的原生非 passive wheel 监听（见下方 useEffect），
  // React 合成 onWheel 无法有效 preventDefault，会导致缩放与页面滚动同时发生。

  // 嵌入页面时普通滚轮不拦截，避免路过图谱导致页面滚动与图谱缩放同时发生；
  // 只有显式缩放意图（Ctrl/⌘+滚轮或触控板捏合）或全屏时才缩放。
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined
    const handleNativeWheel = (event: WheelEvent) => {
      const zoomIntent = fullscreen || event.ctrlKey || event.metaKey
      if (!zoomIntent) return
      if (!svgRef.current) return
      event.preventDefault()
      event.stopPropagation()
      const rect = svgRef.current.getBoundingClientRect()
      if (rect.width <= 0 || rect.height <= 0) return
      const point = {
        x: ((event.clientX - rect.left) / rect.width) * viewBox.width,
        y: ((event.clientY - rect.top) / rect.height) * viewBox.height,
      }
      setTransform(current => {
        const nextScale = getMemoryGraphWheelScale(current.scale, event.deltaY)
        const worldX = (point.x - current.x) / current.scale
        const worldY = (point.y - current.y) / current.scale
        return {
          scale: nextScale,
          x: point.x - worldX * nextScale,
          y: point.y - worldY * nextScale,
        }
      })
    }
    canvas.addEventListener('wheel', handleNativeWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', handleNativeWheel)
  }, [fullscreen, viewBox.height, viewBox.width])

  const toggleKind = (kind: MemoryGraphNodeKind) => {
    setEnabledKinds(current => {
      const next = new Set(current)
      if (next.has(kind) && next.size > 1) next.delete(kind)
      else next.add(kind)
      return next
    })
  }

  const resetView = () => {
    const nextPositions = createMemoryGraphLayout(visibleNodes, visibleEdges, {
      width: viewBox.width,
      height: viewBox.height,
      focusNodeId: selectedNodeId ?? focusNodeId,
    })
    setPositions(nextPositions)
    setTransform(computeFitTransform(nextPositions, viewBox.width, viewBox.height))
  }

  const graphClassName = [
    'bake-memory-graph',
    `bake-memory-graph--${mode}`,
    fullscreen ? 'bake-memory-graph--fullscreen' : '',
  ].filter(Boolean).join(' ')

  const graphContent = (
    <section
      className={graphClassName}
      aria-label="记忆图谱"
      role={fullscreen || mode === 'dock' ? 'dialog' : 'region'}
      aria-modal={fullscreen || mode === 'dock' || undefined}
    >
      <header className="bake-memory-graph__header">
        <div className="bake-memory-graph__heading">
          <div className="tutorial-title-row">
            <span className="bake-memory-graph__eyebrow"><Network size={14} /> 记忆图谱</span>
            <TutorialLink url={TUTORIAL_URLS.memoryGraph} />
          </div>
        </div>
        <div className="bake-memory-graph__actions">
          <button type="button" className="bake-graph-tool" onClick={resetView} aria-label="适配图谱画布" title="适配画布">
            <Focus size={16} />
          </button>
          <button
            ref={fullscreenButtonRef}
            type="button"
            className="bake-graph-tool"
            onClick={() => setFullscreen(current => !current)}
            aria-label={fullscreen ? '退出全屏图谱' : '全屏查看图谱'}
            title={fullscreen ? '退出全屏' : '全屏查看'}
          >
            {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
          </button>
          {mode === 'dock' && onClose && !fullscreen && (
            <button ref={closeButtonRef} type="button" className="bake-graph-tool" onClick={onClose} aria-label="关闭记忆图谱" title="关闭">
              <X size={16} />
            </button>
          )}
        </div>
      </header>

      <div className="bake-memory-graph__filters" aria-label="节点类型筛选">
        {mode === 'overview' && (
          <label className="bake-graph-search">
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="搜索记忆"
              aria-label="搜索记忆图谱"
            />
            {searchQuery && (
              <button type="button" onClick={() => setSearchQuery('')} aria-label="清空图谱搜索" title="清空搜索">
                <X size={13} />
              </button>
            )}
          </label>
        )}
        {allKinds.map(kind => {
          const meta = memoryGraphKindMeta[kind]
          const enabled = enabledKinds.has(kind)
          return (
            <button
              key={kind}
              type="button"
              className={`bake-graph-filter ${enabled ? 'bake-graph-filter--active' : ''}`.trim()}
              aria-pressed={enabled}
              onClick={() => toggleKind(kind)}
            >
              <span className="bake-graph-filter__dot" style={{ background: meta.color }} />
              {meta.label}<span className="bake-graph-filter__count">{kindCounts[kind]}</span>
            </button>
          )
        })}
        <span className="bake-memory-graph__coverage">
          {searchLoading
            ? '搜索中…'
            : normalizedSearchQuery
            ? `匹配 ${searchMatchIds.size} 项`
            : `${defaultScopeLabel ? `${defaultScopeLabel}展示` : '展示'} ${visibleNodes.length}/${slicedGraph.eligibleNodeCount}`}
        </span>
      </div>

      <div className="bake-memory-graph__canvas" ref={canvasRef}>
        {loading && visibleNodes.length === 0 ? (
          <div className="bake-memory-graph__state">
            <span className="bake-memory-graph__loader" />
            <strong>正在整理关系…</strong>
            <span>先读取本地来源与提炼语义，不上传记忆内容。</span>
          </div>
        ) : error && visibleNodes.length === 0 ? (
          <div className="bake-memory-graph__state">
            <strong>记忆图谱暂时无法加载</strong>
            <span>{error}</span>
            {onRetry && <button type="button" className="bake-graph-retry" onClick={onRetry}><RotateCcw size={14} />重新加载</button>}
          </div>
        ) : normalizedSearchQuery && searchMatchIds.size === 0 ? (
          <div className="bake-memory-graph__state">
            <strong>没有找到相关记忆</strong>
            <span>换个关键词搜索知识、文档、操作或数据。</span>
          </div>
        ) : visibleNodes.length === 0 ? (
          <div className="bake-memory-graph__state">
            <strong>{defaultScopeLabel ? `${defaultScopeLabel}还没有可连接的记忆资产` : '还没有可连接的记忆资产'}</strong>
            <span>先从时间线提炼知识、文档、操作或数据，关联会在这里出现。</span>
          </div>
        ) : (
          <svg
            ref={svgRef}
            className="bake-memory-graph__svg"
            viewBox={`0 0 ${viewBox.width} ${viewBox.height}`}
            preserveAspectRatio="xMidYMid meet"
            onPointerDown={(event) => {
              if (event.target !== event.currentTarget && (event.target as Element).closest('[data-graph-node], [data-graph-edge]')) return
              event.currentTarget.setPointerCapture(event.pointerId)
              gestureRef.current = {
                kind: 'pan',
                pointerId: event.pointerId,
                startClientX: event.clientX,
                startClientY: event.clientY,
                startX: transform.x,
                startY: transform.y,
              }
            }}
            onPointerMove={handlePointerMove}
            onPointerUp={endGesture}
            onPointerCancel={endGesture}
            onClick={(event) => {
              if ((event.target as Element).closest('[data-graph-node], [data-graph-edge]')) return
              setSelectedNodeId(focusNodeId ?? null)
              setSelectedEdgeId(null)
            }}
          >
            <defs>
              <pattern id="bake-graph-grid" width="28" height="28" patternUnits="userSpaceOnUse">
                <circle cx="1" cy="1" r="0.8" className="bake-memory-graph__grid-dot" />
              </pattern>
              <marker id="bake-graph-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
                <path d="M0,0 L0,6 L7,3 z" className="bake-memory-graph__arrow" />
              </marker>
            </defs>
            <rect width={viewBox.width} height={viewBox.height} fill="url(#bake-graph-grid)" />
            <g data-graph-viewport transform={`translate(${transform.x} ${transform.y}) scale(${transform.scale})`}>
              {visibleEdges.map(edge => {
                const source = positions[edge.source]
                const target = positions[edge.target]
                if (!source || !target) return null
                const selected = edge.id === selectedEdgeId
                const connected = !selectedNodeId || edge.source === selectedNodeId || edge.target === selectedNodeId
                return (
                  <line
                    key={edge.id}
                    data-graph-edge
                    tabIndex={0}
                    className={`bake-memory-graph__edge bake-memory-graph__edge--${edge.relationType} ${selected ? 'bake-memory-graph__edge--selected' : ''} ${connected ? '' : 'bake-memory-graph__edge--muted'}`.trim()}
                    x1={source.x}
                    y1={source.y}
                    x2={target.x}
                    y2={target.y}
                    markerEnd={edge.directed ? 'url(#bake-graph-arrow)' : undefined}
                    aria-label={relationAriaLabel(edge, nodeById)}
                    onClick={(event) => {
                      event.stopPropagation()
                      setSelectedEdgeId(edge.id)
                      setSelectedNodeId(null)
                    }}
                    onFocus={() => {
                      setSelectedEdgeId(edge.id)
                      setSelectedNodeId(null)
                    }}
                  />
                )
              })}

              {visibleNodes.map(node => {
                const position = positions[node.id]
                if (!position) return null
                const meta = memoryGraphKindMeta[node.kind]
                const selected = node.id === selectedNodeId
                const focused = node.id === focusNodeId
                const searchMatched = normalizedSearchQuery ? searchMatchIds.has(node.id) : false
                const muted = selectedNodeId ? !neighborIds.has(node.id) : false
                const radius = focused ? 19 : selected ? 18 : 15
                return (
                  <g
                    key={node.id}
                    data-graph-node
                    className={`bake-memory-graph__node ${selected ? 'bake-memory-graph__node--selected' : ''} ${focused ? 'bake-memory-graph__node--focus' : ''} ${searchMatched ? 'bake-memory-graph__node--search-match' : ''} ${muted ? 'bake-memory-graph__node--muted' : ''}`.trim()}
                    transform={`translate(${position.x} ${position.y})`}
                    tabIndex={0}
                    role="button"
                    aria-label={`${meta.label}：${node.label}。按 Enter 打开`}
                    onPointerDown={(event) => {
                      event.stopPropagation()
                      svgRef.current?.setPointerCapture(event.pointerId)
                      gestureRef.current = {
                        kind: 'node',
                        pointerId: event.pointerId,
                        nodeId: node.id,
                        startClientX: event.clientX,
                        startClientY: event.clientY,
                        startX: position.x,
                        startY: position.y,
                      }
                    }}
                    onClick={(event) => {
                      event.stopPropagation()
                      setSelectedNodeId(node.id)
                      setSelectedEdgeId(null)
                    }}
                    onDoubleClick={() => onOpenNode?.(node)}
                    onFocus={() => {
                      setSelectedNodeId(node.id)
                      setSelectedEdgeId(null)
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') onOpenNode?.(node)
                    }}
                  >
                    {focused && <circle className="bake-memory-graph__focus-ring" r={28} />}
                    <g className="bake-memory-graph__node-shape" fill={meta.color}>
                      {nodeShape(node, radius)}
                    </g>
                    <text className="bake-memory-graph__node-glyph" textAnchor="middle" dominantBaseline="central">{meta.shortLabel}</text>
                    {!hiddenLabelIds.has(node.id) && (
                      <text className="bake-memory-graph__node-label" textAnchor="middle" y={radius + 17}>
                        {node.label.length > 12 ? `${node.label.slice(0, 12)}…` : node.label}
                      </text>
                    )}
                  </g>
                )
              })}
            </g>
          </svg>
        )}

        {(selectedNode || selectedEdge) && (
          <div className="bake-memory-graph__inspector" aria-live="polite">
            {selectedNode ? (
              <>
                <span className="bake-memory-graph__inspector-type">{memoryGraphKindMeta[selectedNode.kind].label}</span>
                <strong>{selectedNode.label}</strong>
                <span>{selectedNode.summary}</span>
                <small>{[
                  selectedNode.kind === 'knowledge' && selectedNode.heatScore > 0 ? `出现 ${selectedNode.heatScore} 次` : '',
                  selectedNode.concepts.length > 0 ? `主题：${selectedNode.concepts.slice(0, 4).join('、')}` : '当前关系来自共同来源或显式引用',
                ].filter(Boolean).join(' · ')}</small>
                {onOpenNode && <button type="button" onClick={() => onOpenNode(selectedNode)}>打开内容</button>}
              </>
            ) : selectedEdge ? (
              <>
                <span className="bake-memory-graph__inspector-type">{memoryGraphRelationMeta[selectedEdge.relationType].label}</span>
                <strong>{selectedEdge.evidence}</strong>
                <span>{selectedEdge.relationType === 'semantic' ? `相关强度：${selectedEdge.strength}` : '来自本地资产的可解释关联'}</span>
              </>
            ) : null}
          </div>
        )}
      </div>

      <footer className="bake-memory-graph__footer">
        <div className="bake-memory-graph__legend" aria-label="关系图例">
          <span><i className="bake-graph-line bake-graph-line--references" />引用关系</span>
          <span><i className="bake-graph-line bake-graph-line--shared" />共同来源</span>
          <span><i className="bake-graph-line bake-graph-line--semantic" />语义相关</span>
        </div>
        <span className="bake-memory-graph__hint">
          拖拽节点 · 空白处平移 · {fullscreen ? '滚动缩放' : 'Ctrl/⌘+滚动缩放'} · 双击打开
        </span>
        {slicedGraph.hiddenNodeCount > 0 && <span className="bake-memory-graph__hidden">另有 {slicedGraph.hiddenNodeCount} 项未展开</span>}
      </footer>
    </section>
  )

  if (mode !== 'dock') return graphContent

  return (
    <div
      className="bake-memory-graph-drawer-overlay"
      onMouseDown={(event) => {
        if (!fullscreen && event.target === event.currentTarget) onClose?.()
      }}
    >
      {graphContent}
    </div>
  )
}

export default BakeMemoryGraph
