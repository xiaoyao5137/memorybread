import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  ArrowDownToLine,
  ArrowRight,
  ArrowUpFromLine,
  BookOpen,
  BookOpenText,
  Boxes,
  Braces,
  Check,
  CheckCircle2,
  ChevronRight,
  Code2,
  Copy,
  Database,
  Download,
  ExternalLink,
  Eye,
  FileArchive,
  FileCode2,
  FolderInput,
  FolderOpen,
  Loader2,
  PackageCheck,
  PackagePlus,
  Play,
  RefreshCw,
  ScrollText,
  Search,
  TerminalSquare,
  Upload,
  X,
  type LucideIcon,
} from 'lucide-react'
import {
  agentSkillZipBytes,
  importAgentSkillPackage,
  importAgentSkillZip,
  listLocalCreationSkills,
  saveLocalCreationSkill,
  skillFileText,
  type LocalCreationSkill,
} from '../utils/creationSkills'
import {
  copyIntegrationArtifact,
  downloadIntegrationArtifact,
  downloadIntegrationSkillBundle,
  downloadIntegrationSkillFile,
  getIntegrationSkill,
  getIntegrationSkillRun,
  listIntegrationMemoryOptions,
  listIntegrationSkillRuns,
  listIntegrationSkills,
  openDownloadedFile,
  pickLocalDirectory,
  revealDownloadedFile,
  saveDownloadedBytes,
  selectedFilesToIntegrationInput,
  startIntegrationSkillRun,
  type IntegrationDirection,
  type IntegrationMemoryOption,
  type IntegrationSkillCatalogItem,
  type IntegrationSkillDetail,
  type IntegrationSkillInputFile,
  type IntegrationSkillRun,
  type IntegrationRunMode,
} from '../utils/integrationSkills'
import { toUserFacingError } from '../utils/userFacingError'
import { openExternalUrl } from '../utils/appMetadata'
import { useAppStore } from '../store/useAppStore'
import MemoryBackupSection from './MemoryBackupSection'
import TutorialLink, { TUTORIAL_URLS } from './TutorialLink'
import './IntegrationPanel.css'
import './IntegrationWorkbench.css'

type IntegrationTab = IntegrationDirection | 'backup'
type WorkbenchView = 'run' | 'files'

const INTEGRATION_SKILL_CATEGORY: Record<IntegrationDirection, string> = {
  input: 'integration-input',
  output: 'integration-output',
}

const SKILL_ICONS: Record<string, LucideIcon> = {
  obsidian: BookOpenText,
  'obsidian-export': BookOpenText,
  qdrant: Database,
  milvus: Boxes,
  'chroma-pgvector': Braces,
  workbuddy: Boxes,
  'qianwen-office': FileArchive,
  codex: Code2,
  'claude-code': Code2,
}

/** 这几款通过 Skill 包导出集成，不需要在本工作台执行。 */
const SKILL_EXPORT_ONLY_IDS = new Set(['workbuddy', 'qianwen-office', 'codex', 'claude-code'])

/** 集成教程用户手册地址。 */
const SKILL_MANUAL_URLS: Record<string, string> = {
  obsidian: 'https://my.feishu.cn/wiki/MV06wgJruizAOMkoM1DcCJLFnHg',
  'obsidian-export': 'https://my.feishu.cn/wiki/MV06wgJruizAOMkoM1DcCJLFnHg',
  workbuddy: 'https://my.feishu.cn/wiki/IPXKwXyamiuCuSkdq3Ecoj9lnjc',
  'claude-code': 'https://my.feishu.cn/wiki/FTY4w5FY0iDMfck6hDVcuxLVnve',
  codex: 'https://my.feishu.cn/wiki/MRVGwtCJxikku4k7fQLcEyJanMe',
  'qianwen-office': 'https://my.feishu.cn/wiki/HMTqwXgniiRDW4ktYGycGBTbnwe',
}

const TABS: Array<{ id: IntegrationTab; label: string; description: string; icon: LucideIcon }> = [
  { id: 'input', label: '输入', description: '导入外部记忆', icon: ArrowDownToLine },
  { id: 'output', label: '输出', description: '导出上下文或安装 Skill', icon: ArrowUpFromLine },
  { id: 'backup', label: '备份与恢复', description: '备份本地记忆与恢复', icon: RefreshCw },
]

const RUN_STATUS_COPY: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '执行失败',
}

