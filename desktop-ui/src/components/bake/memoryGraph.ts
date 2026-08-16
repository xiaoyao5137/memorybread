import type { ArticleTemplate, BakeKnowledgeItem, DataSource, SopCandidate } from '../../types'

export type MemoryGraphNodeKind = 'knowledge' | 'document' | 'operation' | 'data'

export type MemoryGraphRelationType = 'references' | 'shared_source' | 'semantic'

export type MemoryGraphGenerationSource = 'explicit' | 'shared_source' | 'semantic_metadata'

export interface MemoryGraphNode {
  id: string
  assetId: string
  kind: MemoryGraphNodeKind
  label: string
  summary: string
  concepts: string[]
  sourceMemoryIds: string[]
  sourceCaptureIds: string[]
  heatScore: number
  createdAtMs: number
  updatedAtMs: number
}

export interface MemoryGraphEdge {
  id: string
  source: string
  target: string
  relationType: MemoryGraphRelationType
  directed: boolean
  score?: number
  strength: 'weak' | 'medium' | 'strong'
  evidence: string
  generationSource: MemoryGraphGenerationSource
}

export interface MemoryGraphAssets {
  knowledge: BakeKnowledgeItem[]
  documents: ArticleTemplate[]
  operations: SopCandidate[]
  data: DataSource[]
  totals?: Partial<Record<MemoryGraphNodeKind, number>>
}

export interface MemoryGraphData {
  nodes: MemoryGraphNode[]
  edges: MemoryGraphEdge[]
  graphVersion: string
  eligibleNodeCount: number
}

export interface MemoryGraphSlice extends MemoryGraphData {
  hiddenNodeCount: number
}

export interface MemoryGraphPosition {
  x: number
  y: number
}

const kindOrder: MemoryGraphNodeKind[] = ['knowledge', 'document', 'operation', 'data']

const genericConcepts = new Set([
  '内容',
  '信息',
  '数据',
  '知识',
  '文档',
  '操作',
  '记录',
  '报告',
  '其他',
  '整体',
  '暂无',
  '未分类',
])

const unique = <T,>(items: T[]) => Array.from(new Set(items))

const asText = (value: unknown) => (typeof value === 'string' ? value.trim() : '')

const splitConcept = (value: unknown) => {
  const text = asText(value)
  if (!text) return []
  return text
    .split(/[\s,，、/|;；:：]+/)
    .map(item => item.trim())
    .filter(item => item.length >= 2 && item.length <= 40)
}

const extractLatinConcepts = (value: string) => (
  value.match(/[A-Za-z][A-Za-z0-9_.+-]{1,30}/g) ?? []
)

const normalizeConcept = (value: string) => value
  .normalize('NFKC')
  .toLocaleLowerCase()
  .replace(/[\s\p{P}\p{S}]+/gu, '')

const toConcepts = (values: unknown[], fallbackText = '') => {
  const candidates = values.flatMap(splitConcept)
  extractLatinConcepts(fallbackText).forEach(item => candidates.push(item))
  const seen = new Set<string>()
  return candidates.filter(item => {
    const key = normalizeConcept(item)
    if (!key || key.length < 2 || genericConcepts.has(key) || seen.has(key)) return false
    seen.add(key)
    return true
  }).slice(0, 24)
}

const timestampFromText = (value?: string) => {
  if (!value) return 0
  const parsed = new Date(value).getTime()
  return Number.isFinite(parsed) ? parsed : 0
}

const dataPresentation = (source: DataSource) => {
  const structured = source.latest_snapshot?.structured_data ?? {}
  const title = asText(structured.title) || source.title || `数据 #${source.id}`
  const summary = asText(structured.summary) || source.title || '数据记录'
  const rows = Array.isArray(structured.metric_rows) ? structured.metric_rows : []
  const rowConcepts = rows.flatMap((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return []
    const row = value as Record<string, unknown>
    return [row.metric, row.dimension]
  })
  return { title, summary, rowConcepts }
}

// 网页采集的标题可能混入零宽字符或控制字符，清洗后再作标签，避免截断后只剩「…」
const cleanLabel = (value?: string) => (value ?? '')
  .replace(/[\u200B-\u200F\u2028\u2029\u202A-\u202E\u2060-\u2064\uFEFF]/g, '')
  .replace(/[\u0000-\u001F\u007F]/g, ' ')
  .replace(/\s+/g, ' ')
  .trim()

