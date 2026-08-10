import React, { useCallback, useEffect, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { ChevronRight, Cloud, FolderOpen, HardDriveDownload, LockKeyhole, LogIn, ShieldCheck } from 'lucide-react'
import {
  useBackupMemoryPackageToCloud,
  useExportMemoryPackage,
  useImportMemoryPackage,
  useRestoreMemoryPackageFromCloud,
} from '../hooks/useApi'
import { useAppStore } from '../store/useAppStore'
import type { CloudSnapshot, MemoryPackageImportReport } from '../types'
import { fetchCloudSnapshots } from '../utils/authApi'
import { registerCurrentDevice } from '../utils/softwareUpdate'
import { toUserFacingError } from '../utils/userFacingError'
import { BakeButton, BakeCard, BakePill, BakeSectionHeader } from './bake/BakeShared'

const tableLabels: Record<string, string> = {
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

const summarizeImportReport = (report: MemoryPackageImportReport) => {
  const rows = [report.capture_refs, ...report.tables]
  const inserted = rows.reduce((sum, item) => sum + (item.inserted || 0), 0)
  const updated = rows.reduce((sum, item) => sum + (item.updated || 0), 0)
  const skipped = rows.reduce((sum, item) => sum + (item.skipped || 0), 0)
  return `新增 ${inserted}，更新 ${updated}，跳过 ${skipped}`
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
              <div className="bake-muted">随时导出到本机，或从已有记忆包恢复。</div>
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
              <div className="bake-muted">跨设备传输加密记忆包，每个账号默认 50MB。</div>
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
            const content = `${row.label} 新增 ${row.inserted} / 更新 ${row.updated} / 跳过 ${row.skipped}`
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
  const cloudSnapshotsRequestSeqRef = useRef(0)
  const importFileInputRef = useRef<HTMLInputElement | null>(null)

  const setStatusMessage = useCallback((message: string, exportPath?: string) => {
    setStatusNotice({ message, exportPath })
  }, [])

  const isSignedIn = Boolean(authToken && currentUser)
  const cloudBackupAccessState: CloudBackupAccessState = isSignedIn ? 'available' : 'signed-out'

  const ensureCloudDevice = async () => {
    if (!authToken) throw new Error('请先登录账户')
    return (await registerCurrentDevice(adminApiBaseUrl, authToken)).id
  }

  const handleExportMemoryPackage = async () => {
    setMemoryPackageBusy('export')
    setLastImportReport(null)
    try {
      const result = await exportMemoryPackage()
      const tables = result.manifest.table_summaries
        .map(item => `${tableLabels[item.name] ?? item.name} ${item.row_count}`)
        .join(' / ')
      setStatusMessage(
        `记忆包已保存：${result.path}（${formatBytes(result.file_size_bytes)}，${tables || '暂无数据'}）`,
        result.path,
      )
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '记忆包导出失败'))
    } finally {
      setMemoryPackageBusy(null)
    }
  }

  const handleImportMemoryPackageFile = async (file?: File | null) => {
    if (!file) return
    setMemoryPackageBusy('import')
    setLastImportReport(null)
    try {
      const content = await file.text()
      const report = await importMemoryPackage(content, false)
      setLastImportReport(report)
      setStatusMessage(`记忆包导入完成：${summarizeImportReport(report)}`)
      setBakeMemoryOffset(0)
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '记忆包导入失败'))
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
    setMemoryPackageBusy('cloud-backup')
    try {
      const deviceId = await ensureCloudDevice()
      const result = await backupMemoryPackageToCloud({
        admin_base_url: adminApiBaseUrl,
        service_environment: serviceEnvironment,
        access_token: authToken,
        device_id: deviceId,
      })
      setCloudSnapshots(prev => [result.snapshot, ...prev.filter(item => item.id !== result.snapshot.id)])
      setSelectedCloudSnapshotId(result.snapshot.id)
      setCloudSnapshotsStatus('ready')
      setCloudSnapshotsError(null)
      setStatusMessage(`云端备份完成：${formatBytes(result.encrypted_size)}，校验值 ${result.checksum_sha256.slice(0, 12)}...`)
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '云端上传失败'))
    } finally {
      setMemoryPackageBusy(null)
    }
  }

  const handleRestoreMemoryPackageFromCloud = async () => {
    if (!authToken) {
      setStatusMessage('请先登录账户')
      return
    }
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
      setLastImportReport(result.import_report ?? null)
      setStatusMessage(result.import_report
        ? `云端下载并导入完成：${summarizeImportReport(result.import_report)}`
        : '云端记忆包已下载')
      setBakeMemoryOffset(0)
    } catch (error) {
      setStatusMessage(toUserFacingError(error, '云端下载失败'))
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
    </section>
  )
}

export default MemoryBackupSection