const IntegrationPanel: React.FC = () => {
  const apiBaseUrl = useAppStore(state => state.apiBaseUrl)
  const [activeTab, setActiveTab] = useState<IntegrationTab>('input')
  const [skills, setSkills] = useState<IntegrationSkillCatalogItem[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [catalogError, setCatalogError] = useState('')
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null)
  const [workbenchView, setWorkbenchView] = useState<WorkbenchView>('run')
  const [skillDetail, setSkillDetail] = useState<IntegrationSkillDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [viewingFilePath, setViewingFilePath] = useState('')
  const [inputFiles, setInputFiles] = useState<IntegrationSkillInputFile[]>([])
  const [encodingFiles, setEncodingFiles] = useState(false)
  const [query, setQuery] = useState('')
  const [resultLimit, setResultLimit] = useState(8)
  const [vaultPath, setVaultPath] = useState(
    () => window.localStorage.getItem('memorybread.obsidian-export.vaultPath') || '',
  )
  const [vaultSubfolder, setVaultSubfolder] = useState(
    () => window.localStorage.getItem('memorybread.obsidian-export.subfolder') || 'MemoryBread',
  )
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<number[]>([])
  const [runs, setRuns] = useState<IntegrationSkillRun[]>([])
  const [activeRun, setActiveRun] = useState<IntegrationSkillRun | null>(null)
  const [runPending, setRunPending] = useState(false)
  const [workbenchError, setWorkbenchError] = useState('')
  const [notice, setNotice] = useState('')
  const [downloadToast, setDownloadToast] = useState<{ id: number; title: string; path: string | null } | null>(null)
  const downloadToastTimerRef = useRef<number | undefined>(undefined)
  const dataInputRef = useRef<HTMLInputElement>(null)
  const workbenchRef = useRef<HTMLElement>(null)

  const [customSkills, setCustomSkills] = useState<LocalCreationSkill[]>([])
  const [customSkillsLoading, setCustomSkillsLoading] = useState(true)
  const [customSkillsError, setCustomSkillsError] = useState('')
  const [uploadingDirection, setUploadingDirection] = useState<IntegrationDirection | null>(null)
  const [customSkillDetail, setCustomSkillDetail] = useState<LocalCreationSkill | null>(null)
  const skillPackageInputRef = useRef<HTMLInputElement>(null)
  const skillZipInputRef = useRef<HTMLInputElement>(null)
  const pendingDirectionRef = useRef<IntegrationDirection>('input')

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true)
    setCatalogError('')
    try {
      setSkills(await listIntegrationSkills(apiBaseUrl))
    } catch (error) {
      setCatalogError(toUserFacingError(error, '读取可执行 Skill 失败'))
    } finally {
      setCatalogLoading(false)
    }
  }, [apiBaseUrl])

  const loadCustomSkills = useCallback(async () => {
    setCustomSkillsLoading(true)
    setCustomSkillsError('')
    try {
      const localSkills = await listLocalCreationSkills(apiBaseUrl)
      setCustomSkills(localSkills.filter(skill => (
        skill.categoryId === INTEGRATION_SKILL_CATEGORY.input
        || skill.categoryId === INTEGRATION_SKILL_CATEGORY.output
      )))
    } catch (error) {
      setCustomSkillsError(toUserFacingError(error, '读取自定义 Skill 失败'))
    } finally {
      setCustomSkillsLoading(false)
    }
  }, [apiBaseUrl])

  useEffect(() => {
    void loadCatalog()
    void loadCustomSkills()
  }, [loadCatalog, loadCustomSkills])

  useEffect(() => {
    window.localStorage.setItem('memorybread.obsidian-export.vaultPath', vaultPath)
  }, [vaultPath])

  useEffect(() => {
    window.localStorage.setItem('memorybread.obsidian-export.subfolder', vaultSubfolder)
  }, [vaultSubfolder])

  useEffect(() => () => window.clearTimeout(downloadToastTimerRef.current), [])

  const showDownloadToast = useCallback((title: string, path: string | null) => {
    window.clearTimeout(downloadToastTimerRef.current)
    setDownloadToast({ id: Date.now(), title, path })
    downloadToastTimerRef.current = window.setTimeout(() => setDownloadToast(null), 8000)
  }, [])

  const notifyDownloadSaved = useCallback((title: string, savedPath: string | null | undefined) => {
    if (savedPath) showDownloadToast(title, savedPath)
  }, [showDownloadToast])

  const selectedSkill = useMemo(
    () => skills.find(skill => skill.id === selectedSkillId) || null,
    [selectedSkillId, skills],
  )
  const selectedFile = skillDetail?.files.find(file => file.path === viewingFilePath) || null
  const visibleSkills = activeTab === 'backup' ? [] : skills.filter(skill => skill.direction === activeTab)
  const visibleCustomSkills = activeTab === 'backup'
    ? []
    : customSkills.filter(skill => skill.categoryId === INTEGRATION_SKILL_CATEGORY[activeTab])
  const runInputReady = selectedSkill
    ? selectedSkill.inputKind === 'folder' || selectedSkill.inputKind === 'files'
      ? inputFiles.length > 0
      : selectedSkill.inputKind === 'query'
        ? query.trim().length >= 2
        : selectedSkill.inputKind === 'memory_pick'
          ? selectedMemoryIds.length > 0
            && selectedMemoryIds.length <= MEMORY_EXPORT_MAX
            && vaultPath.trim().length > 0
          : true
    : false

  const openWorkbench = useCallback(async (skill: IntegrationSkillCatalogItem, view: WorkbenchView) => {
    setSelectedSkillId(skill.id)
    setWorkbenchView(SKILL_EXPORT_ONLY_IDS.has(skill.id) ? 'files' : view)
    setSkillDetail(null)
    setViewingFilePath('')
    setInputFiles([])
    setQuery('')
    setSelectedMemoryIds([])
    setActiveRun(null)
    setRuns([])
    setWorkbenchError('')
    setNotice('')
    setDetailLoading(true)
    try {
      const [detail, history] = await Promise.all([
        getIntegrationSkill(apiBaseUrl, skill.id),
        listIntegrationSkillRuns(apiBaseUrl, skill.id, 20),
      ])
      setSkillDetail(detail)
      setRuns(history)
      setActiveRun(history[0] || null)
      setViewingFilePath(detail.files[0]?.path || '')
    } catch (error) {
      setWorkbenchError(toUserFacingError(error, '打开 Skill 工作台失败'))
    } finally {
      setDetailLoading(false)
      window.setTimeout(() => {
        const workbench = workbenchRef.current
        if (typeof workbench?.scrollIntoView === 'function') {
          workbench.scrollIntoView({ behavior: 'smooth', block: 'start' })
        }
      }, 0)
    }
  }, [apiBaseUrl])

  useEffect(() => {
    if (!activeRun || !['queued', 'running'].includes(activeRun.status)) return undefined
    const timeout = window.setTimeout(() => {
      void getIntegrationSkillRun(apiBaseUrl, activeRun.id).then(run => {
        setActiveRun(run)
        setRuns(current => [run, ...current.filter(item => item.id !== run.id)])
      }).catch(error => {
        setWorkbenchError(toUserFacingError(error, '刷新执行状态失败'))
      })
    }, 700)
    return () => window.clearTimeout(timeout)
  }, [activeRun, apiBaseUrl])

  const handleTabChange = (tab: IntegrationTab) => {
    setActiveTab(tab)
    setSelectedSkillId(null)
    setSkillDetail(null)
    setCustomSkillDetail(null)
    setNotice('')
  }

  const handleDataFilesSelected = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files ? Array.from(event.target.files) : []
    event.target.value = ''
    if (!files.length) return
    setEncodingFiles(true)
    setWorkbenchError('')
    try {
      const encoded = await selectedFilesToIntegrationInput(files)
      setInputFiles(encoded)
      setNotice(`已在本机读取 ${encoded.length} 个文件，执行前不会写入记忆库`)
    } catch (error) {
      setWorkbenchError(toUserFacingError(error, '读取本地文件失败'))
    } finally {
      setEncodingFiles(false)
    }
  }

  const handleRun = async (mode: IntegrationRunMode) => {
    if (!selectedSkill) return
    setRunPending(true)
    setWorkbenchError('')
    setNotice('')
    try {
      const run = await startIntegrationSkillRun(apiBaseUrl, selectedSkill.id, {
        mode,
        files: inputFiles,
        config: selectedSkill.inputKind === 'query'
          ? { query, limit: resultLimit }
          : selectedSkill.inputKind === 'memory_pick'
            ? {
                memoryIds: selectedMemoryIds,
                vaultPath: vaultPath.trim(),
                subfolder: vaultSubfolder.trim() || 'MemoryBread',
              }
            : {},
      })
      setActiveRun(run)
      setRuns(current => [run, ...current.filter(item => item.id !== run.id)])
      setWorkbenchView('run')
      setNotice(mode === 'preview' ? '预检已启动，请在右侧查看状态' : '记忆导入已启动，请在右侧查看记忆输出')
    } catch (error) {
      setWorkbenchError(toUserFacingError(error, '启动 Skill 失败'))
    } finally {
      setRunPending(false)
    }
  }

  const openSkillPackagePicker = (direction: IntegrationDirection) => {
    pendingDirectionRef.current = direction
    skillPackageInputRef.current?.click()
  }

  const openSkillZipPicker = (direction: IntegrationDirection) => {
    pendingDirectionRef.current = direction
    skillZipInputRef.current?.click()
  }

  const handleSkillPackageSelected = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files ? Array.from(event.target.files) : []
    event.target.value = ''
    if (!files.length) return
    const direction = pendingDirectionRef.current
    setUploadingDirection(direction)
    setNotice('')
    setCustomSkillsError('')
    try {
      const imported = await importAgentSkillPackage(files)
      const saved = await saveLocalCreationSkill(apiBaseUrl, {
        ...imported,
        categoryId: INTEGRATION_SKILL_CATEGORY[direction],
      })
      setCustomSkills(current => [saved, ...current.filter(skill => skill.id !== saved.id)])
      setNotice(`${saved.title} 已保存为自定义${direction === 'input' ? '输入' : '输出'} Skill`)
    } catch (error) {
      setCustomSkillsError(toUserFacingError(error, '上传 Skill 失败'))
    } finally {
      setUploadingDirection(null)
    }
  }

  const handleSkillZipSelected = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const archive = event.target.files?.[0]
    event.target.value = ''
    if (!archive) return
    const direction = pendingDirectionRef.current
    setUploadingDirection(direction)
    setNotice('')
    setCustomSkillsError('')
    try {
      const imported = await importAgentSkillZip(archive)
      const saved = await saveLocalCreationSkill(apiBaseUrl, {
        ...imported,
        categoryId: INTEGRATION_SKILL_CATEGORY[direction],
      })
      setCustomSkills(current => [saved, ...current.filter(skill => skill.id !== saved.id)])
      setNotice(`${saved.title} 已从 ZIP 技能包导入`)
    } catch (error) {
      setCustomSkillsError(toUserFacingError(error, '上传 ZIP Skill 失败'))
    } finally {
      setUploadingDirection(null)
    }
  }

  const downloadCustomSkill = async (skill: LocalCreationSkill) => {
    setCustomSkillsError('')
    try {
      const bytes = new Uint8Array(agentSkillZipBytes(skill.title, skill.packageFiles || []))
      const savedPath = await saveDownloadedBytes(`${skill.title}.zip`, bytes)
      notifyDownloadSaved(`${skill.title} Skill ZIP 已保存`, savedPath)
    } catch (error) {
      setCustomSkillsError(toUserFacingError(error, '下载 Skill ZIP 失败'))
    }
  }

  return (
    <div className="integration-panel">
      <header className="integration-hero">
        <div className="integration-hero__copy">
          <span className="integration-hero__eyebrow">Local integration workshop</span>
          <h1>集成</h1>
        </div>
        <div className="integration-route" aria-label="本地数据经过可审计 Skill 执行后进入记忆库或形成工作产物">
          <div className="integration-route__node">
            <FolderInput size={18} aria-hidden />
            <span>记忆来源</span>
          </div>
          <div className="integration-route__line" aria-hidden><span /><ArrowRight size={14} /></div>
          <div className="integration-route__node integration-route__node--core">
            <TerminalSquare size={18} aria-hidden />
            <span>记忆导入</span>
          </div>
          <div className="integration-route__line" aria-hidden><span /><ArrowRight size={14} /></div>
          <div className="integration-route__node">
            <ScrollText size={18} aria-hidden />
            <span>记忆输出</span>
          </div>
        </div>
      </header>

      <nav className="integration-tabs" role="tablist" aria-label="集成类型">
        {TABS.map(tab => {
          const Icon = tab.icon
          return (
            <button
              key={tab.id}
              id={`integration-tab-${tab.id}`}
              className={activeTab === tab.id ? 'is-active' : ''}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`integration-panel-${tab.id}`}
              onClick={() => handleTabChange(tab.id)}
            >
              <Icon size={17} aria-hidden />
              <span><strong>{tab.label}</strong><small>{tab.description}</small></span>
            </button>
          )
        })}
      </nav>

      <input
        ref={skillPackageInputRef}
        className="integration-skill-input"
        type="file"
        multiple
        aria-label="选择包含 SKILL.md 的技能文件夹"
        onChange={handleSkillPackageSelected}
        {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
      />
      <input
        ref={skillZipInputRef}
        className="integration-skill-input"
        type="file"
        accept=".zip,application/zip"
        aria-label="选择 Agent Skills ZIP 技能包"
        onChange={handleSkillZipSelected}
      />
      <input
        key={`${selectedSkill?.id || 'none'}-${selectedSkill?.inputKind || 'none'}`}
        ref={dataInputRef}
        className="integration-skill-input"
        type="file"
        multiple
        accept={selectedSkill?.accept || undefined}
        aria-label="选择 Skill 输入文件"
        onChange={handleDataFilesSelected}
        {...(selectedSkill?.inputKind === 'folder'
          ? ({ webkitdirectory: '', directory: '' } as Record<string, string>)
          : {})}
      />

      {activeTab === 'backup' ? (
        <section
          className="integration-content integration-content--backup"
          id="integration-panel-backup"
          role="tabpanel"
          aria-labelledby="integration-tab-backup"
        >
          <div className="integration-section-heading">
            <div>
              <span>Keep memory portable</span>
              <div className="tutorial-title-row"><h2>备份与恢复</h2><TutorialLink url={TUTORIAL_URLS.backup} /></div>
            </div>
          </div>
          <MemoryBackupSection />
        </section>
      ) : (
        <section
          className="integration-content"
          id={`integration-panel-${activeTab}`}
          role="tabpanel"
          aria-labelledby={`integration-tab-${activeTab}`}
        >
          <div className="integration-section-heading">
            <div>
              <span>{activeTab === 'input' ? 'Bring data in, locally' : 'Put memory to work'}</span>
              <h2>{activeTab === 'input' ? '导入记忆Skill' : '输出记忆到外部工具'}</h2>
            </div>
            <button className="integration-upload-button" type="button" onClick={() => openSkillPackagePicker(activeTab)} disabled={uploadingDirection !== null}>
              {uploadingDirection === activeTab ? <Loader2 className="spin" size={17} /> : <PackagePlus size={17} />}
              {uploadingDirection === activeTab ? '正在保存…' : '上传 Skill 文件夹'}
            </button>
          </div>

          {notice && <div className="integration-notice" role="status"><CheckCircle2 size={16} />{notice}</div>}
          {(catalogError || customSkillsError) && (
            <div className="integration-error" role="alert">
              <span>{catalogError || customSkillsError}</span>
              <button type="button" onClick={() => { void loadCatalog(); void loadCustomSkills() }}>重试</button>
            </div>
          )}

          {catalogLoading ? (
            <div className="integration-catalog-loading" role="status"><Loader2 className="spin" size={18} /> 正在读取本机执行器…</div>
          ) : (
            <div className="integration-skill-grid">
              {visibleSkills.map(skill => (
                <ExecutableSkillCard
                  key={skill.id}
                  skill={skill}
                  selected={selectedSkillId === skill.id}
                  onRun={() => void openWorkbench(skill, 'run')}
                  onFiles={() => void openWorkbench(skill, 'files')}
                  onDownload={() => void downloadIntegrationSkillBundle(apiBaseUrl, skill.id)
                    .then(savedPath => notifyDownloadSaved(`${skill.title} Skill 包已保存`, savedPath))
                    .catch(error => {
                      setCatalogError(toUserFacingError(error, '下载 Skill 包失败'))
                    })}
                  onOpenManual={() => {
                    const manualUrl = SKILL_MANUAL_URLS[skill.id]
                    if (!manualUrl) return
                    void openExternalUrl(manualUrl).catch(error => setCatalogError(toUserFacingError(error, '打开用户手册失败')))
                  }}
                />
              ))}
            </div>
          )}

          {selectedSkill && (
            <section className="integration-workbench" ref={workbenchRef} aria-label={`${selectedSkill.title} 本地执行工作台`}>
              <header className="integration-workbench__header">
                <div>
                  <span><TerminalSquare size={15} /> 本地执行工作台</span>
                  <h3>{selectedSkill.title}</h3>
                  <p>{selectedSkill.executor} · v{selectedSkill.version} · {selectedSkill.fileCount} 个可查看文件</p>
                </div>
                <div className="integration-workbench__tabs" role="tablist" aria-label="Skill 工作台视图">
                  {!SKILL_EXPORT_ONLY_IDS.has(selectedSkill.id) && (
                    <button type="button" role="tab" aria-selected={workbenchView === 'run'} className={workbenchView === 'run' ? 'is-active' : ''} onClick={() => setWorkbenchView('run')}><Play size={14} />执行</button>
                  )}
                  <button type="button" role="tab" aria-selected={workbenchView === 'files'} className={workbenchView === 'files' ? 'is-active' : ''} onClick={() => setWorkbenchView('files')}><FileCode2 size={14} />文件与源码</button>
                  <button type="button" onClick={() => { setSelectedSkillId(null); setSkillDetail(null) }} aria-label="关闭 Skill 工作台"><X size={16} /></button>
                </div>
              </header>

              {workbenchError && <div className="integration-error" role="alert"><AlertCircle size={15} />{workbenchError}</div>}
              {detailLoading ? (
                <div className="integration-workbench__loading"><Loader2 className="spin" size={18} /> 正在加载 Skill 描述、源码和执行历史…</div>
              ) : workbenchView === 'files' || SKILL_EXPORT_ONLY_IDS.has(selectedSkill.id) ? (
                <SkillFilesView
                  apiBaseUrl={apiBaseUrl}
                  skill={skillDetail}
                  selectedPath={viewingFilePath}
                  selectedFile={selectedFile}
                  onSelect={setViewingFilePath}
                  onError={setWorkbenchError}
                  onDownloaded={notifyDownloadSaved}
                />
              ) : (
                <div className="integration-workbench__grid">
                  <div className="integration-run-config">
                    <div className="integration-panel-label"><span>01</span><div><strong>准备输入</strong></div></div>
                    {selectedSkill.inputKind === 'folder' || selectedSkill.inputKind === 'files' ? (
                      <div className="integration-file-drop">
                        <FolderInput size={23} />
                        <strong>{inputFiles.length ? `已读取 ${inputFiles.length} 个文件` : selectedSkill.inputKind === 'folder' ? '选择本地仓库文件夹' : '选择本地导出文件'}</strong>
                        <p>{inputFiles.length
                          ? `${formatBytes(inputFiles.reduce((sum, file) => sum + file.sizeBytes, 0))} · 正文尚未写入记忆库`
                          : `支持 ${selectedSkill.accept || 'Skill 声明的文件类型'}，内容仅发送到 127.0.0.1`}</p>
                        <button type="button" onClick={() => dataInputRef.current?.click()} disabled={encodingFiles || runPending}>
                          {encodingFiles ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}
                          {encodingFiles ? '正在本机读取…' : inputFiles.length ? '重新选择' : '选择文件'}
                        </button>
                      </div>
                    ) : selectedSkill.inputKind === 'query' ? (
                      <div className="integration-query-config">
                        <label htmlFor="integration-query">任务或文档线索</label>
                        <div><Search size={16} /><input id="integration-query" value={query} onChange={event => setQuery(event.target.value)} placeholder="例如：MemoryBread Obsidian 导入决策" /></div>
                        <label htmlFor="integration-limit">最多带入 {resultLimit} 条本机记忆</label>
                        <input id="integration-limit" type="range" min="3" max="20" value={resultLimit} onChange={event => setResultLimit(Number(event.target.value))} />
                      </div>
                    ) : selectedSkill.inputKind === 'memory_pick' ? (
                      <MemoryExportPicker
                        apiBaseUrl={apiBaseUrl}
                        vaultPath={vaultPath}
                        onVaultPathChange={setVaultPath}
                        subfolder={vaultSubfolder}
                        onSubfolderChange={setVaultSubfolder}
                        selectedIds={selectedMemoryIds}
                        onSelectedIdsChange={setSelectedMemoryIds}
                      />
                    ) : selectedSkill.executor === 'workbuddy_skill_export' ? (
                      <div className="integration-install-note">
                        <PackageCheck size={23} />
                        <strong>生成 WorkBuddy 可上传的 Skill ZIP</strong>
                        <p>预检会说明文件与隐私边界；正式生成不会自动上传，请在 WorkBuddy 中检查后主动安装。</p>
                      </div>
                    ) : (
                      <div className="integration-install-note">
                        <PackageCheck size={23} />
                        <strong>安装完整 memory-retrieval Skill</strong>
                        <p>预检会确认目标目录；正式安装前，已有版本会移动到带时间戳的备份。</p>
                      </div>
                    )}

                    <div className="integration-run-actions">
                      {selectedSkill.supportsPreview && (
                        <button className="integration-secondary-action" type="button" onClick={() => void handleRun('preview')} disabled={!runInputReady || runPending || encodingFiles}>
                          <Eye size={15} />预检
                        </button>
                      )}
                      <button className="integration-primary-action" type="button" onClick={() => void handleRun('execute')} disabled={!runInputReady || runPending || encodingFiles}>
                        {runPending ? <Loader2 className="spin" size={15} /> : <Play size={15} />}
                        {selectedSkill.direction === 'input'
                          ? '执行导入'
                          : selectedSkill.executor === 'install_agent_skill'
                            ? '安装 Skill'
                            : selectedSkill.executor === 'vault_export'
                              ? '导出到 Vault'
                              : selectedSkill.executor === 'workbuddy_skill_export'
                                ? '生成 Skill 包'
                                : '生成上下文包'}
                      </button>
                    </div>

                    <div className="integration-run-history">
                      <header><strong>执行历史</strong><button type="button" onClick={() => void listIntegrationSkillRuns(apiBaseUrl, selectedSkill.id, 20).then(setRuns).catch(error => setWorkbenchError(toUserFacingError(error, '刷新执行历史失败')))}><RefreshCw size={13} />刷新</button></header>
                      {runs.length ? runs.filter(run => run.skillId === selectedSkill.id).map(run => (
                        <button key={run.id} type="button" className={activeRun?.id === run.id ? 'is-active' : ''} onClick={() => setActiveRun(run)}>
                          <RunStatusDot status={run.status} />
                          <span><strong>{RUN_STATUS_COPY[run.status] || run.status}</strong><small>{formatRunTime(run.createdAtMs)} · {run.mode === 'preview' ? '预检' : '执行'}</small></span>
                          <ChevronRight size={14} />
                        </button>
                      )) : <p>还没有执行记录。</p>}
                    </div>
                  </div>

                  <RunInspector
                    run={activeRun}
                    onCopyError={setWorkbenchError}
                    onDownloaded={notifyDownloadSaved}
                    onOpenMemory={(memoryId) => {
                      useAppStore.setState({
                        windowMode: 'knowledge',
                        repositoryTab: 'memory',
                        repositoryMemoryFocusId: String(memoryId),
                        selectedMemoryId: String(memoryId),
                        bakeMemoryOffset: 0,
                      })
                    }}
                  />
                </div>
              )}
            </section>
          )}

          <section className="integration-custom-section" aria-label={`自定义${activeTab === 'input' ? '输入' : '输出'} Skill`}>
            <div className="integration-custom-section__header">
              <div><h3>自定义 Skill 包</h3></div>
              <div>
                <button type="button" onClick={() => openSkillPackagePicker(activeTab)} disabled={customSkillsLoading || uploadingDirection !== null}><PackagePlus size={14} />文件夹</button>
                <button type="button" onClick={() => openSkillZipPicker(activeTab)} disabled={customSkillsLoading || uploadingDirection !== null}><Upload size={14} />ZIP</button>
              </div>
            </div>
            {customSkillsLoading && visibleCustomSkills.length === 0 ? (
              <div className="integration-custom-empty"><Loader2 className="spin" size={16} /> 正在读取…</div>
            ) : visibleCustomSkills.length === 0 ? (
              <div className="integration-custom-empty">还没有自定义 Skill。</div>
            ) : (
              <div className="integration-custom-list">
                {visibleCustomSkills.map(skill => (
                  <article key={skill.id}>
                    <span><PackageCheck size={17} /></span>
                    <div><strong>{skill.title}</strong><p>{skill.summary}</p></div>
                    <em>{skill.packageFiles?.length || 0} 个文件</em>
                    <div className="integration-custom-list__actions">
                      <button type="button" onClick={() => setCustomSkillDetail(skill)}><Eye size={13} />查看</button>
                      <button type="button" onClick={() => void downloadCustomSkill(skill)}><Download size={13} />下载</button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </section>
      )}

      {customSkillDetail && (
        <CustomSkillDialog skill={customSkillDetail} onClose={() => setCustomSkillDetail(null)} onDownload={() => void downloadCustomSkill(customSkillDetail)} />
      )}

      {downloadToast && (
        <DownloadToastView
          key={downloadToast.id}
          toast={downloadToast}
          onDismiss={() => setDownloadToast(null)}
          onActionError={message => showDownloadToast(message, null)}
        />
      )}
    </div>
  )
}

const MEMORY_OPTIONS_PAGE_SIZE = 60
const MEMORY_EXPORT_MAX = 200

export const MemoryExportPicker: React.FC<{
  apiBaseUrl: string
  vaultPath: string
  onVaultPathChange: (path: string) => void
  subfolder: string
  onSubfolderChange: (value: string) => void
  selectedIds: number[]
  onSelectedIdsChange: (ids: number[]) => void
}> = ({
  apiBaseUrl,
  vaultPath,
  onVaultPathChange,
  subfolder,
  onSubfolderChange,
  selectedIds,
  onSelectedIdsChange,
}) => {
  const [memoryQuery, setMemoryQuery] = useState('')
  const [options, setOptions] = useState<IntegrationMemoryOption[]>([])
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [hasMoreOptions, setHasMoreOptions] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [marquee, setMarquee] = useState<{ left: number; top: number; width: number; height: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ startX: number; startY: number; initial: number[] } | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const lastClickedIndexRef = useRef<number | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setOptionsLoading(true)
      listIntegrationMemoryOptions(apiBaseUrl, memoryQuery, MEMORY_OPTIONS_PAGE_SIZE)
        .then(items => {
          setOptions(items)
          setHasMoreOptions(items.length >= MEMORY_OPTIONS_PAGE_SIZE)
        })
        .catch(() => {
          setOptions([])
          setHasMoreOptions(false)
        })
        .finally(() => setOptionsLoading(false))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [apiBaseUrl, memoryQuery])

  const loadMoreOptions = useCallback(() => {
    if (loadingMore || optionsLoading || !hasMoreOptions) return
    setLoadingMore(true)
    listIntegrationMemoryOptions(apiBaseUrl, memoryQuery, MEMORY_OPTIONS_PAGE_SIZE, options.length)
      .then(items => {
        setOptions(current => {
          const known = new Set(current.map(item => item.id))
          return [...current, ...items.filter(item => !known.has(item.id))]
        })
        setHasMoreOptions(items.length >= MEMORY_OPTIONS_PAGE_SIZE)
      })
      .catch(() => setHasMoreOptions(false))
      .finally(() => setLoadingMore(false))
  }, [apiBaseUrl, memoryQuery, options.length, hasMoreOptions, loadingMore, optionsLoading])

  const handleListScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const container = event.currentTarget
    if (container.scrollTop + container.clientHeight >= container.scrollHeight - 120) {
      loadMoreOptions()
    }
  }

  useEffect(() => {
    if (!dragging) return undefined
    const handleMove = (event: MouseEvent) => {
      const container = containerRef.current
      const drag = dragRef.current
      if (!container || !drag) return
      const rect = container.getBoundingClientRect()
      const x = event.clientX - rect.left + container.scrollLeft
      const y = event.clientY - rect.top + container.scrollTop
      const left = Math.min(drag.startX, x)
      const top = Math.min(drag.startY, y)
      const width = Math.abs(x - drag.startX)
      const height = Math.abs(y - drag.startY)
      setMarquee({ left, top, width, height })
      if (width < 4 && height < 4) return
      const box = { left, top, right: left + width, bottom: top + height }
      const hit: number[] = []
      container.querySelectorAll<HTMLElement>('[data-memory-card]').forEach(card => {
        const cardRect = card.getBoundingClientRect()
        const cardLeft = cardRect.left - rect.left + container.scrollLeft
        const cardTop = cardRect.top - rect.top + container.scrollTop
        const intersects = cardLeft < box.right
          && cardLeft + cardRect.width > box.left
          && cardTop < box.bottom
          && cardTop + cardRect.height > box.top
        if (!intersects) return
        const id = Number(card.dataset.memoryCard)
        if (!Number.isNaN(id)) hit.push(id)
      })
      onSelectedIdsChange(Array.from(new Set([...drag.initial, ...hit])))
    }
    const handleUp = () => {
      setDragging(false)
      setMarquee(null)
      dragRef.current = null
    }
    window.addEventListener('mousemove', handleMove)
    window.addEventListener('mouseup', handleUp)
    return () => {
      window.removeEventListener('mousemove', handleMove)
      window.removeEventListener('mouseup', handleUp)
    }
  }, [dragging, onSelectedIdsChange])

  const handleContainerMouseDown = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement
    if (target.closest('[data-memory-card]')) return
    const container = containerRef.current
    if (!container) return
    const rect = container.getBoundingClientRect()
    dragRef.current = {
      startX: event.clientX - rect.left + container.scrollLeft,
      startY: event.clientY - rect.top + container.scrollTop,
      initial: event.shiftKey || event.metaKey || event.ctrlKey ? selectedIds : [],
    }
    setDragging(true)
    setMarquee({ left: dragRef.current.startX, top: dragRef.current.startY, width: 0, height: 0 })
    event.preventDefault()
  }

  const handleCardClick = (event: React.MouseEvent, option: IntegrationMemoryOption, index: number) => {
    if (event.shiftKey && lastClickedIndexRef.current !== null) {
      const from = Math.min(lastClickedIndexRef.current, index)
      const to = Math.max(lastClickedIndexRef.current, index)
      const rangeIds = options.slice(from, to + 1).map(item => item.id)
      onSelectedIdsChange(Array.from(new Set([...selectedIds, ...rangeIds])))
    } else if (selectedIds.includes(option.id)) {
      onSelectedIdsChange(selectedIds.filter(id => id !== option.id))
    } else {
      onSelectedIdsChange([...selectedIds, option.id])
    }
    lastClickedIndexRef.current = index
  }

  const handleListKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a') {
      event.preventDefault()
      onSelectedIdsChange(options.map(option => option.id))
    }
  }

  const handlePickVault = async () => {
    const picked = await pickLocalDirectory()
    if (picked) onVaultPathChange(picked)
  }

  return (
    <div className="integration-memory-picker">
      <div className="integration-vault-config">
        <label htmlFor="integration-vault-path">Obsidian Vault 文件夹</label>
        <div className="integration-vault-row">
          <button type="button" onClick={() => void handlePickVault()}>
            <FolderOpen size={15} />选择文件夹
          </button>
          <input
            id="integration-vault-path"
            value={vaultPath}
            onChange={event => onVaultPathChange(event.target.value)}
            placeholder="选择或粘贴 Vault 绝对路径，笔记写入其子目录"
          />
        </div>
        <label htmlFor="integration-vault-subfolder">Vault 内子目录</label>
        <input
          id="integration-vault-subfolder"
          value={subfolder}
          onChange={event => onSubfolderChange(event.target.value)}
          placeholder="MemoryBread"
        />
      </div>

      <div className="integration-memory-search">
        <Search size={15} />
        <input
          value={memoryQuery}
          onChange={event => setMemoryQuery(event.target.value)}
          placeholder="搜索记忆标题或摘要，缩小圈选范围"
          aria-label="搜索待导出记忆"
        />
      </div>

      <div className="integration-memory-toolbar">
        <span>
          已选 {selectedIds.length} 条{selectedIds.length > MEMORY_EXPORT_MAX ? ` · 超过单次上限 ${MEMORY_EXPORT_MAX} 条` : ''} · 已加载 {options.length} 条{hasMoreOptions ? ' · 滚动加载更多' : ''} · 空白处拖拽圈选 · Shift 连选 · ⌘/Ctrl+A 全选
        </span>
        <div>
          <button type="button" onClick={() => onSelectedIdsChange(options.map(option => option.id))} disabled={!options.length}>全选</button>
          <button type="button" onClick={() => onSelectedIdsChange([])} disabled={!selectedIds.length}>清空</button>
        </div>
      </div>

      <div
        className={`integration-memory-list${dragging ? ' is-dragging' : ''}`}
        ref={containerRef}
        tabIndex={0}
        role="listbox"
        aria-multiselectable="true"
        aria-label="待导出记忆列表"
        onMouseDown={handleContainerMouseDown}
        onKeyDown={handleListKeyDown}
        onScroll={handleListScroll}
      >
        {optionsLoading && !options.length ? (
          <p className="integration-memory-empty"><Loader2 className="spin" size={15} /> 正在读取本机记忆…</p>
        ) : !options.length ? (
          <p className="integration-memory-empty">没有匹配的本机记忆，换一个关键词试试。</p>
        ) : options.map((option, index) => {
          const selected = selectedIds.includes(option.id)
          return (
            <div
              key={option.id}
              data-memory-card={option.id}
              role="option"
              aria-selected={selected}
              className={`integration-memory-card${selected ? ' is-selected' : ''}`}
              onClick={event => handleCardClick(event, option, index)}
            >
              <span className="integration-memory-card__check">{selected ? <Check size={12} /> : null}</span>
              <div>
                <strong>{option.title}</strong>
                <small>
                  {option.category}
                  {option.observedAt ? ` · ${formatRunTime(option.observedAt)}` : ''}
                </small>
              </div>
            </div>
          )
        })}
        {loadingMore && (
          <p className="integration-memory-empty"><Loader2 className="spin" size={15} /> 正在加载更多记忆…</p>
        )}
        {marquee && (
          <div
            className="integration-marquee"
            aria-hidden
            style={{ left: marquee.left, top: marquee.top, width: marquee.width, height: marquee.height }}
          />
        )}
      </div>
    </div>
  )
}