const toNodes = (assets: MemoryGraphAssets): MemoryGraphNode[] => {
  const knowledgeNodes = assets.knowledge.map<MemoryGraphNode>(item => ({
    id: `knowledge:${item.id}`,
    assetId: item.id,
    kind: 'knowledge',
    label: cleanLabel(item.summary) || `知识 #${item.id}`,
    summary: item.overview || item.details || item.summary || '知识条目',
    concepts: toConcepts([...item.entities, item.category], `${item.summary} ${item.overview ?? ''}`),
    sourceMemoryIds: item.sourceTimelineId ? [item.sourceTimelineId] : [],
    sourceCaptureIds: unique([
      ...item.sourceCaptureIds,
      ...(item.captureId ? [item.captureId] : []),
    ]),
    heatScore: Math.max(0, item.occurrenceCount),
    createdAtMs: item.createdAtMs,
    updatedAtMs: item.updatedAtMs || item.createdAtMs,
  }))

  const documentNodes = assets.documents.map<MemoryGraphNode>(item => ({
    id: `document:${item.id}`,
    assetId: item.id,
    kind: 'document',
    label: cleanLabel(item.title) || `文档 #${item.id}`,
    summary: item.summary || item.promptHint || item.title || '文档',
    concepts: toConcepts([
      ...item.tags,
      ...item.sections.flatMap(section => section.keywords),
      item.docType,
    ], `${item.title} ${item.summary ?? ''}`),
    sourceMemoryIds: unique([...item.sourceMemoryIds, ...item.sourceEpisodeIds]),
    sourceCaptureIds: unique(item.sourceCaptureIds),
    heatScore: Math.max(0, item.usageCount),
    createdAtMs: item.createdAtMs || timestampFromText(item.createdAt),
    updatedAtMs: timestampFromText(item.updatedAt) || item.createdAtMs || 0,
  }))

  const operationNodes = assets.operations.map<MemoryGraphNode>(item => ({
    id: `operation:${item.id}`,
    assetId: item.id,
    kind: 'operation',
    label: cleanLabel(item.extractedProblem) || cleanLabel(item.sourceTitle) || cleanLabel(item.steps[0]) || `操作 #${item.id}`,
    summary: item.detailedContent || item.steps.slice(0, 2).join('；') || '操作手册',
    concepts: toConcepts(item.triggerKeywords, `${item.extractedProblem ?? ''} ${item.detailedContent ?? ''}`),
    sourceMemoryIds: item.sourceTimelineId ? [item.sourceTimelineId] : [],
    sourceCaptureIds: item.sourceCaptureId ? [item.sourceCaptureId] : [],
    heatScore: 0,
    createdAtMs: item.createdAtMs || timestampFromText(item.createdAt),
    updatedAtMs: item.updatedAtMs || item.createdAtMs || timestampFromText(item.updatedAt),
  }))

  const dataNodes = assets.data.map<MemoryGraphNode>(item => {
    const presentation = dataPresentation(item)
    return {
      id: `data:${item.id}`,
      assetId: String(item.id),
      kind: 'data',
      label: cleanLabel(presentation.title),
      summary: presentation.summary,
      concepts: toConcepts([...item.tags, ...presentation.rowConcepts], `${presentation.title} ${presentation.summary}`),
      sourceMemoryIds: unique((item.latest_snapshot?.source_timeline_ids ?? []).map(String)),
      sourceCaptureIds: unique((item.latest_snapshot?.source_capture_ids ?? []).map(String)),
      heatScore: 0,
      createdAtMs: item.created_at ?? item.first_seen_at,
      updatedAtMs: item.latest_snapshot?.observed_at
        ?? item.latest_snapshot?.collected_at
        ?? item.created_at
        ?? item.first_seen_at,
    }
  })

  const byId = new Map<string, MemoryGraphNode>()
  ;[...knowledgeNodes, ...documentNodes, ...operationNodes, ...dataNodes].forEach(node => {
    byId.set(node.id, node)
  })
  return Array.from(byId.values())
}

const pairKey = (source: string, target: string) => (
  source < target ? `${source}\u0000${target}` : `${target}\u0000${source}`
)

const edgePriority: Record<MemoryGraphRelationType, number> = {
  references: 3,
  shared_source: 2,
  semantic: 1,
}

