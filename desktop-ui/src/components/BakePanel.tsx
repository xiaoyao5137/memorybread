import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Network } from 'lucide-react'
import {
  useCreateBakeTemplate,
  useDeleteDataSource,
  useDeleteBakeKnowledge,
  useDeleteBakeSop,
  useDeleteBakeTemplate,
  useFetchDataSource,
  useFetchDataSources,
  useFetchBakeKnowledge,
  useFetchBakeKnowledgeDetail,
  useFetchBakeMemories,
  useFetchBakeOverview,
  useFetchBakeSop,
  useFetchBakeSops,
  useFetchBakeTemplate,
  useFetchBakeTemplates,
  useToggleBakeTemplateStatus,
  useUpdateBakeTemplate,
  useRefreshDataSource,
  useModelStatus,
} from '../hooks/useApi'
import type { BakeOverviewResponse } from '../hooks/useApi'
import { useAppStore, type BakeNavigationTarget } from '../store/useAppStore'
import { toUserFacingError } from '../utils/userFacingError'
import { listLocalCreationSkills, type CreationSkillSource, type LocalCreationSkill } from '../utils/creationSkills'
import type {
  ArticleTemplate,
  BakeKnowledgeItem,
  BakeOverview,
  BakeTab,
  DataSource,
  SopCandidate,
  TimelineItem,
} from '../types'
import BakeHeader from './bake/BakeHeader'
import BakeOverviewTab from './bake/BakeOverviewTab'
import BakeTemplatesTab from './bake/BakeTemplatesTab'
import BakeSopTab from './bake/BakeSopTab'
import BakeKnowledgeTab from './bake/BakeKnowledgeTab'
import BakeDataTab from './bake/BakeDataTab'
import BakeTabs from './bake/BakeTabs'
import BakeMemoryGraph from './bake/BakeMemoryGraph'
import type { MemoryGraphAssets, MemoryGraphNode } from './bake/memoryGraph'
import { BakeButton } from './bake/BakeShared'
import { parseDateInputToMs } from './bake/BakeCaptureTab'
import CreationSkillEditor from './CreationSkillEditor'
import { useConfirmDialog } from './useConfirmDialog'
import './bake/BakePanel.css'

const PAGE_SIZE = 20
const GRAPH_ASSET_LIMIT = 100

const emptyGraphAssets: MemoryGraphAssets = {
  knowledge: [],
  documents: [],
  operations: [],
  data: [],
  totals: {},
}

const mergeUniqueById = <T extends { id: string | number },>(primary: T[], secondary: T[]) => {
  const merged = new Map<string, T>()
  ;[...primary, ...secondary].forEach(item => merged.set(String(item.id), item))
  return Array.from(merged.values())
}

const getFallbackOffsetAfterRemoval = (currentCount: number, offset: number, limit: number) => (
  currentCount <= 1 && offset > 0 ? Math.max(0, offset - limit) : offset
)

const createDraftTemplate = (): ArticleTemplate => ({
  id: `template-draft-${Date.now()}`,
  title: '新模板',
  docType: 'article',
  status: 'draft',
  tags: [],
  applicableTasks: ['creation'],
  sourceMemoryIds: [],
  sourceCaptureIds: [],
  sourceEpisodeIds: [],
  linkedKnowledgeIds: [],
  sections: [],
  stylePhrases: [],
  replacementRules: [],
  promptHint: '',
  usageCount: 0,
  reviewStatus: 'draft',
  updatedAt: new Date().toLocaleString('zh-CN', { hour12: false }),
})

const defaultOverview: BakeOverview = {
  captureCount: 0,
  memoryCount: 0,
  dataCount: 0,
  knowledgeCount: 0,
  templateCount: 0,
  sopCount: 0,
  pendingCandidates: 0,
  recentActivities: [],
  inventoryTrend: [],
}

const mapBakeOverview = (data: BakeOverviewResponse): BakeOverview => {
  const overview = {
    captureCount: data.capture_count,
    memoryCount: data.memory_count,
    dataCount: data.data_count ?? 0,
    knowledgeCount: data.knowledge_count,
    templateCount: data.template_count,
    sopCount: data.sop_count ?? 0,
    pendingCandidates: data.pending_candidates,
    recentActivities: data.recent_activities ?? [],
  }
  const inventoryTrend = (data.inventory_trend ?? []).map(bucket => ({
    label: bucket.label,
    startTs: bucket.start_ts,
    endTs: bucket.end_ts,
    memoryCount: bucket.memory_count,
    dataCount: bucket.data_count ?? 0,
    knowledgeCount: bucket.knowledge_count,
    templateCount: bucket.template_count,
    sopCount: bucket.sop_count,
  }))

  return {
    ...overview,
    inventoryTrend,
  }
}