const ExecutableSkillCard: React.FC<{
  skill: IntegrationSkillCatalogItem
  selected: boolean
  onRun: () => void
  onFiles: () => void
  onDownload: () => void
  onOpenManual: () => void
}> = ({ skill, selected, onRun, onFiles, onDownload, onOpenManual }) => {
  const Icon = SKILL_ICONS[skill.id] || PackageCheck
  const exportOnly = SKILL_EXPORT_ONLY_IDS.has(skill.id)
  const manualUrl = SKILL_MANUAL_URLS[skill.id]
  return (
    <article className={`integration-skill-card${selected ? ' is-selected' : ''}`}>
      <div className="integration-skill-card__topline">
        <span className="integration-skill-card__icon"><Icon size={21} /></span>
        {exportOnly && <span className="integration-skill-card__badge integration-skill-card__badge--skill">Skill</span>}
      </div>
      <span className="integration-skill-card__eyebrow">{skill.eyebrow}</span>
      <h3>{skill.title}</h3>
      <p>{skill.description}</p>
      <div className="integration-skill-card__footer"><span>{skill.capability}</span></div>
      <div className="integration-skill-card__actions">
        {!exportOnly && <button className="is-primary" type="button" onClick={onRun}><Play size={14} />执行</button>}
        <button type="button" onClick={onFiles}><Eye size={14} />文件</button>
        {manualUrl && <button type="button" onClick={onOpenManual} aria-label={`打开 ${skill.title} 集成教程`}><BookOpen size={14} />教程</button>}
        <button type="button" onClick={onDownload} aria-label={`下载 ${skill.title} Skill 包`}><Download size={14} /></button>
      </div>
    </article>
  )
}