const stableHash = (value: string) => {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

const buildEdges = (
  nodes: MemoryGraphNode[],
  assets: MemoryGraphAssets,
): MemoryGraphEdge[] => {
  const nodeIds = new Set(nodes.map(node => node.id))
  const edgesByPair = new Map<string, MemoryGraphEdge>()

  const addEdge = (edge: MemoryGraphEdge) => {
    if (edge.source === edge.target || !nodeIds.has(edge.source) || !nodeIds.has(edge.target)) return
    const key = pairKey(edge.source, edge.target)
    const existing = edgesByPair.get(key)
    if (!existing || edgePriority[edge.relationType] > edgePriority[existing.relationType]) {
      edgesByPair.set(key, edge)
    }
  }

  assets.documents.forEach(document => {
    document.linkedKnowledgeIds.forEach(knowledgeId => addEdge({
      id: `references:document:${document.id}:knowledge:${knowledgeId}`,
      source: `document:${document.id}`,
      target: `knowledge:${knowledgeId}`,
      relationType: 'references',
      directed: true,
      strength: 'strong',
      evidence: '文档显式引用了这条知识',
      generationSource: 'explicit',
    }))
  })

  assets.operations.forEach(operation => {
    operation.linkedKnowledgeIds.forEach(knowledgeId => addEdge({
      id: `references:operation:${operation.id}:knowledge:${knowledgeId}`,
      source: `operation:${operation.id}`,
      target: `knowledge:${knowledgeId}`,
      relationType: 'references',
      directed: true,
      strength: 'strong',
      evidence: '操作手册显式引用了这条知识',
      generationSource: 'explicit',
    }))
  })

  const connectSharedEvidence = (field: 'sourceMemoryIds' | 'sourceCaptureIds', label: string) => {
    const sourceIndex = new Map<string, string[]>()
    nodes.forEach(node => node[field].forEach(sourceId => {
      const indexed = sourceIndex.get(sourceId) ?? []
      indexed.push(node.id)
      sourceIndex.set(sourceId, indexed)
    }))

    const sharedCount = new Map<string, number>()
    sourceIndex.forEach(indexedNodeIds => {
      const ids = unique(indexedNodeIds).slice(0, 24)
      for (let left = 0; left < ids.length; left += 1) {
        for (let right = left + 1; right < ids.length; right += 1) {
          const key = pairKey(ids[left], ids[right])
          sharedCount.set(key, (sharedCount.get(key) ?? 0) + 1)
        }
      }
    })

    sharedCount.forEach((count, key) => {
      const [source, target] = key.split('\u0000')
      addEdge({
        id: `shared:${stableHash(key)}:${field}`,
        source,
        target,
        relationType: 'shared_source',
        directed: false,
        strength: count >= 2 ? 'strong' : 'medium',
        evidence: `共同关联 ${count} 条${label}`,
        generationSource: 'shared_source',
      })
    })
  }

  connectSharedEvidence('sourceMemoryIds', '时间线证据')
  connectSharedEvidence('sourceCaptureIds', '采集证据')

  const nodeById = new Map(nodes.map(node => [node.id, node]))
  const conceptIndex = new Map<string, string[]>()
  const displayConcept = new Map<string, string>()
  nodes.forEach(node => node.concepts.forEach(concept => {
    const key = normalizeConcept(concept)
    if (!key || genericConcepts.has(key)) return
    displayConcept.set(key, displayConcept.get(key) ?? concept)
    const indexed = conceptIndex.get(key) ?? []
    indexed.push(node.id)
    conceptIndex.set(key, indexed)
  }))

  const semanticMatches = new Map<string, Set<string>>()
  conceptIndex.forEach((indexedNodeIds, concept) => {
    const ids = unique(indexedNodeIds)
    if (ids.length < 2 || ids.length > 24) return
    for (let left = 0; left < ids.length; left += 1) {
      for (let right = left + 1; right < ids.length; right += 1) {
        const key = pairKey(ids[left], ids[right])
        const matches = semanticMatches.get(key) ?? new Set<string>()
        matches.add(concept)
        semanticMatches.set(key, matches)
      }
    }
  })

  const semanticCandidates = Array.from(semanticMatches.entries()).flatMap(([key, matches]) => {
    if (edgesByPair.has(key)) return []
    const [source, target] = key.split('\u0000')
    const sourceNode = nodeById.get(source)
    const targetNode = nodeById.get(target)
    if (!sourceNode || !targetNode) return []
    const denominator = Math.sqrt(Math.max(1, sourceNode.concepts.length * targetNode.concepts.length))
    const score = Math.min(1, (matches.size / denominator) + Math.min(0.3, matches.size * 0.12))
    if (score < 0.28) return []
    const concepts = Array.from(matches).slice(0, 3).map(item => displayConcept.get(item) ?? item)
    return [{ key, source, target, score, concepts }]
  }).sort((left, right) => right.score - left.score || left.key.localeCompare(right.key))

  const semanticDegree = new Map<string, number>()
  semanticCandidates.forEach(candidate => {
    if ((semanticDegree.get(candidate.source) ?? 0) >= 4 || (semanticDegree.get(candidate.target) ?? 0) >= 4) return
    semanticDegree.set(candidate.source, (semanticDegree.get(candidate.source) ?? 0) + 1)
    semanticDegree.set(candidate.target, (semanticDegree.get(candidate.target) ?? 0) + 1)
    addEdge({
      id: `semantic:${stableHash(candidate.key)}`,
      source: candidate.source,
      target: candidate.target,
      relationType: 'semantic',
      directed: false,
      score: candidate.score,
      strength: candidate.score >= 0.66 ? 'strong' : candidate.score >= 0.42 ? 'medium' : 'weak',
      evidence: `共同涉及 ${candidate.concepts.join('、')}`,
      generationSource: 'semantic_metadata',
    })
  })

  return Array.from(edgesByPair.values()).sort((left, right) => (
    edgePriority[right.relationType] - edgePriority[left.relationType]
    || (right.score ?? 1) - (left.score ?? 1)
    || left.id.localeCompare(right.id)
  ))
}

export const buildMemoryGraph = (assets: MemoryGraphAssets): MemoryGraphData => {
  const nodes = toNodes(assets)
  const edges = buildEdges(nodes, assets)
  const loadedTotals: Record<MemoryGraphNodeKind, number> = {
    knowledge: assets.knowledge.length,
    document: assets.documents.length,
    operation: assets.operations.length,
    data: assets.data.length,
  }
  const eligibleNodeCount = kindOrder.reduce(
    (sum, kind) => sum + Math.max(loadedTotals[kind], assets.totals?.[kind] ?? 0),
    0,
  )
  const signature = [
    ...nodes.map(node => `${node.id}:${node.updatedAtMs}`),
    ...edges.map(edge => edge.id),
  ].sort().join('|')
  return {
    nodes,
    edges,
    graphVersion: stableHash(signature),
    eligibleNodeCount,
  }
}

export const scopeMemoryGraphToDateRange = (
  graph: MemoryGraphData,
  range: { fromMs: number; toMs: number },
): MemoryGraphData => {
  const seedNodeIds = new Set(graph.nodes
    .filter(node => node.createdAtMs >= range.fromMs && node.createdAtMs <= range.toMs)
    .map(node => node.id))
  const includedNodeIds = new Set(seedNodeIds)

  graph.edges.forEach(edge => {
    if (seedNodeIds.has(edge.source) || seedNodeIds.has(edge.target)) {
      includedNodeIds.add(edge.source)
      includedNodeIds.add(edge.target)
    }
  })

  const nodes = graph.nodes.filter(node => includedNodeIds.has(node.id))
  const edges = graph.edges.filter(edge => (
    includedNodeIds.has(edge.source) && includedNodeIds.has(edge.target)
  ))
  return {
    ...graph,
    nodes,
    edges,
    eligibleNodeCount: nodes.length,
  }
}

const weightedDegree = (nodeId: string, edges: MemoryGraphEdge[]) => edges.reduce((score, edge) => {
  if (edge.source !== nodeId && edge.target !== nodeId) return score
  return score + edgePriority[edge.relationType] + (edge.score ?? 0)
}, 0)

export const sliceMemoryGraph = (
  graph: MemoryGraphData,
  options: { focusNodeId?: string | null; maxNodes?: number; preferHeat?: boolean } = {},
): MemoryGraphSlice => {
  const maxNodes = Math.max(1, options.maxNodes ?? 64)
  if (graph.nodes.length <= maxNodes) {
    return { ...graph, hiddenNodeCount: Math.max(0, graph.eligibleNodeCount - graph.nodes.length) }
  }

  const selected = new Set<string>()
  const nodeById = new Map(graph.nodes.map(node => [node.id, node]))
  const focusNodeId = options.focusNodeId && nodeById.has(options.focusNodeId)
    ? options.focusNodeId
    : null

  if (focusNodeId) {
    selected.add(focusNodeId)
    const directEdges = graph.edges
      .filter(edge => edge.source === focusNodeId || edge.target === focusNodeId)
      .sort((left, right) => (
        edgePriority[right.relationType] - edgePriority[left.relationType]
        || (right.score ?? 1) - (left.score ?? 1)
      ))
    const directNodeIds = new Set<string>([focusNodeId])
    directEdges.forEach(edge => directNodeIds.add(edge.source === focusNodeId ? edge.target : edge.source))

    const secondHopEdges = graph.edges
      .filter(edge => (
        (directNodeIds.has(edge.source) || directNodeIds.has(edge.target))
        && edge.source !== focusNodeId
        && edge.target !== focusNodeId
      ))
      .sort((left, right) => (
        edgePriority[right.relationType] - edgePriority[left.relationType]
        || (right.score ?? 1) - (left.score ?? 1)
      ))
    const connectedNodeIds = new Set<string>(directNodeIds)
    secondHopEdges.forEach(edge => {
      connectedNodeIds.add(edge.source)
      connectedNodeIds.add(edge.target)
    })

    directEdges.forEach(edge => {
      if (selected.size >= maxNodes) return
      selected.add(edge.source)
      if (selected.size < maxNodes) selected.add(edge.target)
    })
    secondHopEdges.forEach(edge => {
      if (selected.size >= maxNodes) return
      if (selected.has(edge.source)) selected.add(edge.target)
      else if (selected.has(edge.target)) selected.add(edge.source)
    })

    const nodes = graph.nodes.filter(node => selected.has(node.id))
    const edges = graph.edges.filter(edge => selected.has(edge.source) && selected.has(edge.target))
    return {
      ...graph,
      nodes,
      edges,
      eligibleNodeCount: connectedNodeIds.size,
      hiddenNodeCount: Math.max(0, connectedNodeIds.size - nodes.length),
    }
  }

  const ranked = [...graph.nodes].sort((left, right) => (
    (options.preferHeat ? right.heatScore - left.heatScore : 0)
    || weightedDegree(right.id, graph.edges) - weightedDegree(left.id, graph.edges)
    || right.updatedAtMs - left.updatedAtMs
    || left.id.localeCompare(right.id)
  ))

  const minimumPerKind = Math.max(1, Math.floor(maxNodes / kindOrder.length))
  kindOrder.forEach(kind => {
    ranked.filter(node => node.kind === kind).slice(0, minimumPerKind).forEach(node => {
      if (selected.size < maxNodes) selected.add(node.id)
    })
  })
  ranked.forEach(node => {
    if (selected.size < maxNodes) selected.add(node.id)
  })

  const nodes = graph.nodes.filter(node => selected.has(node.id))
  const edges = graph.edges.filter(edge => selected.has(edge.source) && selected.has(edge.target))
  return {
    ...graph,
    nodes,
    edges,
    hiddenNodeCount: Math.max(0, graph.eligibleNodeCount - nodes.length),
  }
}

const seededUnit = (seed: string, axis: string) => {
  const numeric = Number.parseInt(stableHash(`${seed}:${axis}`), 36)
  return (numeric % 10_000) / 10_000
}

export const createMemoryGraphLayout = (
  nodes: MemoryGraphNode[],
  edges: MemoryGraphEdge[],
  options: { width?: number; height?: number; focusNodeId?: string | null } = {},
): Record<string, MemoryGraphPosition> => {
  const width = options.width ?? 1000
  const height = options.height ?? 620
  const clusterCenters: Record<MemoryGraphNodeKind, MemoryGraphPosition> = {
    knowledge: { x: width * 0.34, y: height * 0.34 },
    document: { x: width * 0.66, y: height * 0.32 },
    operation: { x: width * 0.35, y: height * 0.68 },
    data: { x: width * 0.68, y: height * 0.67 },
  }
  const positions: Record<string, MemoryGraphPosition> = {}
  const velocities: Record<string, MemoryGraphPosition> = {}

  nodes.forEach(node => {
    const center = clusterCenters[node.kind]
    positions[node.id] = {
      x: center.x + (seededUnit(node.id, 'x') - 0.5) * width * 0.27,
      y: center.y + (seededUnit(node.id, 'y') - 0.5) * height * 0.3,
    }
    velocities[node.id] = { x: 0, y: 0 }
  })

  for (let iteration = 0; iteration < 130; iteration += 1) {
    for (let left = 0; left < nodes.length; left += 1) {
      for (let right = left + 1; right < nodes.length; right += 1) {
        const a = positions[nodes[left].id]
        const b = positions[nodes[right].id]
        let dx = a.x - b.x
        let dy = a.y - b.y
        const distanceSquared = Math.max(90, dx * dx + dy * dy)
        if (dx === 0 && dy === 0) {
          dx = seededUnit(nodes[left].id, nodes[right].id) - 0.5
          dy = seededUnit(nodes[right].id, nodes[left].id) - 0.5
        }
        const force = 5400 / distanceSquared
        const distance = Math.sqrt(distanceSquared)
        const fx = (dx / distance) * force
        const fy = (dy / distance) * force
        velocities[nodes[left].id].x += fx
        velocities[nodes[left].id].y += fy
        velocities[nodes[right].id].x -= fx
        velocities[nodes[right].id].y -= fy
      }
    }

    edges.forEach(edge => {
      const source = positions[edge.source]
      const target = positions[edge.target]
      if (!source || !target) return
      const dx = target.x - source.x
      const dy = target.y - source.y
      const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy))
      const ideal = edge.relationType === 'semantic' ? 125 : 105
      const spring = (distance - ideal) * (edge.relationType === 'semantic' ? 0.006 : 0.009)
      const fx = (dx / distance) * spring
      const fy = (dy / distance) * spring
      velocities[edge.source].x += fx
      velocities[edge.source].y += fy
      velocities[edge.target].x -= fx
      velocities[edge.target].y -= fy
    })

    nodes.forEach(node => {
      const position = positions[node.id]
      const velocity = velocities[node.id]
      const cluster = clusterCenters[node.kind]
      velocity.x += (cluster.x - position.x) * 0.0035
      velocity.y += (cluster.y - position.y) * 0.0035
      velocity.x += (width / 2 - position.x) * 0.0008
      velocity.y += (height / 2 - position.y) * 0.0008
      velocity.x *= 0.72
      velocity.y *= 0.72
      position.x = Math.min(width - 58, Math.max(58, position.x + velocity.x))
      position.y = Math.min(height - 42, Math.max(42, position.y + velocity.y))
    })
  }

  const focusPosition = options.focusNodeId ? positions[options.focusNodeId] : null
  if (focusPosition) {
    // 只平移焦点节点周围的小范围，避免整体平移后节点被推出画布裁切不可见；
    // 全局居中交给渲染层的自动适配变换处理。
    const dx = width / 2 - focusPosition.x
    const dy = height / 2 - focusPosition.y
    const influenceRadius = Math.min(width, height) * 0.42
    nodes.forEach(node => {
      const position = positions[node.id]
      const offsetX = position.x + dx - width / 2
      const offsetY = position.y + dy - height / 2
      const distance = Math.sqrt(offsetX * offsetX + offsetY * offsetY)
      const influence = Math.max(0, 1 - distance / influenceRadius)
      if (influence <= 0) return
      position.x += dx * influence
      position.y += dy * influence
    })
  }

  // 收敛后统一夹取，确保所有节点与标签留在画布可见区内。
  nodes.forEach(node => {
    const position = positions[node.id]
    position.x = Math.min(width - 58, Math.max(58, position.x))
    position.y = Math.min(height - 56, Math.max(42, position.y))
  })

  return positions
}

export const memoryGraphKindMeta: Record<MemoryGraphNodeKind, {
  label: string
  shortLabel: string
  color: string
  shape: 'circle' | 'rounded' | 'diamond' | 'hexagon'
}> = {
  knowledge: { label: '知识', shortLabel: '知', color: '#34C759', shape: 'circle' },
  document: { label: '文档', shortLabel: '文', color: '#FF9500', shape: 'rounded' },
  operation: { label: '操作', shortLabel: '操', color: '#AF52DE', shape: 'diamond' },
  data: { label: '数据', shortLabel: '数', color: '#FF2D55', shape: 'hexagon' },
}

export const memoryGraphRelationMeta: Record<MemoryGraphRelationType, { label: string }> = {
  references: { label: '引用关系' },
  shared_source: { label: '共同来源' },
  semantic: { label: '语义相关' },
}