const BakePanel: React.FC = () => {
  const apiBaseUrl = useAppStore(state => state.apiBaseUrl)
  const {
    bakeTab,
    selectedMemoryId,
    selectedTemplateId,
    selectedSopId,
    selectedKnowledgeId,
    bakeTemplateFocusId,
    bakeKnowledgeFocusId,
    bakeSopFocusId,
    bakeKnowledgeOffset,
    bakeKnowledgeQuery,
    bakeKnowledgeFrom,
    bakeKnowledgeTo,
    bakeKnowledgeLimit,
    bakeTemplateOffset,
    bakeTemplateQuery,
    bakeTemplateFrom,
    bakeTemplateTo,
    bakeTemplateLimit,
    bakeSopOffset,
    bakeSopQuery,
    bakeSopFrom,
    bakeSopTo,
    bakeSopLimit,
    setBakeTab,
    setRepositoryTab,
    setWindowMode,
    bakeNavigationStack,
    pushBakeNavigationTarget,
    popBakeNavigationTarget,
    clearBakeNavigationStack,
    setSelectedMemoryId,
    setSelectedTemplateId,
    setSelectedSopId,
    setSelectedKnowledgeId,
    setSelectedCaptureId,
    setRepositoryMemoryFocusId,
    setBakeTemplateFocusId,
    setBakeKnowledgeFocusId,
    setBakeSopFocusId,
    setBakeKnowledgeOffset,
    setBakeKnowledgeQuery,
    setBakeKnowledgeLimit,
    setBakeTemplateOffset,
    setBakeTemplateLimit,
    setBakeSopOffset,
    setBakeSopLimit,
    setRepositoryCaptureSourceCaptureId,
    creationBackTarget,
    clearCreationBackTarget,
  } = useAppStore()

  const { status: modelStatus, ready: modelsReady, loading: modelStatusLoading } = useModelStatus()
  const fetchOverview = useFetchBakeOverview()
  const fetchKnowledge = useFetchBakeKnowledge()
  const fetchKnowledgeDetail = useFetchBakeKnowledgeDetail()
  const fetchMemories = useFetchBakeMemories()
  const deleteKnowledge = useDeleteBakeKnowledge()
  const fetchTemplates = useFetchBakeTemplates()
  const fetchTemplate = useFetchBakeTemplate()
  const createTemplate = useCreateBakeTemplate()
  const updateTemplate = useUpdateBakeTemplate()
  const toggleTemplateStatus = useToggleBakeTemplateStatus()
  const deleteTemplate = useDeleteBakeTemplate()
  const fetchSops = useFetchBakeSops()
  const fetchSop = useFetchBakeSop()
  const deleteSop = useDeleteBakeSop()
  const fetchDataSource = useFetchDataSource()
  const fetchDataSources = useFetchDataSources()
  const refreshDataSource = useRefreshDataSource()
  const deleteDataSource = useDeleteDataSource()
  const { confirm: confirmDestructive, dialog: confirmDialog } = useConfirmDialog()

  const [overview, setOverview] = useState<BakeOverview>(defaultOverview)
  const [knowledgeItems, setKnowledgeItems] = useState<BakeKnowledgeItem[]>([])
  const [knowledgeTotal, setKnowledgeTotal] = useState(0)
  const [memoryItems, setMemoryItems] = useState<TimelineItem[]>([])
  const [templates, setTemplates] = useState<ArticleTemplate[]>([])
  const [templateTotal, setTemplateTotal] = useState(0)
  const [sopCandidates, setSopCandidates] = useState<SopCandidate[]>([])
  const [sopTotal, setSopTotal] = useState(0)
  const [dataItems, setDataItems] = useState<DataSource[]>([])
  const [dataTotal, setDataTotal] = useState(0)
  const [dataQuery, setDataQuery] = useState('')
  const [draftDataQuery, setDraftDataQuery] = useState('')
  const [dataOffset, setDataOffset] = useState(0)
  const [dataLimit, setDataLimit] = useState(PAGE_SIZE)
  const [selectedDataId, setSelectedDataId] = useState<number | null>(null)
  const [dataFocusId, setDataFocusId] = useState<number | null>(null)
  const [dataLoading, setDataLoading] = useState(false)
  const [refreshingDataId, setRefreshingDataId] = useState<number | null>(null)
  const [deletingDataId, setDeletingDataId] = useState<number | null>(null)
  const [statusMessage, setStatusMessage] = useState<string | null>(null)
  const [draftKnowledgeQuery, setDraftKnowledgeQuery] = useState(bakeKnowledgeQuery)
  const [draftKnowledgeFrom, setDraftKnowledgeFrom] = useState(bakeKnowledgeFrom)
  const [draftKnowledgeTo, setDraftKnowledgeTo] = useState(bakeKnowledgeTo)
  const [draftTemplateQuery, setDraftTemplateQuery] = useState(bakeTemplateQuery)
  const [draftTemplateFrom, setDraftTemplateFrom] = useState(bakeTemplateFrom)
  const [draftTemplateTo, setDraftTemplateTo] = useState(bakeTemplateTo)
  const [draftSopQuery, setDraftSopQuery] = useState(bakeSopQuery)
  const [draftSopFrom, setDraftSopFrom] = useState(bakeSopFrom)
  const [draftSopTo, setDraftSopTo] = useState(bakeSopTo)
  const [creationSkillEditor, setCreationSkillEditor] = useState<{ source?: CreationSkillSource; initialSkill?: LocalCreationSkill } | null>(null)
  const [relatedTemplateSkills, setRelatedTemplateSkills] = useState<LocalCreationSkill[]>([])
  const [graphOpen, setGraphOpen] = useState(false)
  const [graphAssets, setGraphAssets] = useState<MemoryGraphAssets>(emptyGraphAssets)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState<string | null>(null)
  const [graphRevision, setGraphRevision] = useState(0)
  const knowledgeRequestSeqRef = useRef(0)

  const searchOverviewGraph = useCallback(async (query: string): Promise<MemoryGraphAssets> => {
    const [knowledgeResult, templatesResult, sopsResult, dataResult] = await Promise.allSettled([
      fetchKnowledge({ q: query, sort: 'heat', limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      fetchTemplates({ q: query, limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      fetchSops({ q: query, limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      fetchDataSources({ q: query, limit: GRAPH_ASSET_LIMIT, offset: 0 }),
    ])
    const failedRequests = [knowledgeResult, templatesResult, sopsResult, dataResult]
      .filter(result => result.status === 'rejected').length
    if (failedRequests === 4) throw new Error('记忆图谱搜索暂时不可用')

    return {
      knowledge: knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.items : [],
      documents: templatesResult.status === 'fulfilled' ? templatesResult.value.items : [],
      operations: sopsResult.status === 'fulfilled' ? sopsResult.value.items : [],
      data: dataResult.status === 'fulfilled' ? dataResult.value.items : [],
      totals: {
        knowledge: knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.total : 0,
        document: templatesResult.status === 'fulfilled' ? templatesResult.value.total : 0,
        operation: sopsResult.status === 'fulfilled' ? sopsResult.value.total : 0,
        data: dataResult.status === 'fulfilled' ? dataResult.value.total : 0,
      },
    }
  }, [fetchDataSources, fetchKnowledge, fetchSops, fetchTemplates])

  useEffect(() => {
    void fetchOverview().then((data) => {
      setOverview(mapBakeOverview(data))
    }).catch((error) => {
      setStatusMessage(toUserFacingError(error, '记忆数据加载失败'))
    })
  }, [fetchOverview])

  useEffect(() => {
    if (bakeTab !== 'overview') return
    let cancelled = false
    setGraphLoading(true)
    setGraphError(null)

    const loadOverviewAssets = async () => {
      const [memoriesResult, knowledgeResult, templatesResult, sopsResult, dataResult] = await Promise.allSettled([
        fetchMemories({ limit: 1, offset: 0 }),
        fetchKnowledge({ sort: 'heat', limit: GRAPH_ASSET_LIMIT, offset: 0 }),
        fetchTemplates({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
        fetchSops({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
        fetchDataSources({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      ])
      if (cancelled) return

      const knowledge = knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.items : []
      const templateItems = templatesResult.status === 'fulfilled' ? templatesResult.value.items : []
      const sops = sopsResult.status === 'fulfilled' ? sopsResult.value.items : []
      const dataItems = dataResult.status === 'fulfilled' ? dataResult.value.items : []
      const failedGraphRequests = [knowledgeResult, templatesResult, sopsResult, dataResult]
        .filter(result => result.status === 'rejected').length

      // 趋势只使用 /api/bake/overview 的全量聚合；这些列表最多返回 100 条，
      // 只能用于图谱素材和总数，不能二次推导每日产量。
      setOverview(prev => ({
        ...prev,
        memoryCount: memoriesResult.status === 'fulfilled' ? memoriesResult.value.total : prev.memoryCount,
        knowledgeCount: knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.total : prev.knowledgeCount,
        templateCount: templatesResult.status === 'fulfilled' ? templatesResult.value.total : prev.templateCount,
        sopCount: sopsResult.status === 'fulfilled' ? sopsResult.value.total : prev.sopCount,
        dataCount: dataResult.status === 'fulfilled' ? dataResult.value.total : prev.dataCount,
      }))
      setGraphAssets({
        knowledge,
        documents: templateItems,
        operations: sops,
        data: dataItems,
        totals: {
          knowledge: knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.total : knowledge.length,
          document: templatesResult.status === 'fulfilled' ? templatesResult.value.total : templateItems.length,
          operation: sopsResult.status === 'fulfilled' ? sopsResult.value.total : sops.length,
          data: dataResult.status === 'fulfilled' ? dataResult.value.total : dataItems.length,
        },
      })
      if (failedGraphRequests === 4) setGraphError('本地资产暂时无法读取，请稍后重新加载。')
    }

    void loadOverviewAssets()
      .catch(() => {
        if (!cancelled) setGraphError('本地资产暂时无法读取，请稍后重新加载。')
      })
      .finally(() => {
        if (!cancelled) setGraphLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [bakeTab, fetchDataSources, fetchKnowledge, fetchMemories, fetchSops, fetchTemplates, graphRevision])

  useEffect(() => {
    if (!graphOpen || bakeTab === 'overview') return
    let cancelled = false
    setGraphLoading(true)
    setGraphError(null)

    void Promise.allSettled([
      fetchKnowledge({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      fetchTemplates({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      fetchSops({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      fetchDataSources({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
    ]).then(([knowledgeResult, templatesResult, sopsResult, dataResult]) => {
      if (cancelled) return
      const failedRequests = [knowledgeResult, templatesResult, sopsResult, dataResult]
        .filter(result => result.status === 'rejected').length
      setGraphAssets({
        knowledge: knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.items : [],
        documents: templatesResult.status === 'fulfilled' ? templatesResult.value.items : [],
        operations: sopsResult.status === 'fulfilled' ? sopsResult.value.items : [],
        data: dataResult.status === 'fulfilled' ? dataResult.value.items : [],
        totals: {
          knowledge: knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.total : 0,
          document: templatesResult.status === 'fulfilled' ? templatesResult.value.total : 0,
          operation: sopsResult.status === 'fulfilled' ? sopsResult.value.total : 0,
          data: dataResult.status === 'fulfilled' ? dataResult.value.total : 0,
        },
      })
      if (failedRequests === 4) setGraphError('本地资产暂时无法读取，请稍后重新加载。')
    }).catch(() => {
      if (!cancelled) setGraphError('本地资产暂时无法读取，请稍后重新加载。')
    }).finally(() => {
      if (!cancelled) setGraphLoading(false)
    })

    return () => { cancelled = true }
  }, [bakeTab, fetchDataSources, fetchKnowledge, fetchSops, fetchTemplates, graphOpen, graphRevision])

  useEffect(() => {
    if (bakeTab !== 'data') return
    let cancelled = false
    setDataLoading(true)
    const request = dataFocusId !== null
      ? fetchDataSource(dataFocusId).then(item => ({ items: [item], total: 1 }))
      : fetchDataSources({
          q: dataQuery.trim() || undefined,
          limit: dataLimit,
          offset: dataOffset,
        })
    void request.then((data) => {
      if (cancelled) return
      setDataItems(data.items)
      setDataTotal(data.total)
      setSelectedDataId(current => data.items.some(item => item.id === current)
        ? current
        : (data.items[0]?.id ?? null))
    }).catch((error) => {
      if (!cancelled) setStatusMessage(toUserFacingError(error, '数据来源加载失败'))
    }).finally(() => {
      if (!cancelled) setDataLoading(false)
    })
    return () => { cancelled = true }
  }, [bakeTab, dataFocusId, dataLimit, dataOffset, dataQuery, fetchDataSource, fetchDataSources])

  useEffect(() => {
    if (!['templates', 'knowledge', 'sop'].includes(bakeTab)) return
    void fetchMemories({ limit: 1000, offset: 0 }).then((data) => {
      setMemoryItems(data.items)
    }).catch(() => {
      setMemoryItems([])
    })
  }, [bakeTab, fetchMemories])

  useEffect(() => {
    if (bakeTab !== 'knowledge') return
    if (bakeKnowledgeFocusId) {
      const requestSeq = knowledgeRequestSeqRef.current + 1
      knowledgeRequestSeqRef.current = requestSeq
      void fetchKnowledgeDetail(bakeKnowledgeFocusId).then((item) => {
        if (requestSeq !== knowledgeRequestSeqRef.current) return
        setKnowledgeItems([item])
        setKnowledgeTotal(1)
        setSelectedKnowledgeId(item.id)
      }).catch((error) => {
        if (requestSeq !== knowledgeRequestSeqRef.current) return
        setKnowledgeItems([])
        setKnowledgeTotal(0)
        setStatusMessage(toUserFacingError(error, '未找到这条知识'))
      })
      return
    }
    const requestSeq = knowledgeRequestSeqRef.current + 1
    knowledgeRequestSeqRef.current = requestSeq
    void fetchKnowledge({
      q: bakeKnowledgeQuery.trim() || undefined,
      from: parseDateInputToMs(bakeKnowledgeFrom),
      to: parseDateInputToMs(bakeKnowledgeTo, true),
      limit: bakeKnowledgeLimit,
      offset: bakeKnowledgeOffset,
    }).then((data) => {
      if (requestSeq !== knowledgeRequestSeqRef.current) return
      setKnowledgeItems(data.items)
      setKnowledgeTotal(data.total)
    }).catch((error) => {
      if (requestSeq !== knowledgeRequestSeqRef.current) return
      setStatusMessage(toUserFacingError(error, '知识加载失败'))
    })
  }, [bakeKnowledgeFocusId, bakeKnowledgeFrom, bakeKnowledgeLimit, bakeKnowledgeOffset, bakeKnowledgeQuery, bakeKnowledgeTo, bakeTab, fetchKnowledge, fetchKnowledgeDetail, setSelectedKnowledgeId])

  useEffect(() => {
    if (bakeTab !== 'templates') return
    if (bakeTemplateFocusId) {
      void fetchTemplate(bakeTemplateFocusId).then((item) => {
        setTemplates([item])
        setTemplateTotal(1)
        setSelectedTemplateId(item.id)
      }).catch((error) => {
        setTemplates([])
        setTemplateTotal(0)
        setStatusMessage(toUserFacingError(error, '未找到这份文档'))
      })
      return
    }
    void fetchTemplates({
      q: bakeTemplateQuery.trim() || undefined,
      from: parseDateInputToMs(bakeTemplateFrom),
      to: parseDateInputToMs(bakeTemplateTo, true),
      limit: bakeTemplateLimit,
      offset: bakeTemplateOffset,
    }).then((data) => {
      setTemplates(data.items)
      setTemplateTotal(data.total)
    }).catch((error) => {
      setStatusMessage(toUserFacingError(error, '文档加载失败'))
    })
  }, [bakeTab, bakeTemplateFocusId, bakeTemplateFrom, bakeTemplateLimit, bakeTemplateOffset, bakeTemplateQuery, bakeTemplateTo, fetchTemplate, fetchTemplates, setSelectedTemplateId])

  useEffect(() => {
    if (bakeTab !== 'templates' || !selectedTemplateId) return
    if (templates.some(item => item.id === selectedTemplateId)) return
    void fetchTemplate(selectedTemplateId).then((item) => {
      setTemplates(prev => [item, ...prev.filter(existing => existing.id !== item.id)])
    }).catch(() => {
      setStatusMessage(`未找到文档 #${selectedTemplateId}`)
    })
  }, [bakeTab, fetchTemplate, selectedTemplateId, templates])

  useEffect(() => {
    if (bakeTab !== 'sop') return
    if (bakeSopFocusId) {
      void fetchSop(bakeSopFocusId).then((item) => {
        setSopCandidates([item])
        setSopTotal(1)
        setSelectedSopId(item.id)
      }).catch((error) => {
        setSopCandidates([])
        setSopTotal(0)
        setStatusMessage(toUserFacingError(error, '未找到这份操作手册'))
      })
      return
    }
    void fetchSops({
      q: bakeSopQuery.trim() || undefined,
      from: parseDateInputToMs(bakeSopFrom),
      to: parseDateInputToMs(bakeSopTo, true),
      limit: bakeSopLimit,
      offset: bakeSopOffset,
    }).then((data) => {
      setSopCandidates(data.items)
      setSopTotal(data.total)
    }).catch((error) => {
      setStatusMessage(toUserFacingError(error, '操作手册加载失败'))
    })
  }, [bakeSopFocusId, bakeSopFrom, bakeSopLimit, bakeSopOffset, bakeSopQuery, bakeSopTo, bakeTab, fetchSop, fetchSops, setSelectedSopId])

  useEffect(() => {
    if (!statusMessage) return
    const timer = window.setTimeout(() => setStatusMessage(null), 2400)
    return () => window.clearTimeout(timer)
  }, [statusMessage])

  useEffect(() => {
    setDraftKnowledgeQuery(bakeKnowledgeQuery)
  }, [bakeKnowledgeQuery])

  useEffect(() => {
    setDraftKnowledgeFrom(bakeKnowledgeFrom)
  }, [bakeKnowledgeFrom])

  useEffect(() => {
    setDraftKnowledgeTo(bakeKnowledgeTo)
  }, [bakeKnowledgeTo])

  useEffect(() => {
    setDraftTemplateQuery(bakeTemplateQuery)
  }, [bakeTemplateQuery])

  useEffect(() => {
    setDraftTemplateFrom(bakeTemplateFrom)
  }, [bakeTemplateFrom])

  useEffect(() => {
    setDraftTemplateTo(bakeTemplateTo)
  }, [bakeTemplateTo])

  useEffect(() => {
    setDraftSopQuery(bakeSopQuery)
  }, [bakeSopQuery])

  useEffect(() => {
    setDraftSopFrom(bakeSopFrom)
  }, [bakeSopFrom])

  useEffect(() => {
    setDraftSopTo(bakeSopTo)
  }, [bakeSopTo])

  const resolvedTemplateId = selectedTemplateId ?? templates[0]?.id ?? null
  const resolvedSopId = selectedSopId ?? sopCandidates[0]?.id ?? null
  const resolvedKnowledgeId = selectedKnowledgeId ?? knowledgeItems[0]?.id ?? null
  const resolvedKnowledgeItem = knowledgeItems.find(item => item.id === resolvedKnowledgeId)
  const resolvedSopItem = sopCandidates.find(item => item.id === resolvedSopId)
  const memoryTitleById = useMemo(() => new Map(memoryItems.map(item => [item.id, item.title])), [memoryItems])
  const graphAssetsForRender = useMemo<MemoryGraphAssets>(() => ({
    knowledge: mergeUniqueById(graphAssets.knowledge, knowledgeItems),
    documents: mergeUniqueById(graphAssets.documents, templates),
    operations: mergeUniqueById(graphAssets.operations, sopCandidates),
    data: mergeUniqueById(graphAssets.data, dataItems),
    totals: {
      knowledge: Math.max(graphAssets.totals?.knowledge ?? 0, knowledgeTotal),
      document: Math.max(graphAssets.totals?.document ?? 0, templateTotal),
      operation: Math.max(graphAssets.totals?.operation ?? 0, sopTotal),
      data: Math.max(graphAssets.totals?.data ?? 0, dataTotal),
    },
  }), [
    dataItems,
    dataTotal,
    graphAssets,
    knowledgeItems,
    knowledgeTotal,
    sopCandidates,
    sopTotal,
    templateTotal,
    templates,
  ])
  const graphFocusNodeId = bakeTab === 'knowledge' && resolvedKnowledgeId
    ? `knowledge:${resolvedKnowledgeId}`
    : bakeTab === 'templates' && resolvedTemplateId
      ? `document:${resolvedTemplateId}`
      : bakeTab === 'sop' && resolvedSopId
        ? `operation:${resolvedSopId}`
        : bakeTab === 'data' && selectedDataId !== null
          ? `data:${selectedDataId}`
          : null

  useEffect(() => {
    if (!resolvedTemplateId) {
      setRelatedTemplateSkills([])
      return
    }
    let cancelled = false
    listLocalCreationSkills(apiBaseUrl, { sourceKind: 'bake_document', sourceId: resolvedTemplateId })
      .then(items => {
        if (!cancelled) setRelatedTemplateSkills(items)
      })
      .catch(() => {
        if (!cancelled) setRelatedTemplateSkills([])
      })
    return () => { cancelled = true }
  }, [apiBaseUrl, resolvedTemplateId])

  const refreshOverview = async () => {
    const data = await fetchOverview()
    setOverview(mapBakeOverview(data))
  }

  const refreshKnowledge = async (offset = bakeKnowledgeOffset) => {
    const data = await fetchKnowledge({
      q: bakeKnowledgeQuery.trim() || undefined,
      from: parseDateInputToMs(bakeKnowledgeFrom),
      to: parseDateInputToMs(bakeKnowledgeTo, true),
      limit: bakeKnowledgeLimit,
      offset,
    })
    setKnowledgeItems(data.items)
    setKnowledgeTotal(data.total)
  }

  const refreshTemplates = async (offset = bakeTemplateOffset) => {
    const data = await fetchTemplates({
      q: bakeTemplateQuery.trim() || undefined,
      from: parseDateInputToMs(bakeTemplateFrom),
      to: parseDateInputToMs(bakeTemplateTo, true),
      limit: bakeTemplateLimit,
      offset,
    })
    setTemplates(data.items)
    setTemplateTotal(data.total)
  }

  const refreshSops = async (offset = bakeSopOffset) => {
    const data = await fetchSops({
      q: bakeSopQuery.trim() || undefined,
      from: parseDateInputToMs(bakeSopFrom),
      to: parseDateInputToMs(bakeSopTo, true),
      limit: bakeSopLimit,
      offset,
    })
    setSopCandidates(data.items)
    setSopTotal(data.total)
  }

  const refreshData = async (offset = dataOffset) => {
    const data = await fetchDataSources({
      q: dataQuery.trim() || undefined,
      limit: dataLimit,
      offset,
    })
    setDataItems(data.items)
    setDataTotal(data.total)
    setSelectedDataId(current => data.items.some(item => item.id === current)
      ? current
      : (data.items[0]?.id ?? null))
  }

  const currentNavigationTarget = () => ({
    windowMode: 'bake' as const,
    bakeTab,
    selectedMemoryId,
    selectedTemplateId: resolvedTemplateId,
    selectedSopId: resolvedSopId,
    selectedKnowledgeId: resolvedKnowledgeId,
    bakeTemplateFocusId,
    bakeKnowledgeFocusId,
    bakeSopFocusId,
  })

  const restoreNavigationTarget = (target: BakeNavigationTarget) => {
    setWindowMode(target.windowMode)
    if (target.bakeTab) setBakeTab(target.bakeTab)
    if (target.repositoryTab) setRepositoryTab(target.repositoryTab)
    if (target.selectedMemoryId !== undefined) setSelectedMemoryId(target.selectedMemoryId)
    if (target.selectedTemplateId !== undefined) setSelectedTemplateId(target.selectedTemplateId)
    if (target.selectedSopId !== undefined) setSelectedSopId(target.selectedSopId)
    if (target.selectedKnowledgeId !== undefined) setSelectedKnowledgeId(target.selectedKnowledgeId)
    if (target.selectedCaptureId !== undefined) setSelectedCaptureId(target.selectedCaptureId)
    if (target.repositoryMemoryFocusId !== undefined) setRepositoryMemoryFocusId(target.repositoryMemoryFocusId)
    if (target.bakeTemplateFocusId !== undefined) setBakeTemplateFocusId(target.bakeTemplateFocusId)
    if (target.bakeKnowledgeFocusId !== undefined) setBakeKnowledgeFocusId(target.bakeKnowledgeFocusId)
    if (target.bakeSopFocusId !== undefined) setBakeSopFocusId(target.bakeSopFocusId)
    if (target.repositoryCaptureSourceCaptureId !== undefined) {
      setRepositoryCaptureSourceCaptureId(target.repositoryCaptureSourceCaptureId)
    }
  }

  const handleGoBack = () => {
    const target = popBakeNavigationTarget()
    if (!target) {
      setStatusMessage('当前没有可返回的上一步页面')
      return
    }
    restoreNavigationTarget(target)
    setStatusMessage('已返回上一步页面')
  }

  const handleSearchKnowledge = () => {
    clearBakeNavigationStack()
    setSelectedKnowledgeId(null)
    setBakeKnowledgeFocusId(null)
    useAppStore.setState({
      bakeKnowledgeQuery: draftKnowledgeQuery,
      bakeKnowledgeFrom: draftKnowledgeFrom,
      bakeKnowledgeTo: draftKnowledgeTo,
      bakeKnowledgeOffset: 0,
    })
  }

  const handleClearKnowledgeFilters = () => {
    clearBakeNavigationStack()
    setDraftKnowledgeQuery('')
    setDraftKnowledgeFrom('')
    setDraftKnowledgeTo('')
    setSelectedKnowledgeId(null)
    useAppStore.setState({
      bakeKnowledgeFocusId: null,
      bakeKnowledgeQuery: '',
      bakeKnowledgeFrom: '',
      bakeKnowledgeTo: '',
      bakeKnowledgeOffset: 0,
    })
  }

  const handleSearchTemplate = () => {
    clearBakeNavigationStack()
    setSelectedTemplateId(null)
    setBakeTemplateFocusId(null)
    useAppStore.setState({
      bakeTemplateQuery: draftTemplateQuery,
      bakeTemplateFrom: draftTemplateFrom,
      bakeTemplateTo: draftTemplateTo,
      bakeTemplateOffset: 0,
    })
  }

  const handleClearTemplateFilters = () => {
    clearBakeNavigationStack()
    setDraftTemplateQuery('')
    setDraftTemplateFrom('')
    setDraftTemplateTo('')
    setSelectedTemplateId(null)
    useAppStore.setState({
      bakeTemplateFocusId: null,
      bakeTemplateQuery: '',
      bakeTemplateFrom: '',
      bakeTemplateTo: '',
      bakeTemplateOffset: 0,
    })
  }

  const handleSearchSop = () => {
    clearBakeNavigationStack()
    setSelectedSopId(null)
    setBakeSopFocusId(null)
    useAppStore.setState({
      bakeSopQuery: draftSopQuery,
      bakeSopFrom: draftSopFrom,
      bakeSopTo: draftSopTo,
      bakeSopOffset: 0,
    })
  }

  const handleClearSopFilters = () => {
    clearBakeNavigationStack()
    setDraftSopQuery('')
    setDraftSopFrom('')
    setDraftSopTo('')
    setSelectedSopId(null)
    useAppStore.setState({
      bakeSopFocusId: null,
      bakeSopQuery: '',
      bakeSopFrom: '',
      bakeSopTo: '',
      bakeSopOffset: 0,
    })
  }

  const handleSearchData = () => {
    setDataFocusId(null)
    setDataOffset(0)
    setDataQuery(draftDataQuery)
  }

  const handleClearDataSearch = () => {
    setDataFocusId(null)
    setDraftDataQuery('')
    setDataQuery('')
    setDataOffset(0)
  }

  const handleRefreshData = async (sourceId: number) => {
    setRefreshingDataId(sourceId)
    try {
      const result = await refreshDataSource(sourceId, 'auto')
      await refreshData()
      const browserLabels: Record<string, string> = {
        chrome: 'Chrome',
        chrome_canary: 'Chrome Canary',
        edge: 'Edge',
        brave: 'Brave',
        chromium: 'Chromium',
        vivaldi: 'Vivaldi',
        safari: 'Safari',
      }
      const channel = result.collector === 'browser_attach' || result.collector === 'chrome_attach'
        ? `${browserLabels[result.browser ?? ''] ?? '浏览器'}登录会话`
        : '直接网页访问'
      setStatusMessage(`已通过${channel}刷新数据`)
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '数据刷新失败'))
    } finally {
      setRefreshingDataId(null)
    }
  }

  const handleDeleteData = async (sourceId: number) => {
    if (!(await confirmDestructive({ title: '删除这条数据？', description: '删除后不会影响来源时间线和采集记录。' }))) return
    setDeletingDataId(sourceId)
    try {
      await deleteDataSource(sourceId)
      const removesDataRecord = dataItems.some(item => item.id === sourceId)
      const nextOffset = removesDataRecord
        ? getFallbackOffsetAfterRemoval(dataItems.length, dataOffset, dataLimit)
        : dataOffset
      if (nextOffset !== dataOffset) {
        setDataOffset(nextOffset)
      } else {
        await refreshData(nextOffset)
      }
      if (selectedDataId === sourceId) setSelectedDataId(null)
      if (dataFocusId === sourceId) setDataFocusId(null)
      setStatusMessage('已删除数据')
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除数据失败'))
    } finally {
      setDeletingDataId(null)
    }
  }

  const handleBakeTabChange = (tab: BakeTab) => {
    if (tab === bakeTab) return
    clearBakeNavigationStack()
    setBakeTab(tab)
  }

  const handleOpenGraphNode = (node: MemoryGraphNode) => {
    setGraphOpen(true)
    clearBakeNavigationStack()
    if (node.kind === 'knowledge') {
      setBakeTab('knowledge')
      setBakeKnowledgeFocusId(node.assetId)
      useAppStore.setState({
        bakeKnowledgeFocusId: node.assetId,
        bakeKnowledgeQuery: '',
        bakeKnowledgeFrom: '',
        bakeKnowledgeTo: '',
        bakeKnowledgeOffset: 0,
      })
      setSelectedKnowledgeId(node.assetId)
    } else if (node.kind === 'document') {
      setBakeTab('templates')
      setBakeTemplateFocusId(node.assetId)
      setBakeTemplateOffset(0)
      setSelectedTemplateId(node.assetId)
    } else if (node.kind === 'operation') {
      setBakeTab('sop')
      setBakeSopFocusId(node.assetId)
      setBakeSopOffset(0)
      setSelectedSopId(node.assetId)
    } else {
      const sourceId = Number(node.assetId)
      if (!Number.isFinite(sourceId)) {
        setStatusMessage('这条数据的标识无效，无法打开')
        return
      }
      setBakeTab('data')
      setDataFocusId(sourceId)
      setDataOffset(0)
      setSelectedDataId(sourceId)
    }
    setStatusMessage(`已从记忆图谱打开「${node.label}」`)
  }

  const handleCreateTemplate = async () => {
    try {
      const created = await createTemplate(createDraftTemplate())
      setTemplates(prev => [created, ...prev.filter(item => item.id !== created.id)])
      setBakeTab('templates')
      setBakeTemplateOffset(0)
      setSelectedTemplateId(created.id)
      setStatusMessage(`已新建模板「${created.title}」`)
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '新建文档失败'))
    }
  }

  const handleOpenLink = (url?: string, sourceCaptureId?: string) => {
    if (sourceCaptureId) {
      pushBakeNavigationTarget(currentNavigationTarget())
      setWindowMode('knowledge')
      setRepositoryTab('capture')
      setRepositoryCaptureSourceCaptureId(sourceCaptureId)
      setSelectedCaptureId(sourceCaptureId)
      setStatusMessage('已打开关联采集记录')
      return
    }
    if (url) {
      window.open(url, '_blank', 'noopener,noreferrer')
      setStatusMessage('已打开原文链接')
      return
    }
    setStatusMessage('当前内容没有可打开的原文或关联采集记录')
  }

  const handleUpdateTemplate = async (templateId: string, updater: (template: ArticleTemplate) => ArticleTemplate) => {
    const target = templates.find(item => item.id === templateId)
    if (!target) return
    try {
      const updated = await updateTemplate(updater(target))
      setTemplates(prev => prev.map(item => item.id === templateId ? updated : item))
      setStatusMessage(`已更新模板「${updated.title}」`)
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新文档失败'))
    }
  }

  const handleToggleTemplateStatus = async (templateId: string) => {
    try {
      const updated = await toggleTemplateStatus(templateId)
      setTemplates(prev => prev.map(item => item.id === templateId ? updated : item))
      setStatusMessage(`模板状态已切换为「${updated.status}」`)
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新文档状态失败'))
    }
  }

  const handleDeleteTemplate = async (templateId: string) => {
    if (!(await confirmDestructive({ title: '删除这份文档？', description: '删除后无法恢复，来源时间线和其他内容会保留。' }))) return
    try {
      await deleteTemplate(templateId)
      const nextOffset = getFallbackOffsetAfterRemoval(templates.length, bakeTemplateOffset, bakeTemplateLimit)
      if (nextOffset !== bakeTemplateOffset) {
        setBakeTemplateOffset(nextOffset)
      } else {
        await refreshTemplates(nextOffset)
      }
      if (selectedTemplateId === templateId || resolvedTemplateId === templateId) {
        setSelectedTemplateId(null)
      }
      setStatusMessage('已删除文档')
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除文档失败'))
    }
  }

  const handleViewSourceMemory = (memoryId?: string) => {
    if (!memoryId) {
      setStatusMessage('当前模板还没有关联来源时间线')
      return
    }
    pushBakeNavigationTarget(currentNavigationTarget())
    setWindowMode('knowledge')
    setRepositoryTab('memory')
    setRepositoryMemoryFocusId(memoryId)
    setSelectedMemoryId(memoryId)
    setStatusMessage('已切换到来源时间线')
  }

  const handleViewRelatedDocument = async (timelineId: string) => {
    const relatedDoc = templates.find(t => t.sourceMemoryIds.includes(timelineId))
    if (!relatedDoc) {
      setStatusMessage('当前时间线还没有被提炼为文档')
      return
    }
    pushBakeNavigationTarget(currentNavigationTarget())
    setBakeTab('templates')
    setBakeTemplateOffset(0)
    setBakeTemplateFocusId(relatedDoc.id)
    setSelectedTemplateId(relatedDoc.id)
    setStatusMessage(`已切换到关联文档「${relatedDoc.title}」`)
  }

  const handleViewLinkedKnowledge = (knowledgeId: string) => {
    pushBakeNavigationTarget(currentNavigationTarget())
    setBakeTab('knowledge')
    setBakeKnowledgeFocusId(knowledgeId)
    useAppStore.setState({
      bakeKnowledgeFocusId: knowledgeId,
      bakeKnowledgeQuery: '',
      bakeKnowledgeFrom: '',
      bakeKnowledgeTo: '',
      bakeKnowledgeOffset: 0,
    })
    setSelectedKnowledgeId(knowledgeId)
    setStatusMessage('已切换到关联知识')
  }

  const handleDeleteKnowledge = async (id: string) => {
    if (!(await confirmDestructive({ title: '删除这条知识？', description: '删除后无法恢复，来源时间线和采集记录会保留。' }))) return
    try {
      await deleteKnowledge(id)
      const nextOffset = getFallbackOffsetAfterRemoval(knowledgeItems.length, bakeKnowledgeOffset, bakeKnowledgeLimit)
      if (selectedKnowledgeId === id || resolvedKnowledgeId === id) {
        setSelectedKnowledgeId(null)
      }
      if (nextOffset !== bakeKnowledgeOffset) {
        setBakeKnowledgeOffset(nextOffset)
      } else {
        await refreshKnowledge(nextOffset)
      }
      setStatusMessage('已删除知识条目')
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除知识失败'))
    }
  }

  const handleDeleteSop = async (id: string) => {
    if (!(await confirmDestructive({ title: '删除这份操作？', description: '删除后无法恢复，来源时间线和采集记录会保留。' }))) return
    try {
      await deleteSop(id)
      const nextOffset = getFallbackOffsetAfterRemoval(sopCandidates.length, bakeSopOffset, bakeSopLimit)
      if (selectedSopId === id || resolvedSopId === id) {
        setSelectedSopId(null)
      }
      if (nextOffset !== bakeSopOffset) {
        setBakeSopOffset(nextOffset)
      } else {
        await refreshSops(nextOffset)
      }
      setStatusMessage('已删除操作手册')
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除操作手册失败'))
    }
  }

  return (
    <div className="bake-panel">
      {creationBackTarget && (
        <div style={{
          padding: '10px 16px',
          background: '#f0fdfa',
          borderBottom: '1px solid #99f6e4',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <span style={{ fontSize: 13, color: '#0f766e' }}>
            从智能创作的参考资料跳转而来
          </span>
          <button
            type="button"
            onClick={() => {
              const target = creationBackTarget
              clearCreationBackTarget()
              setWindowMode(target.windowMode)
            }}
            style={{
              padding: '6px 12px',
              border: '1px solid #0f766e',
              borderRadius: 6,
              background: '#fff',
              color: '#0f766e',
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <ArrowLeft size={14} />
            返回创作
          </button>
        </div>
      )}
      <BakeHeader />
      {bakeNavigationStack.length > 0 && (
        <div className="bake-backbar">
          <BakeButton compact onClick={handleGoBack}>
            <ArrowLeft size={14} />
            返回上一步
          </BakeButton>
        </div>
      )}
      {statusMessage && <div className="bake-inline-message">{statusMessage}</div>}

      {/* 模型未就绪提示 */}
      {!modelStatusLoading && !modelsReady && (
        <div style={{
          margin: '12px 16px',
          padding: '12px',
          background: '#FFF3CD',
          border: '1px solid #FFE69C',
          borderRadius: 8,
          fontSize: 13,
          color: '#856404',
        }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>AI 能力尚未就绪</div>
          <div style={{ marginBottom: 8 }}>
            {!modelStatus.runtime && '本地 AI 能力尚未就绪。'}
            {!modelStatus.llm && '分析模型尚未加载。'}
            {!modelStatus.embedding && '语义索引尚未加载。'}
          </div>
          <div style={{ fontSize: 12 }}>
            请前往「AI 能力」检查状态，全部就绪后即可继续提炼。
          </div>
        </div>
      )}

      <div className="bake-tabs-shell">
        <BakeTabs current={bakeTab} onChange={handleBakeTabChange} />
        {bakeTab !== 'overview' && (
          <button
            type="button"
            className="bake-graph-toggle"
            aria-pressed={graphOpen}
            aria-label={graphOpen ? '关闭记忆图谱' : '展开记忆图谱'}
            onClick={() => setGraphOpen(current => !current)}
          >
            <Network size={15} />
            <span>记忆图谱</span>
          </button>
        )}
      </div>

      <div className={`bake-graph-workspace ${graphOpen && bakeTab !== 'overview' ? 'bake-graph-workspace--open' : ''}`.trim()}>
        <div className="bake-tab-content">
        {bakeTab === 'overview' && (
          <BakeOverviewTab
            overview={overview}
            graphAssets={graphAssetsForRender}
            graphLoading={graphLoading}
            graphError={graphError}
            onRetryGraph={() => setGraphRevision(current => current + 1)}
            onOpenGraphNode={handleOpenGraphNode}
            onSearchGraph={searchOverviewGraph}
          />
        )}
        {bakeTab === 'knowledge' && (
          <BakeKnowledgeTab
            items={knowledgeItems}
            total={knowledgeTotal}
            offset={bakeKnowledgeOffset}
            limit={bakeKnowledgeLimit}
            query={bakeKnowledgeQuery}
            draftQuery={draftKnowledgeQuery}
            from={bakeKnowledgeFrom}
            to={bakeKnowledgeTo}
            draftFrom={draftKnowledgeFrom}
            draftTo={draftKnowledgeTo}
            selectedKnowledgeId={resolvedKnowledgeId}
            onSelectKnowledge={setSelectedKnowledgeId}
            onPageChange={setBakeKnowledgeOffset}
            onLimitChange={setBakeKnowledgeLimit}
            onDraftQueryChange={setDraftKnowledgeQuery}
            onDraftFromChange={setDraftKnowledgeFrom}
            onDraftToChange={setDraftKnowledgeTo}
            onSearch={handleSearchKnowledge}
            onClearFilters={handleClearKnowledgeFilters}
            focusId={bakeKnowledgeFocusId}
            onDeleteKnowledge={handleDeleteKnowledge}
            onViewSourceTimeline={handleViewSourceMemory}
            sourceTimelineTitle={resolvedKnowledgeItem?.sourceTimelineId ? memoryTitleById.get(resolvedKnowledgeItem.sourceTimelineId) : undefined}
            onOpenCapture={(captureId?: string) => {
              if (!captureId) {
                setStatusMessage('当前内容暂无关联采集记录')
                return
              }
              pushBakeNavigationTarget(currentNavigationTarget())
              setWindowMode('knowledge')
              setRepositoryTab('capture')
              setRepositoryCaptureSourceCaptureId(captureId)
              setSelectedCaptureId(captureId)
              setStatusMessage('已切换到关联采集记录')
            }}
          />
        )}
        {bakeTab === 'data' && (
          <BakeDataTab
            items={dataItems}
            total={dataTotal}
            offset={dataOffset}
            limit={dataLimit}
            draftQuery={draftDataQuery}
            selectedId={selectedDataId}
            loading={dataLoading}
            refreshingId={refreshingDataId}
            deletingId={deletingDataId}
            onDraftQueryChange={setDraftDataQuery}
            onSearch={handleSearchData}
            onClearSearch={handleClearDataSearch}
            onSelect={setSelectedDataId}
            onPageChange={setDataOffset}
            onLimitChange={(limit) => {
              setDataLimit(limit)
              setDataOffset(0)
            }}
            onRefresh={handleRefreshData}
            onDelete={handleDeleteData}
            onViewTimeline={(timelineId) => handleViewSourceMemory(String(timelineId))}
          />
        )}
        {bakeTab === 'templates' && (
          <BakeTemplatesTab
            templates={templates}
            total={templateTotal}
            offset={bakeTemplateOffset}
            limit={bakeTemplateLimit}
            query={bakeTemplateQuery}
            from={bakeTemplateFrom}
            to={bakeTemplateTo}
            draftQuery={draftTemplateQuery}
            draftFrom={draftTemplateFrom}
            draftTo={draftTemplateTo}
            selectedTemplateId={resolvedTemplateId}
            onSelectTemplate={setSelectedTemplateId}
            onCreateTemplate={handleCreateTemplate}
            onUpdateTemplate={handleUpdateTemplate}
            onToggleTemplateStatus={handleToggleTemplateStatus}
            onDeleteTemplate={handleDeleteTemplate}
            onSettleSkill={(template) => setCreationSkillEditor({ source: {
              kind: 'bake_document',
              id: template.id,
              title: template.title,
              docType: template.docType,
              content: template.fullContent || [
                `# ${template.title}`,
                template.summary || '',
                ...template.sections.map(section => `## ${section.title}\n${section.notes || ''}`),
                template.promptHint || '',
              ].filter(Boolean).join('\n\n'),
            } })}
            relatedSkills={relatedTemplateSkills}
            onOpenSkill={(skill) => setCreationSkillEditor({ initialSkill: skill })}
            onViewSourceMemory={handleViewSourceMemory}
            memoryTitleById={memoryTitleById}
            onPageChange={setBakeTemplateOffset}
            onLimitChange={setBakeTemplateLimit}
            onDraftQueryChange={setDraftTemplateQuery}
            onDraftFromChange={setDraftTemplateFrom}
            onDraftToChange={setDraftTemplateTo}
            onSearch={handleSearchTemplate}
            onClearFilters={handleClearTemplateFilters}
            focusId={bakeTemplateFocusId}
          />
        )}
        {bakeTab === 'sop' && (
          <BakeSopTab
            candidates={sopCandidates}
            total={sopTotal}
            offset={bakeSopOffset}
            limit={bakeSopLimit}
            query={bakeSopQuery}
            from={bakeSopFrom}
            to={bakeSopTo}
            draftQuery={draftSopQuery}
            draftFrom={draftSopFrom}
            draftTo={draftSopTo}
            selectedSopId={resolvedSopId}
            onSelectSop={setSelectedSopId}
            onDeleteSop={handleDeleteSop}
            onViewSourceTimeline={handleViewSourceMemory}
            sourceTimelineTitle={resolvedSopItem?.sourceTimelineId ? memoryTitleById.get(resolvedSopItem.sourceTimelineId) : undefined}
            onPageChange={setBakeSopOffset}
            onLimitChange={setBakeSopLimit}
            onDraftQueryChange={setDraftSopQuery}
            onDraftFromChange={setDraftSopFrom}
            onDraftToChange={setDraftSopTo}
            onSearch={handleSearchSop}
            onClearFilters={handleClearSopFilters}
            focusId={bakeSopFocusId}
          />
        )}
        {creationSkillEditor && (
          <CreationSkillEditor
            source={creationSkillEditor.source}
            initialSkill={creationSkillEditor.initialSkill}
            onClose={() => setCreationSkillEditor(null)}
            onSaved={(skill) => {
              setStatusMessage(skill.status === 'draft' ? '技能草稿已自动保存' : '技能已保存')
              if (skill.sourceKind === 'bake_document' && skill.sourceId === resolvedTemplateId) {
                setRelatedTemplateSkills(prev => [skill, ...prev.filter(item => item.id !== skill.id)])
              }
            }}
          />
        )}
        </div>
        {graphOpen && bakeTab !== 'overview' && (
          <BakeMemoryGraph
            assets={graphAssetsForRender}
            focusNodeId={graphFocusNodeId}
            loading={graphLoading}
            error={graphError}
            mode="dock"
            onClose={() => setGraphOpen(false)}
            onRetry={() => setGraphRevision(current => current + 1)}
            onOpenNode={handleOpenGraphNode}
          />
        )}
      </div>
      {confirmDialog}
    </div>
  )
}

export default BakePanel
