import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Network } from 'lucide-react'
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
  RepositoryTab,
  SopCandidate,
  TimelineItem,
} from '../types'
import BakeCaptureTab, { captureNeedsTextRefresh, parseDateInputToMs } from './bake/BakeCaptureTab'
import BakeHeader from './bake/BakeHeader'
import BakeMemoryGraph from './bake/BakeMemoryGraph'
import type { MemoryGraphAssets } from './bake/memoryGraph'
import { BakeButton, BakeCard, BakePill, BakeSectionHeader } from './bake/BakeShared'
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
    repositoryCaptureFrom,
    repositoryCaptureTo,
    repositoryCaptureLimit,
    repositoryCaptureSourceCaptureId,
    repositoryMemoryFocusId,
    selectedTemplateId,
    selectedSopId,
    selectedKnowledgeId,
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
  const fetchSops = useFetchBakeSops()
  const fetchSop = useFetchBakeSop()
  const fetchDataSources = useFetchDataSources()
  const [memories, setMemories] = useState<TimelineItem[]>([])
  const [memoryTotal, setMemoryTotal] = useState(0)
  const [captureItems, setCaptureItems] = useState<BakeCaptureItem[]>([])
  const [captureTotal, setCaptureTotal] = useState(0)
  const [captureDetail, setCaptureDetail] = useState<BakeCaptureItem | null>(null)
  const [memoryCaptures, setMemoryCaptures] = useState<CaptureRecord[]>([])
  const [selectedMemoryRelations, setSelectedMemoryRelations] = useState<{
    document: ArticleTemplate | null
    knowledge: BakeKnowledgeItem | null
    sop: SopCandidate | null
    loading: boolean
  }>({ document: null, knowledge: null, sop: null, loading: false })
  const [statusMessage, setStatusMessage] = useState<string | null>(null)
  const [memoryPageInput, setMemoryPageInput] = useState('')
  const [draftMemoryQuery, setDraftMemoryQuery] = useState(repositoryMemoryQuery)
  const [draftMemoryFrom, setDraftMemoryFrom] = useState(repositoryMemoryFrom)
  const [draftMemoryTo, setDraftMemoryTo] = useState(repositoryMemoryTo)
  const [draftCaptureQuery, setDraftCaptureQuery] = useState(repositoryCaptureQuery)
  const [draftCaptureFrom, setDraftCaptureFrom] = useState(repositoryCaptureFrom)
  const [draftCaptureTo, setDraftCaptureTo] = useState(repositoryCaptureTo)
  const [pendingDeletion, setPendingDeletion] = useState<PendingDeletion | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [graphOpen, setGraphOpen] = useState(false)
  const [graphAssets, setGraphAssets] = useState<MemoryGraphAssets>(emptyGraphAssets)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState<string | null>(null)
  const [graphRevision, setGraphRevision] = useState(0)
  const memoryRequestSeqRef = useRef(0)
  const captureRequestSeqRef = useRef(0)

  const refreshMemories = async (offset = bakeMemoryOffset) => {
    const data = await fetchMemories({
      q: repositoryMemoryQuery.trim() || undefined,
      from: parseDateInputToMs(repositoryMemoryFrom),
      to: parseDateInputToMs(repositoryMemoryTo, true),
      limit: repositoryMemoryLimit,
      offset,
    })
    setMemories(data.items)
    setMemoryTotal(data.total)
    setSelectedMemoryId(data.items[0]?.id ?? null)
  }

  const refreshCaptures = async (
    offset = bakeCaptureOffset,
    sourceCaptureId: string | null = repositoryCaptureSourceCaptureId,
  ) => {
    const data = await fetchCaptures({
      q: repositoryCaptureQuery.trim() || undefined,
      from: parseDateInputToMs(repositoryCaptureFrom),
      to: parseDateInputToMs(repositoryCaptureTo, true),
      source_capture_id: sourceCaptureId ? Number(sourceCaptureId) : undefined,
      limit: repositoryCaptureLimit,
      offset,
    })
    setCaptureItems(data.items)
    setCaptureTotal(data.total)
    setSelectedCaptureId(data.items[0]?.id ?? null)
  }

  useEffect(() => {
    if (repositoryTab !== 'memory') return
    if (repositoryMemoryFocusId) {
      const requestSeq = memoryRequestSeqRef.current + 1
      memoryRequestSeqRef.current = requestSeq
      void fetchMemory(repositoryMemoryFocusId).then((item) => {
        if (requestSeq !== memoryRequestSeqRef.current) return
        setMemories([item])
        setMemoryTotal(1)
        setSelectedMemoryId(item.id)
      }).catch((error) => {
        if (requestSeq !== memoryRequestSeqRef.current) return
        setMemories([])
        setMemoryTotal(0)
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
      setMemories(data.items)
      setMemoryTotal(data.total)
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
    void fetchCaptures({
      q: repositoryCaptureQuery.trim() || undefined,
      from: parseDateInputToMs(repositoryCaptureFrom),
      to: parseDateInputToMs(repositoryCaptureTo, true),
      source_capture_id: repositoryCaptureSourceCaptureId ? Number(repositoryCaptureSourceCaptureId) : undefined,
      limit: repositoryCaptureLimit,
      offset: bakeCaptureOffset,
    }).then((data) => {
      if (requestSeq !== captureRequestSeqRef.current) return
      setCaptureItems(data.items)
      setCaptureTotal(data.total)
    }).catch((error) => {
      if (requestSeq !== captureRequestSeqRef.current) return
      setStatusMessage(toUserFacingError(error, '采集记录加载失败'))
    })
  }, [
    bakeCaptureOffset,
    fetchCaptures,
    repositoryCaptureFrom,
    repositoryCaptureLimit,
    repositoryCaptureQuery,
    repositoryCaptureSourceCaptureId,
    repositoryCaptureTo,
    repositoryTab,
  ])

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
    setDraftCaptureFrom(repositoryCaptureFrom)
    setDraftCaptureTo(repositoryCaptureTo)
  }, [repositoryCaptureFrom, repositoryCaptureQuery, repositoryCaptureTo])

  const resolvedMemoryId = selectedMemoryId ?? memories[0]?.id ?? null
  const resolvedCaptureId = selectedCaptureId ?? captureItems[0]?.id ?? null
  const selectedMemory = memories.find(item => item.id === resolvedMemoryId) ?? (selectedMemoryId ? null : memories[0] ?? null)

  useEffect(() => {
    if (repositoryTab !== 'memory') return
    if (memories.length === 0) {
      setSelectedMemoryId(null)
      return
    }
    if (!selectedMemoryId || !memories.some(item => item.id === selectedMemoryId)) {
      setSelectedMemoryId(memories[0].id)
    }
  }, [memories, repositoryTab, selectedMemoryId, setSelectedMemoryId])

  useEffect(() => {
    if (repositoryTab !== 'memory' || !resolvedMemoryId) {
      setSelectedMemoryRelations({ document: null, knowledge: null, sop: null, loading: false })
      return
    }

    let cancelled = false
    setSelectedMemoryRelations(prev => ({ ...prev, loading: true }))
    void Promise.all([
      fetchTemplates({ limit: 1000 }),
      fetchKnowledge({ limit: 1000 }),
      fetchSops({ limit: 1000 }),
    ]).then(([templateData, knowledgeData, sopData]) => {
      if (cancelled) return
      setSelectedMemoryRelations({
        document: templateData.items.find(template => template.sourceMemoryIds.includes(resolvedMemoryId)) ?? null,
        knowledge: knowledgeData.items.find(item => item.sourceTimelineId === resolvedMemoryId) ?? null,
        sop: sopData.items.find(item => item.sourceTimelineId === resolvedMemoryId) ?? null,
        loading: false,
      })
    }).catch(() => {
      if (!cancelled) {
        setSelectedMemoryRelations({ document: null, knowledge: null, sop: null, loading: false })
      }
    })

    return () => {
      cancelled = true
    }
  }, [fetchKnowledge, fetchSops, fetchTemplates, repositoryTab, resolvedMemoryId])

  useEffect(() => {
    if (repositoryTab !== 'capture') return
    if (captureItems.length === 0) {
      setSelectedCaptureId(null)
      setCaptureDetail(null)
      return
    }
    if (!selectedCaptureId || !captureItems.some(item => item.id === selectedCaptureId)) {
      setSelectedCaptureId(captureItems[0].id)
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

  const memoryPage = Math.floor(bakeMemoryOffset / repositoryMemoryLimit) + 1
  const memoryTotalPages = Math.max(1, Math.ceil(memoryTotal / repositoryMemoryLimit))
  const memoryFilterPills = useMemo(() => {
    const pills: string[] = []
    if (repositoryMemoryFrom) pills.push(`开始：${repositoryMemoryFrom}`)
    if (repositoryMemoryTo) pills.push(`结束：${repositoryMemoryTo}`)
    return pills
  }, [repositoryMemoryFrom, repositoryMemoryTo])

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
      repositoryCaptureFrom: draftCaptureFrom,
      repositoryCaptureTo: draftCaptureTo,
      repositoryCaptureSourceCaptureId: null,
      bakeCaptureOffset: 0,
    })
  }

  const handleClearCaptureFilters = () => {
    clearBakeNavigationStack()
    setDraftCaptureQuery('')
    setDraftCaptureFrom('')
    setDraftCaptureTo('')
    useAppStore.setState({
      repositoryCaptureQuery: '',
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
      setSelectedMemoryRelations({ document: null, knowledge: null, sop: null, loading: false })
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
      const { items: templates } = await fetchTemplates({ limit: 1000 })
      const relatedDoc = templates.find(template => template.sourceMemoryIds.includes(timelineId))
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
      const { items: knowledgeItems } = await fetchKnowledge({ limit: 1000 })
      const relatedKnowledge = knowledgeItems.find(item => item.sourceTimelineId === timelineId)
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
      const { items: sops } = await fetchSops({ limit: 1000 })
      const relatedSop = sops.find(item => item.sourceTimelineId === timelineId)
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
      <BakeHeader title="采集" subtitle="" />
      {bakeNavigationStack.length > 0 && (
        <div className="bake-backbar">
          <BakeButton compact onClick={handleCaptureGoBack}>返回上一步</BakeButton>
        </div>
      )}
      {statusMessage && <div className="bake-inline-message">{statusMessage}</div>}
      <div className="bake-tabs-shell">
        <section className="bake-tabs bake-tabs--scroll">
          {tabs.map(tab => (
            <BakeButton key={tab.key} active={repositoryTab === tab.key} onClick={() => handleRepositoryTabChange(tab.key)}>
              {tab.label}
            </BakeButton>
          ))}
        </section>
        {repositoryTab === 'memory' && (
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
                      placeholder="搜索时间线标题、摘要或详情"
                    />
                  </label>
                  <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--search">
                    <BakeButton compact primary type="submit">搜索</BakeButton>
                  </div>
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
                    {(draftMemoryQuery || draftMemoryFrom || draftMemoryTo || repositoryMemoryQuery || repositoryMemoryFrom || repositoryMemoryTo || repositoryMemoryFocusId) && (
                      <BakeButton compact onClick={handleClearMemoryFilters}>清除筛选</BakeButton>
                    )}
                  </div>
                </div>
              </div>
            </form>

            {memoryFilterPills.length > 0 && (
              <div className="bake-filter-summary">
                {memoryFilterPills.map(item => <BakePill key={item} text={item} />)}
              </div>
            )}
          </>
        )}
        {repositoryTab === 'memory' && (
          <div className="bake-split-list-detail bake-split-list-detail--memories-fixed">
            <BakeCard className="bake-memory-list-card bake-memory-list-card--fixed">
              <BakeSectionHeader
                title="时间线"
              />

              {memories.length === 0 ? (
                <div className="bake-muted">当前筛选条件下没有可浏览的时间线。</div>
              ) : (
                <>
                  <div className="bake-list bake-memory-list bake-memory-list--paged">
                    {memories.map(item => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setSelectedMemoryId(item.id)}
                        className={`bake-list-item bake-memory-list-item bake-memory-list-item--compact ${item.id === selectedMemory?.id ? 'bake-list-item--active' : ''}`.trim()}
                      >
                        <div className="bake-list-item__title bake-line-clamp-1">{item.title}</div>
                        <div className="bake-muted bake-line-clamp-2">{item.summary || '暂无摘要'}</div>
                        <div className="bake-memory-list-item__meta">
                          <span>ID #{item.id} · 创建于 {formatMemoryTime(item)}</span>
                        </div>
                      </button>
                    ))}
                  </div>
                  <div className="bake-pagination bake-pagination--extended">
                    <div className="bake-pagination__controls">
                      <BakeButton compact onClick={() => setBakeMemoryOffset(Math.max(0, bakeMemoryOffset - repositoryMemoryLimit))}>上一页</BakeButton>
                      <BakeButton compact onClick={() => setBakeMemoryOffset(bakeMemoryOffset + repositoryMemoryLimit)}>
                        {bakeMemoryOffset + repositoryMemoryLimit >= memoryTotal ? '已到底' : '下一页'}
                      </BakeButton>
                    </div>
                    <div className="bake-pagination__summary-group bake-muted">
                      <span className="bake-pagination__summary">共 {memoryTotal} 条</span>
                      <span className="bake-pagination__summary">第 {memoryPage}/{memoryTotalPages} 页</span>
                    </div>
                    <div className="bake-pagination__right">
                      <label className="bake-pagination__field">
                        <span className="bake-muted">每页</span>
                        <select
                          className="bake-input bake-pagination__select"
                          value={String(repositoryMemoryLimit)}
                          aria-label="每页条数"
                          onChange={(event) => setRepositoryMemoryLimit(Number(event.target.value))}
                        >
                          {[10, 20, 50, 100].map(option => (
                            <option key={option} value={option}>{option} 条</option>
                          ))}
                        </select>
                      </label>
                      <div className="bake-pagination__jump">
                        <span className="bake-muted">第</span>
                        <input
                          className="bake-input bake-pagination__input"
                          type="number"
                          min={1}
                          max={memoryTotalPages}
                          value={memoryPageInput}
                          onChange={(event) => setMemoryPageInput(event.target.value)}
                          placeholder={String(memoryPage)}
                          aria-label="跳转页码"
                        />
                        <span className="bake-muted">页</span>
                        <BakeButton
                          compact
                          onClick={() => {
                            const target = Number(memoryPageInput)
                            if (!Number.isFinite(target) || target < 1) return
                            const nextPage = Math.min(memoryTotalPages, Math.floor(target))
                            setBakeMemoryOffset((nextPage - 1) * repositoryMemoryLimit)
                            setMemoryPageInput('')
                          }}
                        >
                          前往
                        </BakeButton>
                      </div>
                    </div>
                  </div>
                </>
              )}
            </BakeCard>

            <BakeCard className="bake-memory-detail-card bake-memory-detail-card--stacked">
              {selectedMemory ? (
                <div className="bake-memory-detail bake-memory-detail--fixed">
                  <div className="bake-memory-detail__header-block">
                    <div className="bake-inline-meta">
                      <div style={{ minWidth: 0 }}>
                        <div className="bake-title" style={{ fontSize: 20, lineHeight: 1.4 }}>{selectedMemory.title}</div>
                        <div className="bake-muted bake-line-clamp-1" style={{ marginTop: 6 }}>
                          ID #{selectedMemory.id || '暂无编号'} · 创建于 {formatMemoryTime(selectedMemory)}
                        </div>
                      </div>
                    </div>
                  </div>

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
                    const items = segments.length > 0 ? segments.map(seg => {
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
                    }) : (() => {
                      const itemMap = new Map<string, { ids: number[]; captures: CaptureRecord[] }>()
                      memoryCaptures.forEach(cap => {
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
                    })()

                    return (
                      <div className="bake-memory-action-card">
                        <div className="bake-kv__title">详细内容</div>
                        <div style={{ marginTop: 12 }}>
                          <div style={{ fontWeight: 600, marginBottom: 12, color: '#333' }}>{timeRange}</div>
                          <div style={{ paddingLeft: 12, borderLeft: '2px solid #e0e0e0' }}>
                            {items.map((item, idx) => (
                              <div key={idx} style={{ marginBottom: 12, fontSize: 13, lineHeight: 1.6 }}>
                                <div style={{ marginBottom: 4 }}>
                                  <span style={{ fontWeight: 600, color: '#666', marginRight: 8 }}>{item.itemTimeRange}</span>
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
                                        style={{ color: '#0066cc', textDecoration: 'none', fontSize: 12 }}
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
                      <BakeButton compact onClick={() => handleViewRelatedDocument(selectedMemory.id)}>关联文档</BakeButton>
                      <BakeButton compact onClick={() => handleViewRelatedKnowledge(selectedMemory.id)}>关联知识</BakeButton>
                      <BakeButton compact onClick={() => handleViewRelatedSop(selectedMemory.id)}>关联操作</BakeButton>
                      <BakeButton compact danger onClick={() => setPendingDeletion({ kind: 'memory', id: selectedMemory.id })}>删除时间线</BakeButton>
                    </div>
                    <div className="bake-related-summary">
                      <div className="bake-related-row">
                        <span className="bake-related-row__label">来源采集记录</span>
                        <span className="bake-related-row__value">{selectedMemory.sourceCaptureId ? `采集记录 #${selectedMemory.sourceCaptureId}` : '暂无'}</span>
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
            </BakeCard>
          </div>
        )}
        {repositoryTab === 'capture' && (
          <BakeCaptureTab
            captures={captureItems}
            total={captureTotal}
            limit={repositoryCaptureLimit}
            offset={bakeCaptureOffset}
            query={repositoryCaptureQuery}
            from={repositoryCaptureFrom}
            to={repositoryCaptureTo}
            draftQuery={draftCaptureQuery}
            draftFrom={draftCaptureFrom}
            draftTo={draftCaptureTo}
            sourceCaptureId={repositoryCaptureSourceCaptureId}
            selectedCaptureId={resolvedCaptureId}
            selectedCaptureDetail={captureDetail}
            onSelectCapture={setSelectedCaptureId}
            onPageChange={setBakeCaptureOffset}
            onLimitChange={setRepositoryCaptureLimit}
            onDraftQueryChange={setDraftCaptureQuery}
            onDraftFromChange={setDraftCaptureFrom}
            onDraftToChange={setDraftCaptureTo}
            onSearch={handleSearchCaptures}
            onClearFilters={handleClearCaptureFilters}
            onViewLinkedTimeline={handleViewLinkedTimeline}
            onDeleteCapture={(id) => setPendingDeletion({ kind: 'capture', id })}
            canGoBack={Boolean(captureBackTarget)}
            onGoBack={handleCaptureGoBack}
          />
        )}
        </div>
        {graphOpen && repositoryTab === 'memory' && (
          <BakeMemoryGraph
            assets={graphAssets}
            loading={graphLoading}
            error={graphError}
            mode="dock"
            onClose={() => setGraphOpen(false)}
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
