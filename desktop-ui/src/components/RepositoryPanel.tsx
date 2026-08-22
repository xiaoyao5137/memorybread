import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  useDeleteBakeCapture,
  useDeleteBakeMemory,
  useFetchDataSources,
  useFetchBakeMemory,
  useFetchBakeMemories,
  useFetchBakeCaptureDetail,
  useFetchBakeCaptures,
  useFetchBakeKnowledge,
  useFetchBakeKnowledgeDetail,
  useFetchBakeMemoryRelations,
  useFetchBakeSop,
  useFetchBakeSops,
  useFetchBakeTemplates,
  useFetchCaptures,
} from '../hooks/useApi'
import { useAppStore, type BakeNavigationTarget } from '../store/useAppStore'
import { toUserFacingError } from '../utils/userFacingError'
import type {
  ArticleTemplate,
  BakeCaptureItem,
  BakeKnowledgeItem,
  CaptureRecord,
  DataSource,
  RepositoryTab,
  SopCandidate,
  TimelineItem,
} from '../types'
import BakeCaptureTab, { captureNeedsTextRefresh, parseDateInputToMs } from './bake/BakeCaptureTab'
import BakeHeader from './bake/BakeHeader'
import BakeMemoryGraph from './bake/BakeMemoryGraph'
import type { MemoryGraphAssets } from './bake/memoryGraph'
import { BakeDetailDrawer, BakeRecordTable, BakeTableActionButton, type BakeRecordColumn } from './bake/BakeRecordTable'
import { BakeButton } from './bake/BakeShared'
import './bake/BakePanel.css'

const getFallbackOffsetAfterRemoval = (currentCount: number, offset: number, limit: number) => (
  currentCount <= 1 && offset > 0 ? Math.max(0, offset - limit) : offset
)

const CAPTURE_TEXT_REFRESH_INTERVAL_MS = 2_000
const GRAPH_ASSET_LIMIT = 100

const emptyGraphAssets: MemoryGraphAssets = {
  knowledge: [],
  documents: [],
  operations: [],
  data: [],
  totals: {},
}

type PendingDeletion = {
  kind: 'memory' | 'capture'
  id: string
}

const formatMemoryTime = (item: Pick<TimelineItem, 'createdAt' | 'createdAtMs'>) => {
  if (item.createdAtMs > 0) {
    return new Date(item.createdAtMs).toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  }
  return item.createdAt || '创建时间未知'
}