const SkillFilesView: React.FC<{
  apiBaseUrl: string
  skill: IntegrationSkillDetail | null
  selectedPath: string
  selectedFile: IntegrationSkillDetail['files'][number] | null
  onSelect: (path: string) => void
  onError: (message: string) => void
  onDownloaded: (title: string, savedPath: string | null) => void
}> = ({ apiBaseUrl, skill, selectedPath, selectedFile, onSelect, onError, onDownloaded }) => {
  if (!skill) return <div className="integration-workbench__loading">没有可查看的 Skill 文件。</div>
  return (
    <div className="integration-files-view">
      <aside>
        <header><strong>Skill 文件</strong><small>{skill.files.length} 个</small></header>
        {skill.files.map(file => (
          <button key={file.path} type="button" className={selectedPath === file.path ? 'is-active' : ''} onClick={() => onSelect(file.path)}>
            <FileCode2 size={14} /><span>{file.path}<small>{formatBytes(file.sizeBytes)}</small></span><ChevronRight size={13} />
          </button>
        ))}
        <button className="integration-files-view__download-all" type="button" onClick={() => void downloadIntegrationSkillBundle(apiBaseUrl, skill.id).then(savedPath => onDownloaded(`${skill.title} Skill 包已保存`, savedPath)).catch(error => onError(toUserFacingError(error, '下载 Skill 包失败')))}><Download size={14} />下载完整 Skill 包</button>
      </aside>
      <section>
        <header>
          <div><strong>{selectedFile?.path || '选择文件'}</strong><small>{selectedFile?.mediaType}</small></div>
          {selectedFile && <button type="button" onClick={() => void downloadIntegrationSkillFile(apiBaseUrl, skill.id, selectedFile.path).then(savedPath => onDownloaded(`${selectedFile.path} 已保存`, savedPath)).catch(error => onError(toUserFacingError(error, '下载文件失败')))}><Download size={14} />下载文件</button>}
        </header>
        <pre><code>{selectedFile?.content || '从左侧选择 SKILL.md、integration.json 或真实执行源码。'}</code></pre>
      </section>
    </div>
  )
}

