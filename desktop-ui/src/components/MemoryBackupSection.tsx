import React, { useCallback, useEffect, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { CheckCircle2, ChevronRight, Cloud, FolderOpen, HardDriveDownload, History, LoaderCircle, LockKeyhole, LogIn, ShieldCheck, XCircle } from 'lucide-react'
import {
  useBackupMemoryPackageToCloud,
  useExportMemoryPackage,
  useImportMemoryPackage,
  useRestoreMemoryPackageFromCloud,
} from '../hooks/useApi'
import { CREATION_MODEL_KEY, useAppStore } from '../store/useAppStore'
import type { CloudSnapshot, MemoryPackageImportReport } from '../types'
import { fetchCloudSnapshots } from '../utils/authApi'
import { CREATION_TOOLS_STORAGE_KEY } from '../utils/creationTools'
import {
  FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY,
  FLOATING_ASSIST_AUTO_TASK_KEY,
  FLOATING_ASSIST_ENABLED_KEY,
} from '../utils/floatingAssistAutoTask'
import { INTERACTION_SETTINGS_KEY } from '../utils/interactionSettings'
import { registerCurrentDevice } from '../utils/softwareUpdate'
import { toUserFacingError } from '../utils/userFacingError'
import { BakeButton, BakeCard, BakePill, BakeSectionHeader } from './bake/BakeShared'

const tableLabels: Record<string, string> = {
  breadcrumb_definitions: '面包屑定义',
  breadcrumb_rules: '面包屑规则',
  breadcrumb_inventory: '面包屑数量',
  breadcrumb_awards: '面包屑记录',
  breadcrumb_equipment: '面包屑佩戴',
  capture_refs: '占位引用',
  timelines: '时间线',
  bake_knowledge: '知识',
  bake_documents: '文档',
  bake_document_sections: '文档章节',
  bake_sops: '操作',
  creation_skills: '本地 Skill',
  data_sources: '数据记录',
  data_snapshots: '数据快照',
  data_source_links: '数据来源关系',
}

const clientStateBackupKeys = [
  CREATION_MODEL_KEY,
  CREATION_TOOLS_STORAGE_KEY,
  FLOATING_ASSIST_ENABLED_KEY,
  FLOATING_ASSIST_AUTO_TASK_KEY,
  FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY,
  INTERACTION_SETTINGS_KEY,
]

const collectClientStateForBackup = () => Object.fromEntries(
  clientStateBackupKeys
    .map(key => [key, window.localStorage.getItem(key)] as const)
    .filter((entry): entry is readonly [string, string] => entry[1] !== null),
)

const restoreClientStateFromBackup = (state?: Record<string, string>) => {
  if (!state) return
  clientStateBackupKeys.forEach(key => {
    const value = state[key]
    if (typeof value === 'string' && window.localStorage.getItem(key) === null) {
      window.localStorage.setItem(key, value)
    }
  })
}

const importDetailTabs: Record<string, { windowMode: 'knowledge' } | { windowMode: 'bake'; bakeTab: 'templates' | 'knowledge' | 'sop' | 'data' }> = {
  timelines: { windowMode: 'knowledge' },
  bake_knowledge: { windowMode: 'bake', bakeTab: 'knowledge' },
  bake_documents: { windowMode: 'bake', bakeTab: 'templates' },
  bake_document_sections: { windowMode: 'bake', bakeTab: 'templates' },
  bake_sops: { windowMode: 'bake', bakeTab: 'sop' },
  data_sources: { windowMode: 'bake', bakeTab: 'data' },
  data_snapshots: { windowMode: 'bake', bakeTab: 'data' },
  data_source_links: { windowMode: 'bake', bakeTab: 'data' },
}

const formatBytes = (bytes: number) => {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const BACKUP_ACTIVITY_STORAGE_KEY = 'memory-bread_backup_activity_v1'
const MAX_BACKUP_ACTIVITIES = 100

type BackupActivityKind = 'local-export' | 'local-import' | 'cloud-backup' | 'cloud-restore'
type BackupActivityStatus = 'running' | 'success' | 'failed'

interface BackupActivityDetail {
  label: string
  value: string
}

interface BackupActivity {
  id: string
  kind: BackupActivityKind
  title: string
  status: BackupActivityStatus
  createdAt: number
  completedAt?: number
  summary: string
  details: BackupActivityDetail[]
}

const readBackupActivities = (): BackupActivity[] => {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(BACKUP_ACTIVITY_STORAGE_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter(item => item
        && typeof item.id === 'string'
        && typeof item.title === 'string'
        && typeof item.createdAt === 'number'
        && ['running', 'success', 'failed'].includes(item.status)
        && Array.isArray(item.details))
      .slice(0, MAX_BACKUP_ACTIVITIES) as BackupActivity[]
  } catch {
    return []
  }
}

const parentPath = (path: string) => {
  const normalized = path.replace(/[\\/]+$/, '')
  const separator = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'))
  return separator > 0 ? normalized.slice(0, separator) : normalized
}

const importCounts = (report: MemoryPackageImportReport) => {
  const rows = [report.capture_refs, ...report.tables]
  return {
    incoming: rows.reduce((sum, item) => sum + (item.incoming || 0), 0),
    deduplicated: rows.reduce((sum, item) => sum + (item.skipped || 0), 0),
    imported: rows.reduce((sum, item) => sum + (item.inserted || 0) + (item.updated || 0), 0),
  }
}

const importActivityDetails = (report: MemoryPackageImportReport): BackupActivityDetail[] => {
  const counts = importCounts(report)
  const details: BackupActivityDetail[] = [
    { label: '输入数据行数', value: String(counts.incoming) },
    { label: '去重行数', value: String(counts.deduplicated) },
    { label: '实际导入行数', value: String(counts.imported) },
  ]
  if (report.target_directory) details.push({ label: '恢复目录', value: report.target_directory })
  if (report.local_files) {
    details.push(
      { label: '输入本地文件', value: String(report.local_files.incoming) },
      { label: '写入本地文件', value: String(report.local_files.written) },
      { label: '同名文件冲突', value: String(report.local_files.conflicts || 0) },
    )
  }
  importReportRows(report).forEach(row => {
    details.push({
      label: row.label,
      value: `输入 ${row.incoming}，去重 ${row.skipped}，实际导入 ${row.inserted + row.updated}`,
    })
  })
  return details
}

const activityStatusText: Record<BackupActivityStatus, string> = {
  running: '进行中',
  success: '已完成',
  failed: '失败',
}

const BackupActivityLedger: React.FC<{ activities: BackupActivity[] }> = ({ activities }) => (
  <BakeCard className="bake-backup-activity" aria-label="备份记录">
    <BakeSectionHeader title="备份记录" />
    {activities.length === 0 ? (
      <div className="bake-backup-activity__empty">
        <History size={19} aria-hidden />
        <div>
          <strong>还没有备份记录</strong>
          <span>导出、导入和云端备份操作会记录在这里。</span>
        </div>
      </div>
    ) : (
      <div className="bake-backup-activity__list" role="list" aria-label="备份操作流水">
        {activities.map(activity => {
          const StatusIcon = activity.status === 'success'
            ? CheckCircle2
            : activity.status === 'failed' ? XCircle : LoaderCircle
          return (
            <details className={`bake-backup-activity__item is-${activity.status}`} key={activity.id} role="listitem">
              <summary>
                <span className="bake-backup-activity__status-icon" aria-hidden><StatusIcon size={17} /></span>
                <span className="bake-backup-activity__main">
                  <span className="bake-backup-activity__title-row">
                    <strong>{activity.title}</strong>
                    <span className="bake-backup-activity__status">{activityStatusText[activity.status]}</span>
                  </span>
                  <span className="bake-backup-activity__summary">{activity.summary}</span>
                </span>
                <time dateTime={new Date(activity.createdAt).toISOString()}>
                  {new Date(activity.createdAt).toLocaleString('zh-CN')}
                </time>
                <span className="bake-backup-activity__disclosure" aria-hidden><ChevronRight size={16} /></span>
              </summary>
              <dl>
                <div><dt>操作类型</dt><dd>{activity.title}</dd></div>
                <div><dt>开始时间</dt><dd>{new Date(activity.createdAt).toLocaleString('zh-CN')}</dd></div>
                {activity.completedAt && (
                  <div><dt>耗时</dt><dd>{Math.max(0, activity.completedAt - activity.createdAt) / 1000} 秒</dd></div>
                )}
                {activity.details.map((detail, index) => (
                  <div key={`${detail.label}-${index}`}><dt>{detail.label}</dt><dd>{detail.value}</dd></div>
                ))}
              </dl>
            </details>
          )
        })}
      </div>
    )}
  </BakeCard>
)

const summarizeImportReport = (report: MemoryPackageImportReport) => {
  const rows = [report.capture_refs, ...report.tables]
  const inserted = rows.reduce((sum, item) => sum + (item.inserted || 0), 0)
  const updated = rows.reduce((sum, item) => sum + (item.updated || 0), 0)
  const skipped = rows.reduce((sum, item) => sum + (item.skipped || 0), 0)
  const fileConflicts = report.local_files?.conflicts || 0
  return `合并完成：新增 ${inserted}，保留本机 ${skipped}${updated ? `，更新 ${updated}` : ''}${fileConflicts ? `，同名文件冲突 ${fileConflicts}` : ''}`
}

const importReportRows = (report: MemoryPackageImportReport | null) => {
  if (!report) return []
  return [report.capture_refs, ...report.tables]
    .filter(item => item.incoming > 0)
    .map(item => ({
      ...item,
      label: tableLabels[item.name] ?? item.name,
    }))
}

type MemoryPackageBusy = 'export' | 'import' | 'cloud-backup' | 'cloud-restore' | null
type CloudBackupAccessState = 'signed-out' | 'available'
type CloudSnapshotsStatus = 'idle' | 'loading' | 'ready' | 'error'
interface MemoryBackupNotice {
  message: string
  exportPath?: string
}

interface MemoryBackupCardProps {
  accessState: CloudBackupAccessState
  busy: MemoryPackageBusy
  cloudSnapshots: CloudSnapshot[]
  cloudSnapshotsStatus: CloudSnapshotsStatus
  cloudSnapshotsError: string | null
  selectedCloudSnapshotId: string
  lastImportReport: MemoryPackageImportReport | null
  importFileInputRef: React.RefObject<HTMLInputElement>
  onExport: () => void
  onImportClick: () => void
  onImportFile: (file?: File | null) => void
  onOpenAccount: () => void
  onCloudSnapshotChange: (value: string) => void
  onRefreshCloudSnapshots: () => void
  onBackupToCloud: () => void
  onRestoreFromCloud: () => void
  onOpenImportDetails?: (tableName: string) => void
}

export const MemoryBackupCard: React.FC<MemoryBackupCardProps> = ({
  accessState,
  busy,
  cloudSnapshots,
  cloudSnapshotsStatus,
  cloudSnapshotsError,
  selectedCloudSnapshotId,
  lastImportReport,
  importFileInputRef,
  onExport,
  onImportClick,
  onImportFile,
  onOpenAccount,
  onCloudSnapshotChange,
  onRefreshCloudSnapshots,
  onBackupToCloud,
  onRestoreFromCloud,
  onOpenImportDetails,
}) => {
  const isBusy = busy !== null
  const cloudListLoading = cloudSnapshotsStatus === 'loading'

  return (
    <BakeCard className="bake-memory-package-card">
      <BakeSectionHeader
        title="记忆备份"
        right={accessState === 'signed-out' ? <BakePill text="未登录" /> : undefined}
      />

      <div className="bake-memory-package-grid">
        <div className="bake-memory-package-group" aria-labelledby="local-backup-title">
          <div className="bake-memory-package-group__header">
            <span className="bake-memory-package-group__icon" aria-hidden>
              <HardDriveDownload size={17} />
            </span>
            <div>
              <div id="local-backup-title" className="bake-memory-package-group__title">本机备份</div>
              <div className="bake-muted">备份全部用户内容、设置与本地 Skill；导入时判重合并，不覆盖本机内容。</div>
            </div>
          </div>
          <div className="bake-actions bake-actions--secondary bake-memory-package-actions">
            <BakeButton compact primary disabled={isBusy} onClick={onExport}>
              {busy === 'export' ? '正在导出...' : '导出记忆包'}
            </BakeButton>
            <BakeButton compact disabled={isBusy} onClick={onImportClick}>
              {busy === 'import' ? '正在导入...' : '导入记忆包'}
            </BakeButton>
            <input
              ref={importFileInputRef}
              type="file"
              accept=".json,.mbmemory,.mbsnapshot"
              aria-label="选择本机记忆包"
              className="bake-memory-package-file"
              onChange={(event) => onImportFile(event.target.files?.[0])}
            />
          </div>
        </div>

        <div className="bake-memory-package-group bake-memory-package-group--cloud" aria-labelledby="cloud-backup-title">
          <div className="bake-memory-package-group__header">
            <span className="bake-memory-package-group__icon" aria-hidden>
              <Cloud size={17} />
            </span>
            <div>
              <div id="cloud-backup-title" className="bake-memory-package-group__title">云端备份</div>
              <div className="bake-muted">把完整内容包加密上传；恢复时与本机内容判重合并，不会整体覆盖。</div>
            </div>
          </div>

          {accessState === 'signed-out' && (
            <div className="bake-memory-package-access-state">
              <span className="bake-memory-package-access-state__icon" aria-hidden>
                <LockKeyhole size={19} />
              </span>
              <div className="bake-memory-package-access-state__body">
                <strong>登录后使用云端备份</strong>
                <span>登录只解锁云端同步，本机导入和导出不受影响。</span>
              </div>
              <BakeButton compact primary onClick={onOpenAccount}>
                <LogIn size={14} aria-hidden />
                登录后使用
              </BakeButton>
            </div>
          )}

          {accessState === 'available' && (
            <div className="bake-memory-package-cloud">
              <div className="bake-memory-package-privacy-note">
                <ShieldCheck size={16} aria-hidden />
                <span>记忆包会先在本机加密再上传，备份与恢复无需设置密钥。</span>
              </div>

              <div className="bake-memory-package-fields">
                <div className="bake-form-field bake-memory-package-select">
                  <span className="bake-filter-label">云端备份</span>
                  {cloudSnapshotsStatus === 'idle' && (
                    <div className="bake-memory-package-list-state" role="status">准备读取云端备份...</div>
                  )}
                  {cloudListLoading && (
                    <div className="bake-memory-package-list-state bake-memory-package-list-state--loading" role="status">
                      正在读取云端备份...
                    </div>
                  )}
                  {cloudSnapshotsStatus === 'error' && (
                    <div className="bake-memory-package-list-state bake-memory-package-list-state--error" role="alert">
                      {cloudSnapshotsError || '云端备份暂时无法读取，请稍后重试。'}
                    </div>
                  )}
                  {cloudSnapshotsStatus === 'ready' && cloudSnapshots.length === 0 && (
                    <div className="bake-memory-package-list-state">还没有云端备份，可以先创建一份。</div>
                  )}
                  {cloudSnapshotsStatus === 'ready' && cloudSnapshots.length > 0 && (
                    <select
                      className="bake-input"
                      aria-label="云端备份"
                      value={selectedCloudSnapshotId}
                      onChange={(event) => onCloudSnapshotChange(event.target.value)}
                    >
                      {cloudSnapshots.map(snapshot => (
                        <option key={snapshot.id} value={snapshot.id}>
                          {snapshot.committed_at ? new Date(snapshot.committed_at).toLocaleString('zh-CN') : snapshot.id} · {formatBytes(snapshot.encrypted_size)}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              </div>

              <div className="bake-actions bake-actions--secondary bake-memory-package-actions">
                <BakeButton
                  compact
                  disabled={isBusy || cloudListLoading}
                  onClick={onRefreshCloudSnapshots}
                >
                  {cloudListLoading ? '正在读取...' : '刷新列表'}
                </BakeButton>
                <BakeButton compact primary disabled={isBusy} onClick={onBackupToCloud}>
                  {busy === 'cloud-backup' ? '正在备份...' : '备份到云端'}
                </BakeButton>
                <BakeButton
                  compact
                  disabled={isBusy || cloudListLoading || !selectedCloudSnapshotId}
                  onClick={onRestoreFromCloud}
                >
                  {busy === 'cloud-restore' ? '正在恢复...' : '恢复到本机'}
                </BakeButton>
              </div>
            </div>
          )}
        </div>
      </div>

      {lastImportReport && (
        <div className="bake-memory-package-report" aria-label="记忆包导入结果">
          {importReportRows(lastImportReport).map(row => {
            const content = `${row.label} 新增 ${row.inserted} / 更新 ${row.updated} / 保留本机 ${row.skipped}`
            return importDetailTabs[row.name] && row.inserted + row.updated > 0 ? (
              <button key={row.name} type="button" onClick={() => onOpenImportDetails?.(row.name)} aria-label={`查看${row.label}导入明细`}>
                {content}<ChevronRight size={13} aria-hidden />
              </button>
            ) : <span key={row.name}>{content}</span>
          })}
        </div>
      )}
    </BakeCard>
  )
}

const MemoryBackupSection: React.FC = () => {
  const {
    adminApiBaseUrl,
    serviceEnvironment,
    authToken,
    currentUser,
    setWindowMode,
    setBakeMemoryOffset,
  } = useAppStore()

  const exportMemoryPackage = useExportMemoryPackage()
  const importMemoryPackage = useImportMemoryPackage()
  const backupMemoryPackageToCloud = useBackupMemoryPackageToCloud()
  const restoreMemoryPackageFromCloud = useRestoreMemoryPackageFromCloud()

  const [statusNotice, setStatusNotice] = useState<MemoryBackupNotice | null>(null)
  const [memoryPackageBusy, setMemoryPackageBusy] = useState<MemoryPackageBusy>(null)
  const [cloudSnapshots, setCloudSnapshots] = useState<CloudSnapshot[]>([])
  const [cloudSnapshotsStatus, setCloudSnapshotsStatus] = useState<CloudSnapshotsStatus>('idle')
  const [cloudSnapshotsError, setCloudSnapshotsError] = useState<string | null>(null)
  const [selectedCloudSnapshotId, setSelectedCloudSnapshotId] = useState('')
  const [lastImportReport, setLastImportReport] = useState<MemoryPackageImportReport | null>(null)
  const [backupActivities, setBackupActivities] = useState<BackupActivity[]>(readBackupActivities)
  const cloudSnapshotsRequestSeqRef = useRef(0)
  const importFileInputRef = useRef<HTMLInputElement | null>(null)

  const setStatusMessage = useCallback((message: string, exportPath?: string) => {
    setStatusNotice({ message, exportPath })
  }, [])

  const storeActivities = useCallback((update: (current: BackupActivity[]) => BackupActivity[]) => {
    setBackupActivities(current => {
      const next = update(current).slice(0, MAX_BACKUP_ACTIVITIES)
      try {
        window.localStorage.setItem(BACKUP_ACTIVITY_STORAGE_KEY, JSON.stringify(next))
      } catch {
        // 流水写入失败不应中断实际备份或恢复操作。
      }
      return next
    })
  }, [])

  const beginActivity = useCallback((kind: BackupActivityKind, title: string, details: BackupActivityDetail[] = []) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    storeActivities(current => [{
      id,
      kind,
      title,
      status: 'running',
      createdAt: Date.now(),
      summary: '操作正在进行',
      details,
    }, ...current])
    return id
  }, [storeActivities])

  const finishActivity = useCallback((id: string, status: Exclude<BackupActivityStatus, 'running'>, summary: string, details: BackupActivityDetail[]) => {
    storeActivities(current => current.map(activity => activity.id === id ? {
      ...activity,
      status,
      completedAt: Date.now(),
      summary,
      details: [...activity.details, ...details],
    } : activity))
  }, [storeActivities])

  const isSignedIn = Boolean(authToken && currentUser)
  const cloudBackupAccessState: CloudBackupAccessState = isSignedIn ? 'available' : 'signed-out'

  const ensureCloudDevice = async () => {
    if (!authToken || !currentUser) throw new Error('请先登录账户')
    return (await registerCurrentDevice(adminApiBaseUrl, authToken, {
      environment: serviceEnvironment,
      userId: currentUser.id,
    })).id
  }

  const handleExportMemoryPackage = async () => {
    const activityId = beginActivity('local-export', '导出记忆包')
    setMemoryPackageBusy('export')
    setLastImportReport(null)
    try {
      const result = await exportMemoryPackage(collectClientStateForBackup())
      const skillCount = result.manifest.table_summaries
        .find(item => item.name === 'creation_skills')?.row_count || 0
      const contentSummary = [
        `${result.manifest.table_summaries.length} 个数据表`,
        `${result.manifest.local_file_count || 0} 个本地文件`,
        `${skillCount} 个 Skill`,
      ].join(' / ')
      setStatusMessage(
        `完整备份包已保存：${result.path}（${formatBytes(result.file_size_bytes)}，${contentSummary}；不含原始采集）`,
        result.path,
      )
      finishActivity(activityId, 'success', `${formatBytes(result.file_size_bytes)}，${contentSummary}`, [
        { label: '导出目录', value: parentPath(result.path) },
        { label: '导出文件', value: result.path },
        { label: '文件大小', value: formatBytes(result.file_size_bytes) },
        { label: '数据表数量', value: String(result.manifest.table_summaries.length) },
        { label: '本地文件数量', value: String(result.manifest.local_file_count || 0) },
        { label: 'Skill 数量', value: String(skillCount) },
      ])
    } catch (error) {
      const message = toUserFacingError(error, '记忆包导出失败')
      setStatusMessage(message)
      finishActivity(activityId, 'failed', message, [])
    } finally {
      setMemoryPackageBusy(null)
    }
  }

  const handleImportMemoryPackageFile = async (file?: File | null) => {
    if (!file) return
    const activityId = beginActivity('local-import', '导入记忆包', [
      { label: '导入文件', value: file.name },
      { label: '文件大小', value: formatBytes(file.size) },
    ])
    setMemoryPackageBusy('import')
    setLastImportReport(null)
    try {
      const content = await file.text()
      const report = await importMemoryPackage(content, false)
      restoreClientStateFromBackup(report.client_state)
      setLastImportReport(report)
      setStatusMessage(`备份包导入完成：${summarizeImportReport(report)}`)
      const counts = importCounts(report)
      finishActivity(activityId, 'success', `输入 ${counts.incoming} 行，去重 ${counts.deduplicated} 行，实际导入 ${counts.imported} 行`, importActivityDetails(report))
      setBakeMemoryOffset(0)
    } catch (error) {
      const message = toUserFacingError(error, '记忆包导入失败')
      setStatusMessage(message)
      finishActivity(activityId, 'failed', message, [])
    } finally {
      setMemoryPackageBusy(null)
      if (importFileInputRef.current) importFileInputRef.current.value = ''
    }
  }

  const handleOpenExportFolder = async () => {
    if (!statusNotice?.exportPath) return
    try {
      await invoke('open_export_folder', { path: statusNotice.exportPath })
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '无法打开备份所在文件夹'))
    }
  }

  const handleOpenImportDetails = (tableName: string) => {
    const target = importDetailTabs[tableName]
    if (!target) return
    if (target.windowMode === 'knowledge') {
      useAppStore.setState({
        windowMode: 'knowledge',
        repositoryTab: 'memory',
        repositoryMemoryFocusId: null,
        selectedMemoryId: null,
        bakeMemoryOffset: 0,
      })
      return
    }
    useAppStore.setState({ windowMode: 'bake', bakeTab: target.bakeTab })
  }

  const loadCloudSnapshots = useCallback(async (announceResult: boolean) => {
    if (!authToken || !isSignedIn) return
    const requestSeq = cloudSnapshotsRequestSeqRef.current + 1
    cloudSnapshotsRequestSeqRef.current = requestSeq
    setCloudSnapshotsStatus('loading')
    setCloudSnapshotsError(null)
    try {
      const snapshots = await fetchCloudSnapshots(adminApiBaseUrl, authToken)
      if (requestSeq !== cloudSnapshotsRequestSeqRef.current) return
      setCloudSnapshots(snapshots)
      setSelectedCloudSnapshotId(prev => snapshots.some(item => item.id === prev) ? prev : snapshots[0]?.id || '')
      setCloudSnapshotsStatus('ready')
      if (announceResult) {
        setStatusMessage(snapshots.length > 0 ? `已读取 ${snapshots.length} 个云端备份` : '当前账户还没有云端备份')
      }
    } catch (error) {
      if (requestSeq !== cloudSnapshotsRequestSeqRef.current) return
      const message = toUserFacingError(error, '云端备份列表读取失败')
      setCloudSnapshotsStatus('error')
      setCloudSnapshotsError(message)
      if (announceResult) setStatusMessage(message)
    }
  }, [adminApiBaseUrl, authToken, isSignedIn, setStatusMessage])

  useEffect(() => {
    if (!authToken || !isSignedIn) {
      cloudSnapshotsRequestSeqRef.current += 1
      setCloudSnapshots([])
      setSelectedCloudSnapshotId('')
      setCloudSnapshotsStatus('idle')
      setCloudSnapshotsError(null)
      return
    }
    void loadCloudSnapshots(false)
    return () => {
      cloudSnapshotsRequestSeqRef.current += 1
    }
  }, [authToken, isSignedIn, loadCloudSnapshots])

  const handleBackupMemoryPackageToCloud = async () => {
    if (!authToken || !isSignedIn) {
      setStatusMessage('请先登录账户')
      return
    }
    const activityId = beginActivity('cloud-backup', '云端备份')
    setMemoryPackageBusy('cloud-backup')
    try {
      const deviceId = await ensureCloudDevice()
      const result = await backupMemoryPackageToCloud({
        admin_base_url: adminApiBaseUrl,
        service_environment: serviceEnvironment,
        access_token: authToken,
        device_id: deviceId,
        client_state: collectClientStateForBackup(),
      })
      setCloudSnapshots(prev => [result.snapshot, ...prev.filter(item => item.id !== result.snapshot.id)])
      setSelectedCloudSnapshotId(result.snapshot.id)
      setCloudSnapshotsStatus('ready')
      setCloudSnapshotsError(null)
      setStatusMessage(`云端备份完成：${formatBytes(result.encrypted_size)}，校验值 ${result.checksum_sha256.slice(0, 12)}...`)
      finishActivity(activityId, 'success', `${formatBytes(result.encrypted_size)}，已加密上传`, [
        { label: '云端备份编号', value: result.snapshot.id },
        { label: '本机加密包', value: result.local_encrypted_path },
        { label: '加密包大小', value: formatBytes(result.encrypted_size) },
        { label: 'SHA-256', value: result.checksum_sha256 },
      ])
    } catch (error) {
      const message = toUserFacingError(error, '云端上传失败')
      setStatusMessage(message)
      finishActivity(activityId, 'failed', message, [])
    } finally {
      setMemoryPackageBusy(null)
    }
  }

  const handleRestoreMemoryPackageFromCloud = async () => {
    if (!authToken) {
      setStatusMessage('请先登录账户')
      return
    }
    const activityId = beginActivity('cloud-restore', '云端导入', [
      { label: '云端备份编号', value: selectedCloudSnapshotId },
    ])
    if (!selectedCloudSnapshotId) {
      setStatusMessage('请选择云端记忆包')
      return
    }
    setMemoryPackageBusy('cloud-restore')
    setLastImportReport(null)
    try {
      const result = await restoreMemoryPackageFromCloud({
        admin_base_url: adminApiBaseUrl,
        service_environment: serviceEnvironment,
        access_token: authToken,
        snapshot_id: selectedCloudSnapshotId,
        import_to_local: true,
        dry_run: false,
      })
      restoreClientStateFromBackup(result.import_report?.client_state)
      setLastImportReport(result.import_report ?? null)
      setStatusMessage(result.import_report
        ? `云端下载并恢复完成：${summarizeImportReport(result.import_report)}`
        : '云端记忆包已下载')
      const counts = result.import_report ? importCounts(result.import_report) : null
      finishActivity(activityId, 'success', counts
        ? `输入 ${counts.incoming} 行，去重 ${counts.deduplicated} 行，实际导入 ${counts.imported} 行`
        : '云端记忆包已下载', [
        { label: '下载目录', value: parentPath(result.local_decrypted_path) },
        { label: '解密记忆包', value: result.local_decrypted_path },
        { label: '本机加密包', value: result.local_encrypted_path },
        { label: '加密包大小', value: formatBytes(result.encrypted_size) },
        ...(result.import_report ? importActivityDetails(result.import_report) : []),
      ])
      setBakeMemoryOffset(0)
    } catch (error) {
      const message = toUserFacingError(error, '云端下载失败')
      setStatusMessage(message)
      finishActivity(activityId, 'failed', message, [])
    } finally {
      setMemoryPackageBusy(null)
    }
  }

  return (
    <section className="bake-memory-backup-section" aria-label="记忆备份">
      {statusNotice && (
        <div className="bake-inline-message bake-memory-backup-status" role="status">
          <span>{statusNotice.message}</span>
          {statusNotice.exportPath && (
            <BakeButton compact onClick={() => void handleOpenExportFolder()}>
              <FolderOpen size={14} aria-hidden />
              打开文件夹
            </BakeButton>
          )}
        </div>
      )}
      <MemoryBackupCard
        accessState={cloudBackupAccessState}
        busy={memoryPackageBusy}
        cloudSnapshots={cloudSnapshots}
        cloudSnapshotsStatus={cloudSnapshotsStatus}
        cloudSnapshotsError={cloudSnapshotsError}
        selectedCloudSnapshotId={selectedCloudSnapshotId}
        lastImportReport={lastImportReport}
        importFileInputRef={importFileInputRef}
        onExport={() => void handleExportMemoryPackage()}
        onImportClick={() => importFileInputRef.current?.click()}
        onImportFile={(file) => void handleImportMemoryPackageFile(file)}
        onOpenAccount={() => setWindowMode('account')}
        onCloudSnapshotChange={setSelectedCloudSnapshotId}
        onRefreshCloudSnapshots={() => void loadCloudSnapshots(true)}
        onBackupToCloud={() => void handleBackupMemoryPackageToCloud()}
        onRestoreFromCloud={() => void handleRestoreMemoryPackageFromCloud()}
        onOpenImportDetails={handleOpenImportDetails}
      />
      <BackupActivityLedger activities={backupActivities} />
    </section>
  )
}

export default MemoryBackupSection