const RepositoryPanel: React.FC = () => {
  const {
    repositoryTab,
    selectedMemoryId,
    selectedCaptureId,
    bakeMemoryOffset,
    bakeCaptureOffset,
    repositoryMemoryQuery,
    repositoryMemoryFrom,
    repositoryMemoryTo,
    repositoryMemoryLimit,
    repositoryCaptureQuery,
    repositoryCaptureApp,
    repositoryCaptureFrom,
    repositoryCaptureTo,
    repositoryCaptureLimit,
    repositoryCaptureSourceCaptureId,
    repositoryMemoryFocusId,
    repositoryMemoryItems: memories,
    repositoryMemoryTotal: memoryTotal,
    repositoryMemoryDrawerOpen: memoryDrawerOpen,
    selectedTemplateId,
    selectedSopId,
    selectedKnowledgeId,
    bakeDataFocusId,
    setWindowMode,
    setBakeTab,
    setRepositoryTab,
    setSelectedMemoryId,
    setSelectedKnowledgeId,
    setSelectedTemplateId,
    setSelectedSopId,
    setSelectedCaptureId,
    setRepositoryMemoryFocusId,
    setBakeTemplateFocusId,
    setBakeKnowledgeFocusId,
    setBakeSopFocusId,
    setBakeDataFocusId,
    setBakeMemoryOffset,
    setBakeCaptureOffset,
    setRepositoryMemoryLimit,
    setRepositoryCaptureLimit,
    setRepositoryCaptureSourceCaptureId,
    captureBackTarget,
    bakeNavigationStack,
    pushBakeNavigationTarget,
    popBakeNavigationTarget,
    clearBakeNavigationStack,
  } = useAppStore()

  const fetchMemories = useFetchBakeMemories()
  const fetchMemory = useFetchBakeMemory()
  const deleteMemory = useDeleteBakeMemory()
  const fetchCaptures = useFetchBakeCaptures()
  const fetchCaptureDetail = useFetchBakeCaptureDetail()
  const deleteCapture = useDeleteBakeCapture()
  const fetchCapturesRaw = useFetchCaptures()
  const fetchTemplates = useFetchBakeTemplates()
  const fetchKnowledge = useFetchBakeKnowledge()
  const fetchKnowledgeDetail = useFetchBakeKnowledgeDetail()
  const fetchTimelineRelations = useFetchBakeMemoryRelations()
  const fetchSops = useFetchBakeSops()
  const fetchSop = useFetchBakeSop()
  const fetchDataSources = useFetchDataSources()
  const [captureItems, setCaptureItems] = useState<BakeCaptureItem[]>([])
  const [captureTotal, setCaptureTotal] = useState(0)
  const [captureDetail, setCaptureDetail] = useState<BakeCaptureItem | null>(null)
  const [memoryCaptures, setMemoryCaptures] = useState<CaptureRecord[]>([])
  const [selectedMemoryRelations, setSelectedMemoryRelations] = useState<{
    document: ArticleTemplate | null
    knowledge: BakeKnowledgeItem | null
    sop: SopCandidate | null
    data: DataSource | null
    loading: boolean
  }>({ document: null, knowledge: null, sop: null, data: null, loading: false })
  const [statusMessage, setStatusMessage] = useState<string | null>(null)
  const [draftMemoryQuery, setDraftMemoryQuery] = useState(repositoryMemoryQuery)
  const [draftMemoryFrom, setDraftMemoryFrom] = useState(repositoryMemoryFrom)
  const [draftMemoryTo, setDraftMemoryTo] = useState(repositoryMemoryTo)
  const [draftCaptureQuery, setDraftCaptureQuery] = useState(repositoryCaptureQuery)
  const [draftCaptureApp, setDraftCaptureApp] = useState(repositoryCaptureApp)
  const [draftCaptureFrom, setDraftCaptureFrom] = useState(repositoryCaptureFrom)
  const [draftCaptureTo, setDraftCaptureTo] = useState(repositoryCaptureTo)
  const [pendingDeletion, setPendingDeletion] = useState<PendingDeletion | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [graphOpen, setGraphOpen] = useState(false)
  const [graphFocusTimelineId, setGraphFocusTimelineId] = useState<string | null>(null)
  const [graphAssets, setGraphAssets] = useState<MemoryGraphAssets>(emptyGraphAssets)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState<string | null>(null)
  const [graphRevision, setGraphRevision] = useState(0)
  const memoryRequestSeqRef = useRef(0)
  const captureRequestSeqRef = useRef(0)
  // 标记当前 captureItems 是否来自一次已完成的列表请求；
  // 关联跳转进入采集页时列表尚未返回，避免此时把跳转选中的记录误清
  const captureListLoadedRef = useRef(false)
  const memoryDetailTriggerRef = useRef<HTMLButtonElement | null>(null)

  const refreshMemories = async (offset = bakeMemoryOffset) => {
    const data = await fetchMemories({
      q: repositoryMemoryQuery.trim() || undefined,
      from: parseDateInputToMs(repositoryMemoryFrom),
      to: parseDateInputToMs(repositoryMemoryTo, true),
      limit: repositoryMemoryLimit,
      offset,
    })
    useAppStore.setState({
      repositoryMemoryItems: data.items,
      repositoryMemoryTotal: data.total,
      selectedMemoryId: null,
    })
  }

  const refreshCaptures = async (
    offset = bakeCaptureOffset,
    sourceCaptureId: string | null = repositoryCaptureSourceCaptureId,
  ) => {
    const data = await fetchCaptures({
      q: repositoryCaptureQuery.trim() || undefined,
      app: repositoryCaptureApp.trim() || undefined,
      from: parseDateInputToMs(repositoryCaptureFrom),
      to: parseDateInputToMs(repositoryCaptureTo, true),
      source_capture_id: sourceCaptureId ? Number(sourceCaptureId) : undefined,
      limit: repositoryCaptureLimit,
      offset,
    })
    setCaptureItems(data.items)
    setCaptureTotal(data.total)
    setSelectedCaptureId(null)
    setCaptureDetail(null)
  }

  useEffect(() => {
    if (repositoryTab !== 'memory') return
    if (repositoryMemoryFocusId) {
    const requestSeq = memoryRequestSeqRef.current + 1
    memoryRequestSeqRef.current = requestSeq
    void fetchMemory(repositoryMemoryFocusId).then((item) => {
      if (requestSeq !== memoryRequestSeqRef.current) return
      // 列表、选中态、抽屉开关一次 store 更新全部到位，避免本地状态与 zustand
      // 混用时 React 用旧列表渲染新选中态，导致选中态被清理 effect 误清
      useAppStore.setState({
        repositoryMemoryItems: [item],
        repositoryMemoryTotal: 1,
        selectedMemoryId: item.id,
        // 关联跳转的目标时间线到达后直接展开详情抽屉，不依赖自动打开 effect 的时序
        repositoryMemoryDrawerOpen: true,
      })
    }).catch((error) => {
      if (requestSeq !== memoryRequestSeqRef.current) return
      useAppStore.setState({
        repositoryMemoryFocusId: null,
        repositoryMemoryItems: [],
        repositoryMemoryTotal: 0,
        selectedMemoryId: null,
      })
      setStatusMessage(toUserFacingError(error, '未找到这条时间线'))
    })
    return
  }
    const requestSeq = memoryRequestSeqRef.current + 1
    memoryRequestSeqRef.current = requestSeq
    void fetchMemories({
      q: repositoryMemoryQuery.trim() || undefined,
      from: parseDateInputToMs(repositoryMemoryFrom),
      to: parseDateInputToMs(repositoryMemoryTo, true),
      limit: repositoryMemoryLimit,
      offset: bakeMemoryOffset,
    }).then((data) => {
      if (requestSeq !== memoryRequestSeqRef.current) return
      useAppStore.setState({ repositoryMemoryItems: data.items, repositoryMemoryTotal: data.total })
    }).catch((error) => {
      if (requestSeq !== memoryRequestSeqRef.current) return
      setStatusMessage(toUserFacingError(error, '时间线加载失败'))
    })
  }, [
    bakeMemoryOffset,
    fetchMemories,
    fetchMemory,
    repositoryMemoryFocusId,
    repositoryMemoryFrom,
    repositoryMemoryLimit,
    repositoryMemoryQuery,
    repositoryMemoryTo,
    repositoryTab,
    setSelectedMemoryId,
  ])

  useEffect(() => {
    if (repositoryTab !== 'capture') return
    const requestSeq = captureRequestSeqRef.current + 1
    captureRequestSeqRef.current = requestSeq
    captureListLoadedRef.current = false
    void fetchCaptures({
      q: repositoryCaptureQuery.trim() || undefined,
      app: repositoryCaptureApp.trim() || undefined,
      from: parseDateInputToMs(repositoryCaptureFrom),
      to: parseDateInputToMs(repositoryCaptureTo, true),
      source_capture_id: repositoryCaptureSourceCaptureId ? Number(repositoryCaptureSourceCaptureId) : undefined,
      limit: repositoryCaptureLimit,
      offset: bakeCaptureOffset,
    }).then((data) => {
      if (requestSeq !== captureRequestSeqRef.current) return
      captureListLoadedRef.current = true
      setCaptureItems(data.items)
      setCaptureTotal(data.total)
    }).catch((error) => {
      if (requestSeq !== captureRequestSeqRef.current) return
      captureListLoadedRef.current = true
      setStatusMessage(toUserFacingError(error, '采集记录加载失败'))
    })
  }, [
    bakeCaptureOffset,
    fetchCaptures,
    repositoryCaptureApp,
    repositoryCaptureFrom,
    repositoryCaptureLimit,
    repositoryCaptureQuery,
    repositoryCaptureSourceCaptureId,
    repositoryCaptureTo,
    repositoryTab,
  ])

  // 关联跳转时 store 已清空应用筛选，这里同步清空草稿输入框，
  // 避免工具栏残留旧筛选文本与实际列表口径不一致
  useEffect(() => {
    if (!repositoryCaptureSourceCaptureId) return
    setDraftCaptureQuery('')
    setDraftCaptureApp('')
    setDraftCaptureFrom('')
    setDraftCaptureTo('')
  }, [repositoryCaptureSourceCaptureId])

  useEffect(() => {
    if (repositoryTab !== 'capture' || !selectedCaptureId) {
      setCaptureDetail(null)
      return
    }

    let cancelled = false
    let refreshTimer: number | null = null
    let isInitialRequest = true

    const loadCaptureDetail = async () => {
      try {
        const item = await fetchCaptureDetail(selectedCaptureId)
        if (cancelled) return

        setCaptureDetail(item)
        if (captureNeedsTextRefresh(item)) {
          refreshTimer = window.setTimeout(() => {
            refreshTimer = null
            void loadCaptureDetail()
          }, CAPTURE_TEXT_REFRESH_INTERVAL_MS)
        }
      } catch (error) {
        if (!cancelled && isInitialRequest) {
          setStatusMessage(toUserFacingError(error, '采集记录详情加载失败'))
        }
      } finally {
        isInitialRequest = false
      }
    }

    void loadCaptureDetail()
    return () => {
      cancelled = true
      if (refreshTimer != null) window.clearTimeout(refreshTimer)
    }
  }, [fetchCaptureDetail, repositoryTab, selectedCaptureId])

  useEffect(() => {
    if (!statusMessage) return
    const timer = window.setTimeout(() => setStatusMessage(null), 2400)
    return () => window.clearTimeout(timer)
  }, [statusMessage])

  useEffect(() => {
    if (!pendingDeletion || isDeleting) return

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setPendingDeletion(null)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isDeleting, pendingDeletion])

  useEffect(() => {
    if (!graphOpen || repositoryTab !== 'memory') return
    let cancelled = false
    setGraphLoading(true)
    setGraphError(null)

    void Promise.allSettled([
      fetchKnowledge({ sort: 'heat', limit: GRAPH_ASSET_LIMIT, offset: 0 }),
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
  }, [fetchDataSources, fetchKnowledge, fetchSops, fetchTemplates, graphOpen, graphRevision, repositoryTab])

  useEffect(() => {
    setDraftMemoryQuery(repositoryMemoryQuery)
    setDraftMemoryFrom(repositoryMemoryFrom)
    setDraftMemoryTo(repositoryMemoryTo)
  }, [repositoryMemoryFrom, repositoryMemoryQuery, repositoryMemoryTo])

  useEffect(() => {
    setDraftCaptureQuery(repositoryCaptureQuery)
    setDraftCaptureApp(repositoryCaptureApp)
    setDraftCaptureFrom(repositoryCaptureFrom)
    setDraftCaptureTo(repositoryCaptureTo)
  }, [repositoryCaptureApp, repositoryCaptureFrom, repositoryCaptureQuery, repositoryCaptureTo])

  const resolvedMemoryId = selectedMemoryId
  const resolvedCaptureId = selectedCaptureId
  const selectedMemory = memories.find(item => item.id === resolvedMemoryId) ?? null
  const graphFocusTimeline = graphFocusTimelineId
    ? memories.find(item => item.id === graphFocusTimelineId) ?? null
    : null

  // 从时间线行打开图谱时，只展示由该时间线提炼出的记忆资产，形成“这条时间线的记忆图谱”。
  const graphAssetsForRender = useMemo<MemoryGraphAssets>(() => {
    if (!graphFocusTimelineId) return graphAssets
    const knowledge = graphAssets.knowledge.filter(item => item.sourceTimelineId === graphFocusTimelineId)
    const documents = graphAssets.documents.filter(item => item.sourceMemoryIds.includes(graphFocusTimelineId))
    const operations = graphAssets.operations.filter(item => item.sourceTimelineId === graphFocusTimelineId)
    const data = graphAssets.data.filter(item => (
      (item.latest_snapshot?.source_timeline_ids ?? []).map(String).includes(graphFocusTimelineId)
    ))
    return {
      knowledge,
      documents,
      operations,
      data,
      totals: {
        knowledge: knowledge.length,
        document: documents.length,
        operation: operations.length,
        data: data.length,
      },
    }
  }, [graphAssets, graphFocusTimelineId])

  const graphFocusNodeId = useMemo(() => {
    if (!graphFocusTimelineId) return null
    const { knowledge, documents, operations, data } = graphAssetsForRender
    if (knowledge.length > 0) return `knowledge:${knowledge[0].id}`
    if (documents.length > 0) return `document:${documents[0].id}`
    if (operations.length > 0) return `operation:${operations[0].id}`
    if (data.length > 0) return `data:${data[0].id}`
    return null
  }, [graphAssetsForRender, graphFocusTimelineId])

  const handleOpenTimelineGraph = (item: TimelineItem) => {
    setGraphFocusTimelineId(item.id)
    setGraphOpen(true)
  }

  const handleCloseGraph = () => {
    setGraphOpen(false)
    setGraphFocusTimelineId(null)
  }

  useEffect(() => {
    if (repositoryTab !== 'memory') return
    if (selectedMemoryId && !memories.some(item => item.id === selectedMemoryId)) {
      useAppStore.setState({ selectedMemoryId: null, repositoryMemoryDrawerOpen: false })
    }
  }, [memories, repositoryTab, selectedMemoryId, setSelectedMemoryId])

  useEffect(() => {
    if (
      repositoryTab === 'memory'
      && repositoryMemoryFocusId
      && selectedMemory
      && String(selectedMemory.id) === String(repositoryMemoryFocusId)
    ) {
      useAppStore.setState({ repositoryMemoryDrawerOpen: true })
    }
  }, [repositoryMemoryFocusId, repositoryTab, selectedMemory?.id])

  useEffect(() => {
    if (repositoryTab !== 'memory' || !resolvedMemoryId) {
      setSelectedMemoryRelations({ document: null, knowledge: null, sop: null, data: null, loading: false })
      return
    }

    let cancelled = false
    setSelectedMemoryRelations(prev => ({ ...prev, loading: true }))
    // 定向接口按来源时间线查关联产物；列表接口分页上限会截断窗口外的关联，
    // 不能再拉全量列表后按 sourceTimelineId 客户端过滤
    void fetchTimelineRelations(resolvedMemoryId).then((relations) => {
      if (cancelled) return
      setSelectedMemoryRelations({
        document: relations.document,
        knowledge: relations.knowledge,
        sop: relations.sop,
        data: relations.data,
        loading: false,
      })
    }).catch(() => {
      if (!cancelled) {
        setSelectedMemoryRelations({ document: null, knowledge: null, sop: null, data: null, loading: false })
      }
    })

    return () => {
      cancelled = true
    }
  }, [fetchTimelineRelations, repositoryTab, resolvedMemoryId])

  useEffect(() => {
    if (repositoryTab !== 'capture') return
    // 列表还在加载时不清理选中态，保证关联跳转带入的 selectedCaptureId 能存活到列表返回
    if (!captureListLoadedRef.current) return
    if (captureItems.length === 0) {
      setSelectedCaptureId(null)
      setCaptureDetail(null)
      return
    }
    if (selectedCaptureId && !captureItems.some(item => item.id === selectedCaptureId)) {
      setSelectedCaptureId(null)
      setCaptureDetail(null)
    }
  }, [captureItems, repositoryTab, selectedCaptureId, setSelectedCaptureId])

  useEffect(() => {
    const memory = memories.find(m => m.id === selectedMemoryId)
    if (!memory?.captureIds || memory.captureIds.length === 0) {
      setMemoryCaptures([])
      return
    }
    void fetchCapturesRaw({ ids: memory.captureIds.join(','), limit: 500 }).then(data => {
      setMemoryCaptures(data.captures.sort((a, b) => a.ts - b.ts))
    }).catch(() => setMemoryCaptures([]))
  }, [selectedMemoryId, memories, fetchCapturesRaw])

  const openMemoryDrawer = (item: TimelineItem, trigger: HTMLButtonElement) => {
    memoryDetailTriggerRef.current = trigger
    useAppStore.setState({ selectedMemoryId: item.id, repositoryMemoryDrawerOpen: true })
  }

  const closeMemoryDrawer = () => {
    const trigger = memoryDetailTriggerRef.current
    useAppStore.setState({ repositoryMemoryDrawerOpen: false, selectedMemoryId: null, repositoryMemoryFocusId: null })
    window.setTimeout(() => trigger?.focus(), 0)
  }

  const memoryColumns: BakeRecordColumn<TimelineItem>[] = [
    {
      key: 'created',
      label: '新增时间',
      className: 'bake-record-table__time',
      render: item => <><div>{formatMemoryTime(item)}</div><div className="bake-record-table__secondary">ID #{item.id}</div></>,
    },
    {
      key: 'title',
      label: '时间线标题',
      className: 'bake-record-table__title',
      render: item => <div className="bake-record-table__primary bake-line-clamp-2">{item.title || '未命名时间线'}</div>,
    },
    {
      key: 'summary',
      label: '内容摘要',
      render: item => <div className="bake-record-table__preview bake-line-clamp-2">{item.summary || '暂无摘要'}</div>,
    },
    {
      key: 'captures',
      label: '关联采集',
      className: 'bake-record-table__status',
      render: item => <span className="bake-record-table__badge">{item.captureIds?.length ?? 0} 条</span>,
    },
  ]

  const handleSearchMemories = () => {
    clearBakeNavigationStack()
    setSelectedMemoryId(null)
    setRepositoryMemoryFocusId(null)
    useAppStore.setState({
      repositoryMemoryFocusId: null,
      repositoryMemoryQuery: draftMemoryQuery,
      repositoryMemoryFrom: draftMemoryFrom,
      repositoryMemoryTo: draftMemoryTo,
      bakeMemoryOffset: 0,
    })
  }

  const handleClearMemoryFilters = () => {
    clearBakeNavigationStack()
    setDraftMemoryQuery('')
    setDraftMemoryFrom('')
    setDraftMemoryTo('')
    setSelectedMemoryId(null)
    useAppStore.setState({
      repositoryMemoryFocusId: null,
      repositoryMemoryQuery: '',
      repositoryMemoryFrom: '',
      repositoryMemoryTo: '',
      bakeMemoryOffset: 0,
    })
  }

  const handleSearchCaptures = () => {
    clearBakeNavigationStack()
    setSelectedCaptureId(null)
    setCaptureDetail(null)
    useAppStore.setState({
      repositoryCaptureQuery: draftCaptureQuery,
      repositoryCaptureApp: draftCaptureApp,
      repositoryCaptureFrom: draftCaptureFrom,
      repositoryCaptureTo: draftCaptureTo,
      repositoryCaptureSourceCaptureId: null,
      bakeCaptureOffset: 0,
    })
  }

  const handleClearCaptureFilters = () => {
    clearBakeNavigationStack()
    setDraftCaptureQuery('')
    setDraftCaptureApp('')
    setDraftCaptureFrom('')
    setDraftCaptureTo('')
    useAppStore.setState({
      repositoryCaptureQuery: '',
      repositoryCaptureApp: '',
      repositoryCaptureFrom: '',
      repositoryCaptureTo: '',
      repositoryCaptureSourceCaptureId: null,
      bakeCaptureOffset: 0,
    })
  }

  const deleteMemoryAndRefresh = async (id: string) => {
    try {
      await deleteMemory(id)
      clearBakeNavigationStack()
      const nextOffset = getFallbackOffsetAfterRemoval(memories.length, bakeMemoryOffset, repositoryMemoryLimit)
      setRepositoryMemoryFocusId(null)
      setSelectedMemoryId(null)
      setMemoryCaptures([])
      setSelectedMemoryRelations({ document: null, knowledge: null, sop: null, data: null, loading: false })
      if (nextOffset !== bakeMemoryOffset) {
        setBakeMemoryOffset(nextOffset)
      } else {
        await refreshMemories(nextOffset)
      }
      setStatusMessage('已删除时间线')
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除时间线失败'))
    }
  }

  const deleteCaptureAndRefresh = async (id: string) => {
    try {
      await deleteCapture(id)
      clearBakeNavigationStack()
      const nextOffset = getFallbackOffsetAfterRemoval(captureItems.length, bakeCaptureOffset, repositoryCaptureLimit)
      const clearsSourceScope = repositoryCaptureSourceCaptureId === id
      if (clearsSourceScope) {
        setRepositoryCaptureSourceCaptureId(null)
      }
      setSelectedCaptureId(null)
      setCaptureDetail(null)
      if (nextOffset !== bakeCaptureOffset) {
        setBakeCaptureOffset(nextOffset)
      } else {
        await refreshCaptures(nextOffset, clearsSourceScope ? null : repositoryCaptureSourceCaptureId)
      }
      setStatusMessage('已删除采集记录')
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '删除采集记录失败'))
    }
  }

  const confirmDeletion = async () => {
    if (!pendingDeletion || isDeleting) return

    const deletion = pendingDeletion
    setIsDeleting(true)
    try {
      if (deletion.kind === 'memory') {
        await deleteMemoryAndRefresh(deletion.id)
      } else {
        await deleteCaptureAndRefresh(deletion.id)
      }
      setPendingDeletion(null)
    } finally {
      setIsDeleting(false)
    }
  }

  const handleRepositoryTabChange = (tab: RepositoryTab) => {
    if (tab === repositoryTab) return
    clearBakeNavigationStack()
    setRepositoryTab(tab)
  }

  const currentNavigationTarget = () => ({
    windowMode: 'knowledge' as const,
    repositoryTab,
    selectedMemoryId: resolvedMemoryId,
    selectedCaptureId: resolvedCaptureId,
    selectedTemplateId,
    selectedSopId,
    selectedKnowledgeId,
    repositoryCaptureSourceCaptureId,
    repositoryMemoryFocusId,
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

  const handleViewLinkedKnowledge = (knowledgeId?: string | null) => {
    if (!knowledgeId) {
      setStatusMessage('当前时间线尚未提炼出 bake 知识')
      return
    }
    pushBakeNavigationTarget(currentNavigationTarget())
    setWindowMode('bake')
    setBakeTab('knowledge')
    setBakeKnowledgeFocusId(knowledgeId)
    setSelectedKnowledgeId(knowledgeId)
    setStatusMessage('已切换到关联知识')
  }

  const handleViewRelatedDocument = async (timelineId: string) => {
    try {
      const relatedDoc = (await fetchTimelineRelations(timelineId)).document
      if (!relatedDoc) {
        setStatusMessage('当前时间线还没有关联文档')
        return
      }
      pushBakeNavigationTarget(currentNavigationTarget())
      setWindowMode('bake')
      setBakeTab('templates')
      setBakeTemplateFocusId(relatedDoc.id)
      setSelectedTemplateId(relatedDoc.id)
      setStatusMessage(`已切换到关联文档「${relatedDoc.title}」`)
    } catch (error) {
      setStatusMessage('查询关联文档失败')
    }
  }

  const handleViewRelatedKnowledge = async (timelineId: string) => {
    try {
      const relatedKnowledge = (await fetchTimelineRelations(timelineId)).knowledge
      if (!relatedKnowledge) {
        setStatusMessage('当前时间线还没有关联知识')
        return
      }
      const focusedKnowledge = await fetchKnowledgeDetail(relatedKnowledge.id).catch(() => relatedKnowledge)
      pushBakeNavigationTarget(currentNavigationTarget())
      setWindowMode('bake')
      setBakeTab('knowledge')
      setBakeKnowledgeFocusId(focusedKnowledge.id)
      setSelectedKnowledgeId(focusedKnowledge.id)
      setStatusMessage(`已切换到关联知识「${focusedKnowledge.summary}」`)
    } catch {
      setStatusMessage('查询关联知识失败')
    }
  }

  const handleViewRelatedSop = async (timelineId: string) => {
    try {
      const relatedSop = (await fetchTimelineRelations(timelineId)).sop
      if (!relatedSop) {
        setStatusMessage('当前时间线还没有关联操作')
        return
      }
      const focusedSop = await fetchSop(relatedSop.id).catch(() => relatedSop)
      pushBakeNavigationTarget(currentNavigationTarget())
      setWindowMode('bake')
      setBakeTab('sop')
      setBakeSopFocusId(focusedSop.id)
      setSelectedSopId(focusedSop.id)
      setStatusMessage(`已切换到关联操作「${focusedSop.extractedProblem || focusedSop.sourceTitle || focusedSop.id}」`)
    } catch {
      setStatusMessage('查询关联操作失败')
    }
  }

  const handleViewLinkedTimeline = (timelineId?: string | null) => {
    if (!timelineId) {
      setStatusMessage('该采集尚未归入任何时间线')
      return
    }
    pushBakeNavigationTarget(currentNavigationTarget())
    setWindowMode('knowledge')
    setRepositoryTab('memory')
    setRepositoryMemoryFocusId(timelineId)
    setSelectedMemoryId(timelineId)
    setStatusMessage('已切换到所属时间线')
  }

  const handleViewRelatedData = async (timelineId: string) => {
    try {
      const relatedData = (await fetchTimelineRelations(timelineId)).data
      if (!relatedData) {
        setStatusMessage('当前时间线还没有关联数据')
        return
      }
      pushBakeNavigationTarget(currentNavigationTarget())
      setWindowMode('bake')
      setBakeTab('data')
      setBakeDataFocusId(String(relatedData.id))
      setStatusMessage(`已切换到关联数据「${relatedData.title}」`)
    } catch {
      setStatusMessage('查询关联数据失败')
    }
  }

  const handleCaptureGoBack = () => {
    if (!captureBackTarget) {
      setStatusMessage('当前没有可返回的上一步页面')
      return
    }

    const target = popBakeNavigationTarget()
    if (!target) return
    restoreNavigationTarget(target)
    setStatusMessage('已返回上一步页面')
  }

  const tabs: Array<{ key: RepositoryTab; label: string }> = [
    { key: 'memory', label: '时间线' },
    { key: 'capture', label: '采集记录' },
  ]

  return (
    <div className="bake-panel bake-panel--repository">
      <BakeHeader
        title="采集"
        subtitle=""
        backAction={bakeNavigationStack.length > 0 ? {
          label: '返回上一步',
          onClick: handleCaptureGoBack,
        } : undefined}
      />
      {statusMessage && <div className="bake-inline-message">{statusMessage}</div>}
      <div className="bake-tabs-shell">
        <section className="bake-tabs bake-tabs--scroll">
          {tabs.map(tab => (
            <BakeButton key={tab.key} active={repositoryTab === tab.key} onClick={() => handleRepositoryTabChange(tab.key)}>
              {tab.label}
            </BakeButton>
          ))}
        </section>
      </div>

      <div className={`bake-graph-workspace ${graphOpen && repositoryTab === 'memory' ? 'bake-graph-workspace--open' : ''}`.trim()}>
        <div className="bake-tab-content">
        {repositoryTab === 'memory' && (
          <>
            <form
              className="bake-list-toolbar bake-list-toolbar--repository"
              onSubmit={(event) => {
                event.preventDefault()
                handleSearchMemories()
              }}
            >
              <div className="bake-list-toolbar__repository">
                <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--search">
                  <label className="bake-form-field bake-filter-field bake-filter-field--search">
                    <span className="bake-filter-label">关键词</span>
                    <input
                      className="bake-input"
                      value={draftMemoryQuery}
                      onChange={(event) => setDraftMemoryQuery(event.target.value)}
                      placeholder="搜索时间线 ID、标题、摘要或详情"
                    />
                  </label>
                </div>
                <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--dates">
                  <label className="bake-form-field bake-filter-field">
                    <span className="bake-filter-label">开始日期</span>
                    <input
                      className="bake-input"
                      type="date"
                      value={draftMemoryFrom}
                      onChange={(event) => setDraftMemoryFrom(event.target.value)}
                    />
                  </label>
                  <label className="bake-form-field bake-filter-field">
                    <span className="bake-filter-label">结束日期</span>
                    <input
                      className="bake-input"
                      type="date"
                      value={draftMemoryTo}
                      onChange={(event) => setDraftMemoryTo(event.target.value)}
                    />
                  </label>
                  <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
                    <div className="bake-list-toolbar__repository-primary-actions">
                      <BakeButton compact type="button" onClick={handleClearMemoryFilters}>清空</BakeButton>
                      <BakeButton compact primary type="submit">搜索</BakeButton>
                    </div>
                  </div>
                </div>
              </div>
            </form>

          </>
        )}
        {repositoryTab === 'memory' && (
          <>
            <BakeRecordTable
              items={memories}
              total={memoryTotal}
              limit={repositoryMemoryLimit}
              offset={bakeMemoryOffset}
              columns={memoryColumns}
              getRowId={item => item.id}
              ariaLabel="时间线表格"
              emptyTitle={(repositoryMemoryQuery || repositoryMemoryFrom || repositoryMemoryTo || repositoryMemoryFocusId) ? '没有匹配的时间线' : '还没有时间线'}
              emptyDescription={(repositoryMemoryQuery || repositoryMemoryFrom || repositoryMemoryTo || repositoryMemoryFocusId) ? '调整关键词或日期后再试。' : '采集内容形成时间线后，会展示在这里。'}
              activeId={memoryDrawerOpen ? selectedMemory?.id : null}
              itemLabel="条时间线"
              onPageChange={setBakeMemoryOffset}
              onLimitChange={setRepositoryMemoryLimit}
              renderActions={item => (
                <>
                  <BakeTableActionButton kind="detail" label={`查看时间线：${item.title || '未命名时间线'}`} onClick={trigger => openMemoryDrawer(item, trigger)} />
                  <BakeTableActionButton kind="graph" label={`在记忆图谱中查看时间线「${item.title || '未命名时间线'}」`} onClick={() => handleOpenTimelineGraph(item)} />
                </>
              )}
            />

            <BakeDetailDrawer
              open={Boolean(memoryDrawerOpen && selectedMemory)}
              wide
              eyebrow="时间线详情"
              title={selectedMemory?.title || '未命名时间线'}
              meta={selectedMemory ? <>ID #{selectedMemory.id} · 新增于 {formatMemoryTime(selectedMemory)} · {selectedMemory.captureIds?.length ?? 0} 条采集</> : undefined}
              closeLabel="关闭时间线详情"
              onClose={closeMemoryDrawer}
            >
              {selectedMemory ? (
                <div className="bake-memory-detail bake-memory-detail--fixed">
                  <div className="bake-memory-action-card">
                    <div className="bake-kv__title">时间线摘要</div>
                    <div className="bake-muted" style={{ lineHeight: 1.8 }}>{selectedMemory.summary || '暂无摘要'}</div>
                  </div>

                  {memoryCaptures.length > 0 && (() => {
                    const minTs = memoryCaptures[0].ts
                    const maxTs = memoryCaptures[memoryCaptures.length - 1].ts
                    const minDate = new Date(minTs)
                    const maxDate = new Date(maxTs)
                    const timeRange = `${minDate.getMonth() + 1}月${minDate.getDate()}日 ${minDate.getHours()}:${String(minDate.getMinutes()).padStart(2, '0')}-${maxDate.getHours()}:${String(maxDate.getMinutes()).padStart(2, '0')}`

                    const segments = selectedMemory.keyTimestamps || []
                    // 无片段或片段未覆盖全部采集时，按应用/窗口维度分组的兜底展示
                    const buildCaptureGroupItems = (captures: CaptureRecord[]) => {
                      const itemMap = new Map<string, { ids: number[]; captures: CaptureRecord[] }>()
                      captures.forEach(cap => {
                        const key = `${cap.app_name}|${cap.win_title || ''}`
                        if (!itemMap.has(key)) {
                          itemMap.set(key, { ids: [], captures: [] })
                        }
                        const item = itemMap.get(key)!
                        item.ids.push(cap.id)
                        item.captures.push(cap)
                      })
                      return Array.from(itemMap.values()).map(item => {
                        const minTs = Math.min(...item.captures.map(c => c.ts))
                        const maxTs = Math.max(...item.captures.map(c => c.ts))
                        const minDate = new Date(minTs)
                        const maxDate = new Date(maxTs)
                        const itemTimeRange = minTs === maxTs
                          ? `${minDate.getHours()}:${String(minDate.getMinutes()).padStart(2, '0')}`
                          : `${minDate.getHours()}:${String(minDate.getMinutes()).padStart(2, '0')}-${maxDate.getHours()}:${String(maxDate.getMinutes()).padStart(2, '0')}`
                        const text = item.captures.map(c => c.ocr_text || c.ax_text || '').join(' ').trim()
                        const summary = text.slice(0, 60) + (text.length > 60 ? '...' : '')
                        return { ids: item.ids, itemTimeRange, summary: summary || `${item.captures[0].app_name}活动` }
                      })
                    }
                    const items = segments.length > 0 ? (() => {
                      const segmentItems = segments.map(seg => {
                        const minDate = new Date(seg.start_ts)
                        const maxDate = new Date(seg.end_ts)
                        const segmentCaptureIds = seg.capture_ids.length > 0
                          ? seg.capture_ids
                          : memoryCaptures
                            .filter(capture => capture.ts >= seg.start_ts && capture.ts <= seg.end_ts)
                            .map(capture => capture.id)
                        const itemTimeRange = seg.start_ts === seg.end_ts
                          ? `${minDate.getHours()}:${String(minDate.getMinutes()).padStart(2, '0')}`
                          : `${minDate.getHours()}:${String(minDate.getMinutes()).padStart(2, '0')}-${maxDate.getHours()}:${String(maxDate.getMinutes()).padStart(2, '0')}`
                        return {
                          ids: segmentCaptureIds,
                          itemTimeRange,
                          summary: seg.summary
                        }
                      })
                      // 历史合并数据可能存在 keyTimestamps 只覆盖部分采集的情况，
                      // 将未被任何片段引用的采集按应用/窗口补齐，保证与列表页关联采集数一致。
                      const coveredIds = new Set(segments.flatMap(seg => seg.capture_ids))
                      const uncoveredCaptures = memoryCaptures.filter(capture =>
                        !coveredIds.has(capture.id)
                        && !segments.some(seg => capture.ts >= seg.start_ts && capture.ts <= seg.end_ts)
                      )
                      return uncoveredCaptures.length > 0
                        ? [...segmentItems, ...buildCaptureGroupItems(uncoveredCaptures)]
                        : segmentItems
                    })() : buildCaptureGroupItems(memoryCaptures)

                    return (
                      <div className="bake-memory-action-card">
                        <div className="bake-kv__title">详细内容</div>
                        <div style={{ marginTop: 12 }}>
                          <div style={{ fontWeight: 600, marginBottom: 12, color: 'var(--mb-text-primary)' }}>{timeRange}</div>
                          <div style={{ paddingLeft: 12, borderLeft: '2px solid var(--mb-border-strong)' }}>
                            {items.map((item, idx) => (
                              <div key={idx} style={{ marginBottom: 12, fontSize: 13, lineHeight: 1.6 }}>
                                <div style={{ marginBottom: 4 }}>
                                  <span style={{ fontWeight: 600, color: 'var(--mb-text-secondary)', marginRight: 8 }}>{item.itemTimeRange}</span>
                                  <span>{item.summary}</span>
                                </div>
                                <div>
                                  {item.ids.map((id, i) => (
                                    <span key={id}>
                                      <a
                                        href="#"
                                        onClick={(e) => {
                                          e.preventDefault()
                                          pushBakeNavigationTarget({
                                            windowMode: 'knowledge',
                                            repositoryTab: 'memory',
                                            selectedMemoryId: selectedMemory.id,
                                          })
                                          setRepositoryTab('capture')
                                          setRepositoryCaptureSourceCaptureId(String(id))
                                          setSelectedCaptureId(String(id))
                                          setStatusMessage(`已切换到采集记录 #${id}`)
                                        }}
                                        style={{ color: 'var(--mb-link)', textDecoration: 'none', fontSize: 12 }}
                                      >
                                        #{id}
                                      </a>
                                      {i < item.ids.length - 1 && ', '}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )
                  })()}

                  <div className="bake-memory-action-card bake-memory-action-card--secondary">
                    <div>
                      <div className="bake-kv__title">回溯</div>
                    </div>
                    <div className="bake-actions bake-actions--secondary bake-memory-detail__action-copy">
                      <BakeButton compact onClick={() => {
                        if (!selectedMemory.sourceCaptureId) {
                          setStatusMessage('当前时间线暂无来源采集记录')
                          return
                        }
                        pushBakeNavigationTarget({
                          windowMode: 'knowledge',
                          repositoryTab: 'memory',
                          selectedMemoryId: selectedMemory.id,
                        })
                        setRepositoryTab('capture')
                        setRepositoryCaptureSourceCaptureId(selectedMemory.sourceCaptureId)
                        setSelectedCaptureId(selectedMemory.sourceCaptureId)
                        setStatusMessage('已切换到来源采集记录')
                      }}>来源采集记录</BakeButton>
                      <BakeButton compact onClick={() => handleViewRelatedData(selectedMemory.id)}>关联数据</BakeButton>
                      <BakeButton compact onClick={() => handleViewRelatedDocument(selectedMemory.id)}>关联文档</BakeButton>
                      <BakeButton compact onClick={() => handleViewRelatedKnowledge(selectedMemory.id)}>关联知识</BakeButton>
                      <BakeButton compact onClick={() => handleViewRelatedSop(selectedMemory.id)}>关联操作</BakeButton>
                      <BakeButton compact danger onClick={() => setPendingDeletion({ kind: 'memory', id: selectedMemory.id })}>删除</BakeButton>
                    </div>
                    <div className="bake-related-summary">
                      <div className="bake-related-row">
                        <span className="bake-related-row__label">来源采集记录</span>
                        <span className="bake-related-row__value">{selectedMemory.sourceCaptureId ? `采集记录 #${selectedMemory.sourceCaptureId}` : '暂无'}</span>
                      </div>
                      <div className="bake-related-row">
                        <span className="bake-related-row__label">关联数据</span>
                        <span className="bake-related-row__value">
                          {selectedMemoryRelations.loading ? '查询中...' : selectedMemoryRelations.data?.title ?? '暂无'}
                        </span>
                      </div>
                      <div className="bake-related-row">
                        <span className="bake-related-row__label">关联文档</span>
                        <span className="bake-related-row__value">
                          {selectedMemoryRelations.loading ? '查询中...' : selectedMemoryRelations.document?.title ?? '暂无'}
                        </span>
                      </div>
                      <div className="bake-related-row">
                        <span className="bake-related-row__label">关联知识</span>
                        <span className="bake-related-row__value">
                          {selectedMemoryRelations.loading ? '查询中...' : selectedMemoryRelations.knowledge?.summary ?? '暂无'}
                        </span>
                      </div>
                      <div className="bake-related-row">
                        <span className="bake-related-row__label">关联操作</span>
                        <span className="bake-related-row__value">
                          {selectedMemoryRelations.loading
                            ? '查询中...'
                            : selectedMemoryRelations.sop?.extractedProblem || selectedMemoryRelations.sop?.sourceTitle || '暂无'}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="bake-muted">暂无时间线详情</div>
              )}
            </BakeDetailDrawer>
          </>
        )}
        {repositoryTab === 'capture' && (
          <BakeCaptureTab
            captures={captureItems}
            total={captureTotal}
            limit={repositoryCaptureLimit}
            offset={bakeCaptureOffset}
            query={repositoryCaptureQuery}
            app={repositoryCaptureApp}
            from={repositoryCaptureFrom}
            to={repositoryCaptureTo}
            draftQuery={draftCaptureQuery}
            draftApp={draftCaptureApp}
            draftFrom={draftCaptureFrom}
            draftTo={draftCaptureTo}
            sourceCaptureId={repositoryCaptureSourceCaptureId}
            selectedCaptureId={resolvedCaptureId}
            selectedCaptureDetail={captureDetail}
            onSelectCapture={setSelectedCaptureId}
            onPageChange={setBakeCaptureOffset}
            onLimitChange={setRepositoryCaptureLimit}
            onDraftQueryChange={setDraftCaptureQuery}
            onDraftAppChange={setDraftCaptureApp}
            onDraftFromChange={setDraftCaptureFrom}
            onDraftToChange={setDraftCaptureTo}
            onSearch={handleSearchCaptures}
            onClearFilters={handleClearCaptureFilters}
            onViewLinkedTimeline={handleViewLinkedTimeline}
            onDeleteCapture={(id) => setPendingDeletion({ kind: 'capture', id })}
            onRefresh={handleSearchCaptures}
          />
        )}
        </div>
        {graphOpen && repositoryTab === 'memory' && (
          <BakeMemoryGraph
            assets={graphAssetsForRender}
            focusNodeId={graphFocusNodeId}
            loading={graphLoading}
            error={graphError}
            mode="dock"
            defaultScopeLabel={graphFocusTimeline ? `时间线「${graphFocusTimeline.title || '未命名时间线'}」` : undefined}
            onClose={handleCloseGraph}
            onRetry={() => setGraphRevision(current => current + 1)}
          />
        )}
      </div>
      {pendingDeletion && (
        <div
          className="bake-modal-overlay"
          onClick={() => {
            if (!isDeleting) setPendingDeletion(null)
          }}
        >
          <section
            className="bake-modal bake-modal--confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="repository-delete-title"
            aria-describedby="repository-delete-description"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="bake-modal__header">
              <h3 id="repository-delete-title">
                {pendingDeletion.kind === 'memory' ? '删除时间线？' : '删除采集记录？'}
              </h3>
              <button
                type="button"
                className="bake-modal__close"
                aria-label="关闭"
                disabled={isDeleting}
                onClick={() => setPendingDeletion(null)}
              >
                ×
              </button>
            </div>
            <div className="bake-modal__body bake-modal__body--confirm">
              <p id="repository-delete-description">
                {pendingDeletion.kind === 'memory'
                  ? '删除后无法恢复，关联采集记录、文档、操作和知识会保留。'
                  : '对应截图会一并删除，已生成的时间线和其他内容会保留。'}
              </p>
            </div>
            <div className="bake-modal__footer">
              <button
                type="button"
                className="bake-btn bake-btn--compact"
                autoFocus
                disabled={isDeleting}
                onClick={() => setPendingDeletion(null)}
              >
                取消
              </button>
              <BakeButton compact danger disabled={isDeleting} onClick={() => void confirmDeletion()}>
                {isDeleting ? '删除中…' : '确认删除'}
              </BakeButton>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}

export default RepositoryPanel