const RunInspector: React.FC<{
  run: IntegrationSkillRun | null
  onCopyError: (message: string) => void
  onDownloaded: (title: string, savedPath: string | null) => void
  onOpenMemory: (memoryId: string | number) => void
}> = ({ run, onCopyError, onDownloaded, onOpenMemory }) => {
  if (!run) {
    return (
      <div className="integration-run-inspector integration-run-inspector--empty">
        <TerminalSquare size={28} /><strong>等待一次本地执行</strong><p>启动后，这里会显示阶段、结果和逐条日志。</p>
      </div>
    )
  }
  const artifact = run.result?.artifact
  return (
    <div className="integration-run-inspector">
      <div className="integration-panel-label"><span>02</span><div><strong>执行状态</strong><small>{run.id}</small></div></div>
      <RunRail status={run.status} />
      {run.result && (
        <div className="integration-run-result">
          <header><CheckCircle2 size={16} /><strong>{run.mode === 'preview' ? '预检结果' : '执行结果'}</strong></header>
          {run.result.kind === 'import' && (
            <div className="integration-result-metrics">
              <Metric label="解析" value={run.result.parsed} />
              <Metric label="创建" value={run.result.created} />
              <Metric label="更新" value={run.result.updated} />
              <Metric label="未变化" value={run.result.unchanged} />
              <Metric label="跳过" value={run.result.skipped} />
            </div>
          )}
          {run.result.kind === 'vault_preview' && (
            <div className="integration-result-metrics">
              <Metric label="计划笔记" value={run.result.noteCount} />
              <Metric label="将覆盖" value={run.result.overwriteCount} />
            </div>
          )}
          {run.result.kind === 'vault_export' && (
            <div className="integration-result-metrics">
              <Metric label="新增" value={run.result.created} />
              <Metric label="更新" value={run.result.updated} />
              <Metric label="未变化" value={run.result.unchanged} />
            </div>
          )}
          {(run.result.kind === 'vault_preview' || run.result.kind === 'vault_export') && (
            <p className="integration-result-target">
              {run.result.target ? `目标目录：${run.result.target}` : '尚未选择 Vault 文件夹，正式导出前请先选择。'}
            </p>
          )}
          {run.result.kind === 'install_preview' && <p>目标：{run.result.target}。{run.result.existingInstallation ? '检测到旧版本，正式安装会先备份。' : '目标目录可以直接安装。'}</p>}
          {run.result.kind === 'install' && <p>已安装到 {run.result.target}，共 {run.result.fileCount} 个文件。调用方式：<code>{run.result.invocation}</code></p>}
          {run.result.kind === 'workbuddy_skill_preview' && <p>将在本机生成包含 {run.result.fileCount} 个文件的 Skill ZIP；随后从 {run.result.target} 主动上传。</p>}
          {!!run.result.records?.length && (
            <div className="integration-result-records" aria-label={run.result.kind === 'import' ? '导入记忆明细' : '记忆来源明细'}>
              {run.result.records.map(record => (
                <button key={`${record.id}-${record.path || record.title}`} type="button" onClick={() => onOpenMemory(record.id)}>
                  <span>
                    <strong>{record.title}</strong>
                    <small>{record.path || `时间线 #${record.id}`}</small>
                  </span>
                  {record.outcome && <em>{record.outcome === 'created' ? '新增' : record.outcome === 'updated' ? '更新' : '未变化'}</em>}
                  <ChevronRight size={14} aria-hidden />
                </button>
              ))}
            </div>
          )}
          {artifact && (
            <div className="integration-artifact">
              <FileArchive size={21} /><div><strong>{artifact.fileName}</strong><small>{run.result.matchCount || 0} 条本机上下文</small></div>
              {artifact.mediaType.startsWith('text/') && (
                <button type="button" onClick={() => void copyIntegrationArtifact(artifact).catch(error => onCopyError(toUserFacingError(error, '复制上下文包失败')))}><Copy size={14} />复制</button>
              )}
              <button type="button" onClick={() => void downloadIntegrationArtifact(artifact).then(savedPath => onDownloaded(`${artifact.fileName} 已保存`, savedPath)).catch(error => onCopyError(toUserFacingError(error, '下载上下文包失败')))}><Download size={14} />下载</button>
            </div>
          )}
        </div>
      )}
      {run.status === 'failed' && <div className="integration-run-failure"><AlertCircle size={16} /><div><strong>{run.errorCode || 'EXECUTION_FAILED'}</strong><p>{run.errorMessage || '本地执行失败'}</p></div></div>}
      <div className="integration-run-log">
        <header><ScrollText size={15} /><strong>执行日志</strong><small>{run.logs.length} 条</small></header>
        <ol>{run.logs.map((entry, index) => (
          <li key={`${entry.ts}-${index}`} className={`is-${entry.level}`}><time>{new Date(entry.ts).toLocaleTimeString()}</time><span>{entry.message}</span></li>
        ))}</ol>
      </div>
    </div>
  )
}

