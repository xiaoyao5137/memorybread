import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import {
  useCreateBakeKnowledge,
  useCreateBakeSop,
  useCreateBakeTemplate,
  useCreateDataSource,
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
  useRefreshBakeDocument,
  useSetBakeDocumentRefreshPolicy,
  useUpdateBakeKnowledge,
  useUpdateBakeSop,
  useUpdateBakeTemplate,
  useUpdateDataSource,
  useRefreshDataSource,
  useModelStatus,
  useUpdateMemoryFavorite,
} from '../hooks/useApi'
import type { BakeOverviewResponse, DataSourceWriteInput } from '../hooks/useApi'
import { useAppStore, type BakeNavigationTarget } from '../store/useAppStore'
import { toUserFacingError } from '../utils/userFacingError'
import { listLocalCreationSkills, type CreationSkillSource, type LocalCreationSkill } from '../utils/creationSkills'
import type {
  ArticleTemplate,
  BakeKnowledgeItem,
  BakeOverview,
  BakeTab,
  DataSource,
  MemoryFavoriteFilter,
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

const favoriteFilterToQuery = (filter: MemoryFavoriteFilter): boolean | undefined => (
  filter === 'all' ? undefined : filter === 'favorite'
)

const favoriteMatchesFilter = (filter: MemoryFavoriteFilter, isFavorite: boolean) => (
  filter === 'all' || (filter === 'favorite' ? isFavorite : !isFavorite)
)

const getLocalDayRange = (nowMs: number) => {
  const start = new Date(nowMs)
  start.setHours(0, 0, 0, 0)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  return { fromMs: start.getTime(), toMs: end.getTime() - 1 }
}

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
  title: '新文档',
  docType: 'general_document',
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
    bakeDataFocusId,
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
    setBakeDataFocusId,
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
  const createKnowledge = useCreateBakeKnowledge()
  const updateKnowledge = useUpdateBakeKnowledge()
  const deleteKnowledge = useDeleteBakeKnowledge()
  const fetchTemplates = useFetchBakeTemplates()
  const fetchTemplate = useFetchBakeTemplate()
  const createTemplate = useCreateBakeTemplate()
  const updateTemplate = useUpdateBakeTemplate()
  const toggleTemplateStatus = useToggleBakeTemplateStatus()
  const deleteTemplate = useDeleteBakeTemplate()
  const refreshBakeDocument = useRefreshBakeDocument()
  const setTemplateRefreshPolicy = useSetBakeDocumentRefreshPolicy()
  const fetchSops = useFetchBakeSops()
  const fetchSop = useFetchBakeSop()
  const createSop = useCreateBakeSop()
  const updateSop = useUpdateBakeSop()
  const deleteSop = useDeleteBakeSop()
  const fetchDataSource = useFetchDataSource()
  const fetchDataSources = useFetchDataSources()
  const refreshDataSource = useRefreshDataSource()
  const createDataSource = useCreateDataSource()
  const updateDataSource = useUpdateDataSource()
  const deleteDataSource = useDeleteDataSource()
  const updateMemoryFavorite = useUpdateMemoryFavorite()
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
  const [dataSourceKind, setDataSourceKind] = useState<'' | DataSource['source_kind']>('')
  const [draftDataSourceKind, setDraftDataSourceKind] = useState<'' | DataSource['source_kind']>('')
  const [dataFrom, setDataFrom] = useState('')
  const [draftDataFrom, setDraftDataFrom] = useState('')
  const [dataTo, setDataTo] = useState('')
  const [draftDataTo, setDraftDataTo] = useState('')
  const [dataFavoriteFilter, setDataFavoriteFilter] = useState<MemoryFavoriteFilter>('all')
  const [dataOffset, setDataOffset] = useState(0)
  const [dataLimit, setDataLimit] = useState(PAGE_SIZE)
  const [selectedDataId, setSelectedDataId] = useState<number | null>(null)
  const [dataFocusId, setDataFocusId] = useState<number | null>(null)
  const [dataLoading, setDataLoading] = useState(false)
  const [refreshingDataId, setRefreshingDataId] = useState<number | null>(null)
  const [refreshingTemplateId, setRefreshingTemplateId] = useState<string | null>(null)
  const [deletingDataId, setDeletingDataId] = useState<number | null>(null)
  const [statusMessage, setStatusMessage] = useState<string | null>(null)
  const [draftKnowledgeQuery, setDraftKnowledgeQuery] = useState(bakeKnowledgeQuery)
  const [draftKnowledgeFrom, setDraftKnowledgeFrom] = useState(bakeKnowledgeFrom)
  const [draftKnowledgeTo, setDraftKnowledgeTo] = useState(bakeKnowledgeTo)
  const [knowledgeFavoriteFilter, setKnowledgeFavoriteFilter] = useState<MemoryFavoriteFilter>('all')
  const [draftTemplateQuery, setDraftTemplateQuery] = useState(bakeTemplateQuery)
  const [draftTemplateFrom, setDraftTemplateFrom] = useState(bakeTemplateFrom)
  const [draftTemplateTo, setDraftTemplateTo] = useState(bakeTemplateTo)
  const [templateDocType, setTemplateDocType] = useState('')
  const [draftTemplateDocType, setDraftTemplateDocType] = useState('')
  const [templateFavoriteFilter, setTemplateFavoriteFilter] = useState<MemoryFavoriteFilter>('all')
  const [draftSopQuery, setDraftSopQuery] = useState(bakeSopQuery)
  const [draftSopFrom, setDraftSopFrom] = useState(bakeSopFrom)
  const [draftSopTo, setDraftSopTo] = useState(bakeSopTo)
  const [sopFavoriteFilter, setSopFavoriteFilter] = useState<MemoryFavoriteFilter>('all')
  const [creationSkillEditor, setCreationSkillEditor] = useState<{ source?: CreationSkillSource; initialSkill?: LocalCreationSkill } | null>(null)
  const [relatedTemplateSkills, setRelatedTemplateSkills] = useState<LocalCreationSkill[]>([])
  const [graphOpen, setGraphOpen] = useState(false)
  const [graphAssets, setGraphAssets] = useState<MemoryGraphAssets>(emptyGraphAssets)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState<string | null>(null)
  const [graphRevision, setGraphRevision] = useState(0)
  const knowledgeRequestSeqRef = useRef(0)
  const templateRequestSeqRef = useRef(0)
  const sopRequestSeqRef = useRef(0)
  const [overviewTodayRange, setOverviewTodayRange] = useState(() => getLocalDayRange(Date.now()))

  useEffect(() => {
    const delayUntilTomorrow = Math.max(1_000, overviewTodayRange.toMs - Date.now() + 1)
    const timer = window.setTimeout(() => {
      setOverviewTodayRange(getLocalDayRange(Date.now()))
    }, delayUntilTomorrow)
    return () => window.clearTimeout(timer)
  }, [overviewTodayRange.toMs])

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
      const [
        memoriesResult,
        todayKnowledgeResult,
        knowledgeResult,
        todayTemplatesResult,
        templatesResult,
        todaySopsResult,
        sopsResult,
        dataResult,
      ] = await Promise.allSettled([
        fetchMemories({ limit: 1, offset: 0 }),
        fetchKnowledge({
          sort: 'heat',
          from: overviewTodayRange.fromMs,
          to: overviewTodayRange.toMs,
          limit: GRAPH_ASSET_LIMIT,
          offset: 0,
        }),
        fetchKnowledge({ sort: 'heat', limit: GRAPH_ASSET_LIMIT, offset: 0 }),
        fetchTemplates({
          from: overviewTodayRange.fromMs,
          to: overviewTodayRange.toMs,
          limit: GRAPH_ASSET_LIMIT,
          offset: 0,
        }),
        fetchTemplates({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
        fetchSops({
          from: overviewTodayRange.fromMs,
          to: overviewTodayRange.toMs,
          limit: GRAPH_ASSET_LIMIT,
          offset: 0,
        }),
        fetchSops({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
        fetchDataSources({ limit: GRAPH_ASSET_LIMIT, offset: 0 }),
      ])
      if (cancelled) return

      const knowledge = mergeUniqueById(
        todayKnowledgeResult.status === 'fulfilled' ? todayKnowledgeResult.value.items : [],
        knowledgeResult.status === 'fulfilled' ? knowledgeResult.value.items : [],
      )
      const templateItems = mergeUniqueById(
        todayTemplatesResult.status === 'fulfilled' ? todayTemplatesResult.value.items : [],
        templatesResult.status === 'fulfilled' ? templatesResult.value.items : [],
      )
      const sops = mergeUniqueById(
        todaySopsResult.status === 'fulfilled' ? todaySopsResult.value.items : [],
        sopsResult.status === 'fulfilled' ? sopsResult.value.items : [],
      )
      const dataItems = dataResult.status === 'fulfilled' ? dataResult.value.items : []
      const failedGraphRequests = [
        [todayKnowledgeResult, knowledgeResult],
        [todayTemplatesResult, templatesResult],
        [todaySopsResult, sopsResult],
        [dataResult],
      ].filter(results => results.every(result => result.status === 'rejected')).length

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
  }, [bakeTab, fetchDataSources, fetchKnowledge, fetchMemories, fetchSops, fetchTemplates, graphRevision, overviewTodayRange])

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
          source_kind: dataSourceKind || undefined,
          from: parseDateInputToMs(dataFrom),
          to: parseDateInputToMs(dataTo, true),
          favorite: favoriteFilterToQuery(dataFavoriteFilter),
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
  }, [bakeTab, dataFavoriteFilter, dataFocusId, dataFrom, dataLimit, dataOffset, dataQuery, dataSourceKind, dataTo, fetchDataSource, fetchDataSources])

  // 创作参考资料等外部入口通过 store 指定要打开的数据来源，这里同步到
  // 数据页的本地聚焦状态，复用既有的单条来源加载逻辑。
  useEffect(() => {
    if (bakeTab !== 'data' || !bakeDataFocusId) return
    const focusId = Number(bakeDataFocusId)
    if (!Number.isFinite(focusId) || focusId <= 0) return
    setDataFocusId(focusId)
    setDataOffset(0)
    setSelectedDataId(focusId)
  }, [bakeTab, bakeDataFocusId])

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
      favorite: favoriteFilterToQuery(knowledgeFavoriteFilter),
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
  }, [bakeKnowledgeFocusId, bakeKnowledgeFrom, bakeKnowledgeLimit, bakeKnowledgeOffset, bakeKnowledgeQuery, bakeKnowledgeTo, bakeTab, fetchKnowledge, fetchKnowledgeDetail, knowledgeFavoriteFilter, setSelectedKnowledgeId])

  useEffect(() => {
    if (bakeTab !== 'templates') return
    const requestSeq = templateRequestSeqRef.current + 1
    templateRequestSeqRef.current = requestSeq
    if (bakeTemplateFocusId) {
      void fetchTemplate(bakeTemplateFocusId).then((item) => {
        if (requestSeq !== templateRequestSeqRef.current) return
        setTemplates([item])
        setTemplateTotal(1)
        setSelectedTemplateId(item.id)
      }).catch((error) => {
        if (requestSeq !== templateRequestSeqRef.current) return
        setTemplates([])
        setTemplateTotal(0)
        setStatusMessage(toUserFacingError(error, '未找到这份文档'))
      })
      return
    }
    void fetchTemplates({
      q: bakeTemplateQuery.trim() || undefined,
      doc_type: templateDocType || undefined,
      from: parseDateInputToMs(bakeTemplateFrom),
      to: parseDateInputToMs(bakeTemplateTo, true),
      favorite: favoriteFilterToQuery(templateFavoriteFilter),
      limit: bakeTemplateLimit,
      offset: bakeTemplateOffset,
    }).then((data) => {
      if (requestSeq !== templateRequestSeqRef.current) return
      setTemplates(data.items)
      setTemplateTotal(data.total)
    }).catch((error) => {
      if (requestSeq !== templateRequestSeqRef.current) return
      setStatusMessage(toUserFacingError(error, '文档加载失败'))
    })
  }, [bakeTab, bakeTemplateFocusId, bakeTemplateFrom, bakeTemplateLimit, bakeTemplateOffset, bakeTemplateQuery, bakeTemplateTo, fetchTemplate, fetchTemplates, setSelectedTemplateId, templateDocType, templateFavoriteFilter])

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
    const requestSeq = sopRequestSeqRef.current + 1
    sopRequestSeqRef.current = requestSeq
    if (bakeSopFocusId) {
      void fetchSop(bakeSopFocusId).then((item) => {
        if (requestSeq !== sopRequestSeqRef.current) return
        setSopCandidates([item])
        setSopTotal(1)
        setSelectedSopId(item.id)
      }).catch((error) => {
        if (requestSeq !== sopRequestSeqRef.current) return
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
      favorite: favoriteFilterToQuery(sopFavoriteFilter),
      limit: bakeSopLimit,
      offset: bakeSopOffset,
    }).then((data) => {
      if (requestSeq !== sopRequestSeqRef.current) return
      setSopCandidates(data.items)
      setSopTotal(data.total)
    }).catch((error) => {
      if (requestSeq !== sopRequestSeqRef.current) return
      setStatusMessage(toUserFacingError(error, '操作手册加载失败'))
    })
  }, [bakeSopFocusId, bakeSopFrom, bakeSopLimit, bakeSopOffset, bakeSopQuery, bakeSopTo, bakeTab, fetchSop, fetchSops, setSelectedSopId, sopFavoriteFilter])

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
      favorite: favoriteFilterToQuery(knowledgeFavoriteFilter),
      limit: bakeKnowledgeLimit,
      offset,
    })
    setKnowledgeItems(data.items)
    setKnowledgeTotal(data.total)
  }

  const refreshTemplates = async (offset = bakeTemplateOffset) => {
    const data = await fetchTemplates({
      q: bakeTemplateQuery.trim() || undefined,
      doc_type: templateDocType || undefined,
      from: parseDateInputToMs(bakeTemplateFrom),
      to: parseDateInputToMs(bakeTemplateTo, true),
      favorite: favoriteFilterToQuery(templateFavoriteFilter),
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
      favorite: favoriteFilterToQuery(sopFavoriteFilter),
      limit: bakeSopLimit,
      offset,
    })
    setSopCandidates(data.items)
    setSopTotal(data.total)
  }

  const refreshData = async (offset = dataOffset) => {
    const data = await fetchDataSources({
      q: dataQuery.trim() || undefined,
      source_kind: dataSourceKind || undefined,
      from: parseDateInputToMs(dataFrom),
      to: parseDateInputToMs(dataTo, true),
      favorite: favoriteFilterToQuery(dataFavoriteFilter),
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
    bakeDataFocusId,
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
    if (target.bakeDataFocusId !== undefined) setBakeDataFocusId(target.bakeDataFocusId)
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

  const handleKnowledgeFavoriteFilterChange = (value: MemoryFavoriteFilter) => {
    clearBakeNavigationStack()
    setKnowledgeFavoriteFilter(value)
    setBakeKnowledgeFocusId(null)
    setSelectedKnowledgeId(null)
    setBakeKnowledgeOffset(0)
  }

  const handleTemplateFavoriteFilterChange = (value: MemoryFavoriteFilter) => {
    clearBakeNavigationStack()
    setTemplateFavoriteFilter(value)
    setBakeTemplateFocusId(null)
    setSelectedTemplateId(null)
    setBakeTemplateOffset(0)
  }

  const handleSopFavoriteFilterChange = (value: MemoryFavoriteFilter) => {
    clearBakeNavigationStack()
    setSopFavoriteFilter(value)
    setBakeSopFocusId(null)
    setSelectedSopId(null)
    setBakeSopOffset(0)
  }

  const handleDataFavoriteFilterChange = (value: MemoryFavoriteFilter) => {
    setDataFavoriteFilter(value)
    setDataFocusId(null)
    setBakeDataFocusId(null)
    setSelectedDataId(null)
    setDataOffset(0)
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
    setKnowledgeFavoriteFilter('all')
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
    setTemplateDocType(draftTemplateDocType)
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
    setDraftTemplateDocType('')
    setTemplateDocType('')
    setTemplateFavoriteFilter('all')
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
    setSopFavoriteFilter('all')
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
    setBakeDataFocusId(null)
    setDataOffset(0)
    setDataQuery(draftDataQuery)
    setDataSourceKind(draftDataSourceKind)
    setDataFrom(draftDataFrom)
    setDataTo(draftDataTo)
  }

  const handleClearDataSearch = () => {
    setDataFocusId(null)
    setBakeDataFocusId(null)
    setDraftDataQuery('')
    setDataQuery('')
    setDraftDataSourceKind('')
    setDataSourceKind('')
    setDraftDataFrom('')
    setDataFrom('')
    setDraftDataTo('')
    setDataTo('')
    setDataFavoriteFilter('all')
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

  const handleCreateData = async (input: DataSourceWriteInput): Promise<boolean> => {
    try {
      const created = await createDataSource(input)
      setDataItems(previous => [created, ...previous.filter(item => item.id !== created.id)])
      setDataTotal(previous => previous + 1)
      setSelectedDataId(created.id)
      setDataOffset(0)
      setStatusMessage(`已新建数据「${input.title}」`)
      await refreshOverview()
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '新建数据失败'))
      return false
    }
  }

  const handleUpdateData = async (sourceId: number, input: DataSourceWriteInput): Promise<boolean> => {
    try {
      const updated = await updateDataSource(sourceId, input)
      setDataItems(previous => previous.map(item => item.id === sourceId ? updated : item))
      setStatusMessage(`已保存数据「${input.title}」`)
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '保存数据失败'))
      return false
    }
  }

  const handleDeleteData = async (sourceId: number): Promise<boolean> => {
    if (!(await confirmDestructive({ title: '删除这条数据？', description: '删除后不会影响来源时间线和采集记录。' }))) return false
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
      if (Number(bakeDataFocusId) === sourceId) setBakeDataFocusId(null)
      setStatusMessage('已删除数据')
      await refreshOverview()
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除数据失败'))
      return false
    } finally {
      setDeletingDataId(null)
    }
  }

  const handleBakeTabChange = (tab: BakeTab) => {
    if (tab === bakeTab) return
    clearBakeNavigationStack()
    setGraphOpen(false)
    setBakeTab(tab)
  }

  const handleOpenAssetGraph = (kind: MemoryGraphNode['kind'], assetId: string | number) => {
    setGraphOpen(true)
    const id = String(assetId)
    if (kind === 'knowledge') setSelectedKnowledgeId(id)
    else if (kind === 'document') setSelectedTemplateId(id)
    else if (kind === 'operation') setSelectedSopId(id)
    else setSelectedDataId(Number(assetId))
  }

  const handleOpenGraphNode = (node: MemoryGraphNode) => {
    setGraphOpen(false)
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

  const handleToggleKnowledgeFavorite = async (
    item: BakeKnowledgeItem,
    isFavorite: boolean,
  ): Promise<boolean> => {
    try {
      await updateMemoryFavorite('knowledge', item.id, isFavorite)
      const remainsVisible = favoriteMatchesFilter(knowledgeFavoriteFilter, isFavorite)
      setKnowledgeItems(previous => remainsVisible
        ? previous.map(entry => entry.id === item.id ? { ...entry, isFavorite } : entry)
        : previous.filter(entry => entry.id !== item.id))
      if (!remainsVisible) {
        setKnowledgeTotal(previous => Math.max(0, previous - 1))
        setSelectedKnowledgeId(null)
      }
      setStatusMessage(isFavorite ? '已收藏知识' : '已取消收藏知识')
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新知识收藏状态失败'))
      return false
    }
  }

  const handleToggleTemplateFavorite = async (
    item: ArticleTemplate,
    isFavorite: boolean,
  ): Promise<boolean> => {
    try {
      await updateMemoryFavorite('document', item.id, isFavorite)
      const remainsVisible = favoriteMatchesFilter(templateFavoriteFilter, isFavorite)
      setTemplates(previous => remainsVisible
        ? previous.map(entry => entry.id === item.id ? { ...entry, isFavorite } : entry)
        : previous.filter(entry => entry.id !== item.id))
      if (!remainsVisible) {
        setTemplateTotal(previous => Math.max(0, previous - 1))
        setSelectedTemplateId(null)
      }
      setStatusMessage(isFavorite ? '已收藏文档' : '已取消收藏文档')
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新文档收藏状态失败'))
      return false
    }
  }

  const handleToggleSopFavorite = async (
    item: SopCandidate,
    isFavorite: boolean,
  ): Promise<boolean> => {
    try {
      await updateMemoryFavorite('operation', item.id, isFavorite)
      const remainsVisible = favoriteMatchesFilter(sopFavoriteFilter, isFavorite)
      setSopCandidates(previous => remainsVisible
        ? previous.map(entry => entry.id === item.id ? { ...entry, isFavorite } : entry)
        : previous.filter(entry => entry.id !== item.id))
      if (!remainsVisible) {
        setSopTotal(previous => Math.max(0, previous - 1))
        setSelectedSopId(null)
      }
      setStatusMessage(isFavorite ? '已收藏操作' : '已取消收藏操作')
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新操作收藏状态失败'))
      return false
    }
  }

  const handleToggleDataFavorite = async (
    item: DataSource,
    isFavorite: boolean,
  ): Promise<boolean> => {
    try {
      await updateMemoryFavorite('data', item.id, isFavorite)
      const remainsVisible = favoriteMatchesFilter(dataFavoriteFilter, isFavorite)
      setDataItems(previous => remainsVisible
        ? previous.map(entry => entry.id === item.id ? { ...entry, is_favorite: isFavorite } : entry)
        : previous.filter(entry => entry.id !== item.id))
      if (!remainsVisible) {
        setDataTotal(previous => Math.max(0, previous - 1))
        setSelectedDataId(null)
      }
      setStatusMessage(isFavorite ? '已收藏数据' : '已取消收藏数据')
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新数据收藏状态失败'))
      return false
    }
  }

  const handleCreateTemplate = async (
    input: Pick<ArticleTemplate, 'title' | 'docType' | 'fullContent'>,
  ): Promise<boolean> => {
    try {
      const created = await createTemplate({
        ...createDraftTemplate(),
        title: input.title,
        docType: input.docType,
        fullContent: input.fullContent,
      })
      setTemplates(prev => [created, ...prev.filter(item => item.id !== created.id)])
      setBakeTab('templates')
      setBakeTemplateOffset(0)
      setSelectedTemplateId(created.id)
      setStatusMessage(`已新建文档「${created.title}」`)
      await refreshOverview()
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '新建文档失败'))
      return false
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

  const handleUpdateTemplate = async (templateId: string, updater: (template: ArticleTemplate) => ArticleTemplate): Promise<boolean> => {
    const target = templates.find(item => item.id === templateId)
    if (!target) return false
    try {
      const updated = await updateTemplate(updater(target))
      setTemplates(prev => prev.map(item => item.id === templateId ? updated : item))
      setStatusMessage(`已保存文档「${updated.title}」`)
      await refreshOverview()
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新文档失败'))
      return false
    }
  }

  const handleToggleTemplateStatus = async (templateId: string) => {
    try {
      const updated = await toggleTemplateStatus(templateId)
      setTemplates(prev => prev.map(item => item.id === templateId ? updated : item))
      setStatusMessage(`文档状态已切换为「${updated.status}」`)
      await refreshOverview()
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新文档状态失败'))
    }
  }

  const documentRefreshSkipLabels: Record<string, string> = {
    policy_never: '刷新策略为“从不刷新”，已跳过',
    url_missing: '该文档没有来源网址，无法刷新',
    url_invalid: '来源网址格式不合法，无法刷新',
    page_gone: '来源页面已不存在，已停止自动刷新',
    check_throttled: '近期已检查过刷新，本次跳过',
    no_update_evidence: '该文档不满足自动原地更新条件，已跳过',
    content_fresh: '内容仍在新鲜期内，无需刷新',
  }

  const handleRefreshTemplate = async (templateId: string) => {
    setRefreshingTemplateId(templateId)
    try {
      // 文档刷新直接使用一次性隐藏浏览器会话，不依赖预先打开的页签。
      const result = await refreshBakeDocument(templateId)
      if (result.document) {
        setTemplates(prev => prev.map(item => item.id === templateId ? result.document! : item))
      }
      if (result.status === 'updated') {
        setStatusMessage(result.completenessStatus === 'partial'
          ? '已取得新版本，但页面内容只完成部分采集'
          : '已验证最新来源，发现新版本')
      } else if (result.status === 'no_change') {
        setStatusMessage(result.completenessStatus === 'partial'
          ? '已检查来源页面，但只完成部分采集'
          : '已检查来源页面，内容暂无变化')
      } else if (result.status === 'skipped') {
        setStatusMessage(documentRefreshSkipLabels[result.reason ?? ''] ?? `已跳过刷新（${result.reason ?? '未知原因'}）`)
      } else {
        setStatusMessage(`刷新失败（${result.reason ?? '未知原因'}）`)
      }
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '刷新文档失败'))
    } finally {
      setRefreshingTemplateId(null)
    }
  }

  const handleSetTemplateRefreshPolicy = async (templateId: string, policy: 'auto' | 'always' | 'never'): Promise<boolean> => {
    try {
      const updated = await setTemplateRefreshPolicy(templateId, policy)
      setTemplates(prev => prev.map(item => item.id === templateId ? updated : item))
      const labels = { auto: '自动判断', always: '每次都刷新', never: '从不刷新' }
      setStatusMessage(`刷新策略已设为「${labels[policy]}」`)
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '更新刷新策略失败'))
      return false
    }
  }

  const handleDeleteTemplate = async (templateId: string): Promise<boolean> => {
    if (!(await confirmDestructive({ title: '删除这份文档？', description: '删除后无法恢复，来源时间线和其他内容会保留。' }))) return false
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
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除文档失败'))
      return false
    }
  }

  const handleViewSourceMemory = (memoryId?: string) => {
    if (!memoryId) {
      setStatusMessage('当前文档还没有关联来源时间线')
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

  const handleCreateKnowledge = async (
    input: Pick<BakeKnowledgeItem, 'summary' | 'overview' | 'detailedContent' | 'importance'>,
  ): Promise<boolean> => {
    try {
      const created = await createKnowledge(input)
      setKnowledgeItems(previous => [created, ...previous.filter(item => item.id !== created.id)])
      setKnowledgeTotal(previous => previous + 1)
      setBakeKnowledgeOffset(0)
      setSelectedKnowledgeId(created.id)
      setStatusMessage(`已新建知识「${created.summary}」`)
      await refreshOverview()
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '新建知识失败'))
      return false
    }
  }

  const handleUpdateKnowledge = async (knowledge: BakeKnowledgeItem): Promise<boolean> => {
    try {
      const updated = await updateKnowledge(knowledge)
      setKnowledgeItems(previous => previous.map(item => item.id === updated.id ? updated : item))
      setStatusMessage(`已保存知识「${updated.summary}」`)
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '保存知识失败'))
      return false
    }
  }

  const handleCreateSop = async (
    input: Pick<SopCandidate, 'extractedProblem' | 'detailedContent' | 'steps' | 'triggerKeywords'>,
  ): Promise<boolean> => {
    try {
      const created = await createSop(input)
      setSopCandidates(previous => [created, ...previous.filter(item => item.id !== created.id)])
      setSopTotal(previous => previous + 1)
      setBakeSopOffset(0)
      setSelectedSopId(created.id)
      setStatusMessage(`已新建操作「${created.extractedProblem || '未命名操作'}」`)
      await refreshOverview()
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '新建操作失败'))
      return false
    }
  }

  const handleUpdateSop = async (sop: SopCandidate): Promise<boolean> => {
    try {
      const updated = await updateSop(sop)
      setSopCandidates(previous => previous.map(item => item.id === updated.id ? updated : item))
      setStatusMessage(`已保存操作「${updated.extractedProblem || '未命名操作'}」`)
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '保存操作失败'))
      return false
    }
  }

  const handleDeleteKnowledge = async (id: string): Promise<boolean> => {
    if (!(await confirmDestructive({ title: '删除这条知识？', description: '删除后无法恢复，来源时间线和采集记录会保留。' }))) return false
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
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除知识失败'))
      return false
    }
  }

  const handleDeleteSop = async (id: string): Promise<boolean> => {
    if (!(await confirmDestructive({ title: '删除这份操作？', description: '删除后无法恢复，来源时间线和采集记录会保留。' }))) return false
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
      return true
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除操作手册失败'))
      return false
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
              border: '1px solid var(--mb-accent)',
              borderRadius: 6,
              background: 'var(--mb-bg-card)',
              color: 'var(--mb-accent)',
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
      <BakeHeader
        currentTab={bakeTab}
        backAction={bakeNavigationStack.length > 0 ? {
          label: '返回上一步',
          onClick: handleGoBack,
        } : undefined}
      />
      {statusMessage && <div className="bake-inline-message">{statusMessage}</div>}

      {/* 模型未就绪提示 */}
      {!modelStatusLoading && !modelsReady && (
        <div style={{
          margin: '12px 16px',
          padding: '12px',
          background: 'var(--mb-warning-soft)',
          border: '1px solid var(--mb-warning-border)',
          borderRadius: 8,
          fontSize: 13,
          color: 'var(--mb-warning-text)',
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
            graphDefaultDateRange={overviewTodayRange}
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
            favoriteFilter={knowledgeFavoriteFilter}
            onFavoriteFilterChange={handleKnowledgeFavoriteFilterChange}
            onToggleFavorite={handleToggleKnowledgeFavorite}
            onOpenGraph={(item) => handleOpenAssetGraph('knowledge', item.id)}
            focusId={bakeKnowledgeFocusId}
            onDeleteKnowledge={handleDeleteKnowledge}
            onCreateKnowledge={handleCreateKnowledge}
            onUpdateKnowledge={handleUpdateKnowledge}
            onViewSourceTimeline={handleViewSourceMemory}
            sourceTimelineTitle={resolvedKnowledgeItem?.sourceTimelineId ? memoryTitleById.get(resolvedKnowledgeItem.sourceTimelineId) : undefined}
          />
        )}
        {bakeTab === 'data' && (
          <BakeDataTab
            items={dataItems}
            total={dataTotal}
            offset={dataOffset}
            limit={dataLimit}
            query={dataQuery}
            draftQuery={draftDataQuery}
            sourceKind={dataSourceKind}
            draftSourceKind={draftDataSourceKind}
            from={dataFrom}
            to={dataTo}
            draftFrom={draftDataFrom}
            draftTo={draftDataTo}
            selectedId={selectedDataId}
            focusId={dataFocusId}
            loading={dataLoading}
            refreshingId={refreshingDataId}
            deletingId={deletingDataId}
            onDraftQueryChange={setDraftDataQuery}
            onDraftSourceKindChange={setDraftDataSourceKind}
            onDraftFromChange={setDraftDataFrom}
            onDraftToChange={setDraftDataTo}
            onSearch={handleSearchData}
            onClearSearch={handleClearDataSearch}
            favoriteFilter={dataFavoriteFilter}
            onFavoriteFilterChange={handleDataFavoriteFilterChange}
            onToggleFavorite={handleToggleDataFavorite}
            onOpenGraph={(item) => handleOpenAssetGraph('data', item.id)}
            onSelect={setSelectedDataId}
            onPageChange={setDataOffset}
            onLimitChange={(limit) => {
              setDataLimit(limit)
              setDataOffset(0)
            }}
            onRefresh={handleRefreshData}
            onDelete={handleDeleteData}
            onCreate={handleCreateData}
            onUpdate={handleUpdateData}
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
            docType={templateDocType}
            draftQuery={draftTemplateQuery}
            draftFrom={draftTemplateFrom}
            draftTo={draftTemplateTo}
            draftDocType={draftTemplateDocType}
            selectedTemplateId={resolvedTemplateId}
            onSelectTemplate={setSelectedTemplateId}
            onCreateTemplate={handleCreateTemplate}
            onUpdateTemplate={handleUpdateTemplate}
            onToggleTemplateStatus={handleToggleTemplateStatus}
            onDeleteTemplate={handleDeleteTemplate}
            onRefreshTemplate={handleRefreshTemplate}
            refreshingTemplateId={refreshingTemplateId}
            onSetTemplateRefreshPolicy={handleSetTemplateRefreshPolicy}
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
            onDraftDocTypeChange={setDraftTemplateDocType}
            onSearch={handleSearchTemplate}
            onClearFilters={handleClearTemplateFilters}
            favoriteFilter={templateFavoriteFilter}
            onFavoriteFilterChange={handleTemplateFavoriteFilterChange}
            onToggleFavorite={handleToggleTemplateFavorite}
            onOpenGraph={(item) => handleOpenAssetGraph('document', item.id)}
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
            onCreateSop={handleCreateSop}
            onUpdateSop={handleUpdateSop}
            onViewSourceTimeline={handleViewSourceMemory}
            sourceTimelineTitle={resolvedSopItem?.sourceTimelineId ? memoryTitleById.get(resolvedSopItem.sourceTimelineId) : undefined}
            onPageChange={setBakeSopOffset}
            onLimitChange={setBakeSopLimit}
            onDraftQueryChange={setDraftSopQuery}
            onDraftFromChange={setDraftSopFrom}
            onDraftToChange={setDraftSopTo}
            onSearch={handleSearchSop}
            onClearFilters={handleClearSopFilters}
            favoriteFilter={sopFavoriteFilter}
            onFavoriteFilterChange={handleSopFavoriteFilterChange}
            onToggleFavorite={handleToggleSopFavorite}
            onOpenGraph={(item) => handleOpenAssetGraph('operation', item.id)}
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