const RunRail: React.FC<{ status: string }> = ({ status }) => {
  const failed = status === 'failed'
  const stages = [
    { id: 'queued', label: '进入队列' },
    { id: 'running', label: '记忆导入' },
    { id: failed ? 'failed' : 'succeeded', label: failed ? '失败' : '完成' },
  ]
  const activeIndex = status === 'queued' ? 0 : status === 'running' ? 1 : 2
  return (
    <div className={`integration-run-rail${failed ? ' is-failed' : ''}`}>
      {stages.map((stage, index) => <React.Fragment key={stage.id}>
        {index > 0 && <span className={index <= activeIndex ? 'is-done' : ''} />}
        <div className={index < activeIndex ? 'is-done' : index === activeIndex ? 'is-active' : ''}>
          <i>{index < activeIndex || (index === 2 && !failed && activeIndex === 2) ? <Check size={13} /> : index + 1}</i>
          <small>{stage.label}</small>
        </div>
      </React.Fragment>)}
    </div>
  )
}

const RunStatusDot: React.FC<{ status: string }> = ({ status }) => <i className={`integration-status-dot is-${status}`} aria-hidden />
const Metric: React.FC<{ label: string; value?: number }> = ({ label, value }) => <div><strong>{value || 0}</strong><small>{label}</small></div>

const DownloadToastView: React.FC<{
  toast: { title: string; path: string | null }
  onDismiss: () => void
  onActionError: (message: string) => void
}> = ({ toast, onDismiss, onActionError }) => (
  <div className="integration-download-toast" role="status" aria-live="polite">
    <CheckCircle2 size={17} aria-hidden />
    <div className="integration-download-toast__copy">
      <strong>{toast.title}</strong>
      {toast.path && <small>{toast.path}</small>}
    </div>
    <div className="integration-download-toast__actions">
      {toast.path && (
        <>
          <button type="button" onClick={() => void openDownloadedFile(toast.path as string).catch(error => onActionError(toUserFacingError(error, '无法打开下载的文件')))}><ExternalLink size={13} />打开文件</button>
          <button type="button" onClick={() => void revealDownloadedFile(toast.path as string).catch(error => onActionError(toUserFacingError(error, '无法打开所在文件夹')))}><FolderOpen size={13} />打开文件夹</button>
        </>
      )}
      <button type="button" aria-label="关闭下载提示" onClick={onDismiss}><X size={14} /></button>
    </div>
  </div>
)

const CustomSkillDialog: React.FC<{ skill: LocalCreationSkill; onClose: () => void; onDownload: () => void }> = ({ skill, onClose, onDownload }) => {
  const [selectedPath, setSelectedPath] = useState(skill.packageFiles?.[0]?.path || '')
  const file = skill.packageFiles?.find(item => item.path === selectedPath)
  return (
    <div className="integration-dialog-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <section className="integration-dialog" role="dialog" aria-modal="true" aria-label={`${skill.title} Skill 文件`}>
        <header><div><span>自定义 Skill 包</span><h3>{skill.title}</h3></div><button type="button" onClick={onClose} aria-label="关闭"><X size={17} /></button></header>
        <div className="integration-dialog__body">
          <aside>{(skill.packageFiles || []).map(item => <button key={item.path} type="button" className={selectedPath === item.path ? 'is-active' : ''} onClick={() => setSelectedPath(item.path)}><FileCode2 size={13} />{item.path}</button>)}</aside>
          <pre><code>{file ? skillFileText(file) || '二进制文件不可预览' : '没有包文件'}</code></pre>
        </div>
        <footer><button type="button" onClick={onDownload}><Download size={14} />下载标准 ZIP</button><button type="button" onClick={onClose}>关闭</button></footer>
      </section>
    </div>
  )
}

function formatBytes(bytes = 0) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatRunTime(timestamp: number) {
  return new Date(timestamp).toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export default IntegrationPanel
