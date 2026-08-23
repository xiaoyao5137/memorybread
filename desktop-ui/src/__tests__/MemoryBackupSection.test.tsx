import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { invoke } from '@tauri-apps/api/core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MemoryBackupSection from '../components/MemoryBackupSection'
import RepositoryPanel from '../components/RepositoryPanel'
import { useAppStore } from '../store/useAppStore'

const mocks = vi.hoisted(() => ({
  fetchMemories: vi.fn(),
  fetchMemory: vi.fn(),
  deleteMemory: vi.fn(),
  fetchCaptures: vi.fn(),
  fetchCaptureDetail: vi.fn(),
  deleteCapture: vi.fn(),
  fetchCapturesRaw: vi.fn(),
  fetchTemplates: vi.fn(),
  fetchKnowledge: vi.fn(),
  fetchKnowledgeDetail: vi.fn(),
  fetchTimelineRelations: vi.fn(),
  fetchSops: vi.fn(),
  fetchSop: vi.fn(),
  fetchDataSources: vi.fn(),
  exportMemoryPackage: vi.fn(),
  importMemoryPackage: vi.fn(),
  backupMemoryPackageToCloud: vi.fn(),
  restoreMemoryPackageFromCloud: vi.fn(),
  fetchCloudSnapshots: vi.fn(),
  upsertCloudDevice: vi.fn(),
}))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('../hooks/useApi', () => ({
  useFetchBakeMemories: () => mocks.fetchMemories,
  useFetchBakeMemory: () => mocks.fetchMemory,
  useDeleteBakeMemory: () => mocks.deleteMemory,
  useFetchBakeCaptures: () => mocks.fetchCaptures,
  useFetchBakeCaptureDetail: () => mocks.fetchCaptureDetail,
  useDeleteBakeCapture: () => mocks.deleteCapture,
  useFetchCaptures: () => mocks.fetchCapturesRaw,
  useFetchBakeTemplates: () => mocks.fetchTemplates,
  useFetchBakeKnowledge: () => mocks.fetchKnowledge,
  useFetchBakeKnowledgeDetail: () => mocks.fetchKnowledgeDetail,
  useFetchBakeMemoryRelations: () => mocks.fetchTimelineRelations,
  useFetchBakeSops: () => mocks.fetchSops,
  useFetchBakeSop: () => mocks.fetchSop,
  useFetchDataSources: () => mocks.fetchDataSources,
  useExportMemoryPackage: () => mocks.exportMemoryPackage,
  useImportMemoryPackage: () => mocks.importMemoryPackage,
  useBackupMemoryPackageToCloud: () => mocks.backupMemoryPackageToCloud,
  useRestoreMemoryPackageFromCloud: () => mocks.restoreMemoryPackageFromCloud,
}))

vi.mock('../utils/authApi', () => ({
  fetchCloudSnapshots: mocks.fetchCloudSnapshots,
  upsertCloudDevice: mocks.upsertCloudDevice,
}))

vi.mock('../utils/softwareUpdate', () => ({
  registerCurrentDevice: vi.fn().mockResolvedValue({ id: 'device-1' }),
}))

beforeEach(() => {
  window.localStorage.clear()
  Object.values(mocks).forEach(mock => mock.mockReset())
  vi.mocked(invoke).mockReset().mockResolvedValue(undefined)
  mocks.fetchMemories.mockResolvedValue({ items: [], total: 0 })
  mocks.fetchCaptures.mockResolvedValue({ items: [], total: 0 })
  mocks.fetchTemplates.mockResolvedValue({ items: [], total: 0 })
  mocks.fetchKnowledge.mockResolvedValue({ items: [], total: 0 })
  mocks.fetchSops.mockResolvedValue({ items: [], total: 0 })
  mocks.fetchDataSources.mockResolvedValue({ items: [], total: 0 })
  mocks.fetchCapturesRaw.mockResolvedValue([])
  mocks.fetchCloudSnapshots.mockResolvedValue([{
    id: 'snapshot-after-login',
    device_id: 'device-1',
    encrypted_size: 4096,
    status: 'committed',
    committed_at: '2026-07-13T10:00:00Z',
  }])

  useAppStore.getState().reset()
  useAppStore.getState().clearAuthSession()
})

describe('MemoryBackupSection', () => {
  it('导出成功后可打开备份所在文件夹', async () => {
    mocks.exportMemoryPackage.mockResolvedValue({
      path: '/tmp/memorybread-test/.memory-bread/backups/memory-package-1.mbmemory.json',
      file_sha256: 'file-sha256',
      file_size_bytes: 2048,
      manifest: {
        app: 'memory-bread',
        format_version: 2,
        schema_version: 5,
        exported_at_ms: 1,
        source_db_path: '/tmp/memorybread-test/.memory-bread/memory.db',
        excluded_tables: [],
        excluded_capture_columns: [],
        local_file_count: 2,
        payload_sha256: 'payload-sha256',
        table_summaries: [
          { name: 'timelines', row_count: 3, identity_columns: ['id'] },
          { name: 'bake_sops', row_count: 2, identity_columns: ['id'] },
          { name: 'data_sources', row_count: 4, identity_columns: ['canonical_key'] },
          { name: 'data_snapshots', row_count: 4, identity_columns: ['id'] },
          { name: 'data_source_links', row_count: 7, identity_columns: ['source_ref_key'] },
          { name: 'creation_skills', row_count: 6, identity_columns: ['client_skill_key'] },
        ],
      },
    })

    render(<MemoryBackupSection />)
    fireEvent.click(screen.getByRole('button', { name: '导出记忆包' }))

    const notice = await screen.findByText(/完整备份包已保存：/)
    expect(notice).toHaveTextContent('memory-package-1.mbmemory.json')
    expect(notice).toHaveTextContent('6 个数据表')
    expect(notice).toHaveTextContent('2 个本地文件')
    expect(notice).toHaveTextContent('6 个 Skill')
    expect(notice).toHaveTextContent('不含原始采集')
    fireEvent.click(screen.getByRole('button', { name: '打开文件夹' }))

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('open_export_folder', {
        path: '/tmp/memorybread-test/.memory-bread/backups/memory-package-1.mbmemory.json',
      })
    })

    const activitySummary = screen.getByText('2.0 KB，6 个数据表 / 2 个本地文件 / 6 个 Skill')
    fireEvent.click(activitySummary.closest('summary')!)
    expect(screen.getByText('导出目录')).toBeInTheDocument()
    expect(screen.getByText('/tmp/memorybread-test/.memory-bread/backups')).toBeInTheDocument()
    expect(JSON.parse(window.localStorage.getItem('memory-bread_backup_activity_v1') || '[]')).toMatchObject([{
      title: '导出记忆包',
      status: 'success',
    }])
  })

  it('登录后自动读取并显示云端备份', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs-test-token',
      expires_at: '2026-07-14T10:00:00Z',
      user: {
        id: 'user-1',
        email: 'user@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-07-13T10:00:00Z',
      },
    })
    render(<MemoryBackupSection />)

    await waitFor(() => {
      expect(mocks.fetchCloudSnapshots).toHaveBeenCalledWith(
        useAppStore.getState().adminApiBaseUrl,
        'mbs-test-token',
      )
    })
    expect(await screen.findByRole('combobox', { name: '云端备份' })).toHaveValue('snapshot-after-login')
    expect(screen.getByRole('button', { name: '备份到云端' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '恢复到本机' })).toBeEnabled()
  })

  it('云端备份和云端导入均记录路径与导入统计', async () => {
    useAppStore.getState().setAuthSession({
      access_token: 'mbs-test-token',
      expires_at: '2026-07-14T10:00:00Z',
      user: {
        id: 'user-1',
        email: 'user@memorybread.local',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-07-13T10:00:00Z',
      },
    })
    mocks.backupMemoryPackageToCloud.mockResolvedValue({
      local_encrypted_path: '/tmp/memorybread-test/backups/cloud.mbmemory.enc.json',
      oss_object_key: 'snapshot-object',
      checksum_sha256: 'abcdef1234567890',
      encrypted_size: 4096,
      snapshot: {
        id: 'snapshot-new',
        device_id: 'device-1',
        encrypted_size: 4096,
        status: 'committed',
        committed_at: '2026-07-13T11:00:00Z',
      },
    })
    mocks.restoreMemoryPackageFromCloud.mockResolvedValue({
      local_encrypted_path: '/tmp/memorybread-test/backups/restored.mbmemory.enc.json',
      local_decrypted_path: '/tmp/memorybread-test/backups/restored.mbmemory.json',
      checksum_sha256: 'abcdef1234567890',
      encrypted_size: 4096,
      oss_object_key: 'snapshot-object',
      import_report: {
        file_sha256: 'file-sha256',
        payload_sha256: 'payload-sha256',
        dry_run: false,
        target_directory: '/tmp/memorybread-test/.memory-bread',
        capture_refs: { name: 'capture_refs', incoming: 0, inserted: 0, updated: 0, skipped: 0 },
        tables: [{ name: 'timelines', incoming: 5, inserted: 3, updated: 0, skipped: 2 }],
      },
    })
    render(<MemoryBackupSection />)
    await screen.findByRole('combobox', { name: '云端备份' })

    fireEvent.click(screen.getByRole('button', { name: '备份到云端' }))
    const backupSummary = await screen.findByText('4.0 KB，已加密上传')
    fireEvent.click(backupSummary.closest('summary')!)
    expect(screen.getByText('/tmp/memorybread-test/backups/cloud.mbmemory.enc.json')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '恢复到本机' }))
    const restoreSummary = await screen.findByText('输入 5 行，去重 2 行，实际导入 3 行')
    fireEvent.click(restoreSummary.closest('summary')!)
    expect(screen.getByText('/tmp/memorybread-test/backups/restored.mbmemory.json')).toBeInTheDocument()
    expect(screen.getByText('/tmp/memorybread-test/.memory-bread')).toBeInTheDocument()
    expect(JSON.parse(window.localStorage.getItem('memory-bread_backup_activity_v1') || '[]')).toMatchObject([
      { title: '云端导入', status: 'success' },
      { title: '云端备份', status: 'success' },
    ])
  })

  it('备份导入结果可进入对应数据明细', async () => {
    mocks.importMemoryPackage.mockResolvedValue({
      file_sha256: 'file-sha256',
      payload_sha256: 'payload-sha256',
      dry_run: false,
      target_directory: '/tmp/memorybread-test/.memory-bread',
      capture_refs: { name: 'capture_refs', incoming: 0, inserted: 0, updated: 0, skipped: 0 },
      tables: [{ name: 'data_sources', incoming: 5, inserted: 2, updated: 0, skipped: 3 }],
    })
    render(<MemoryBackupSection />)
    const file = new File(['{}'], 'memory.mbmemory.json', { type: 'application/json' })
    Object.defineProperty(file, 'text', { value: vi.fn().mockResolvedValue('{}') })
    fireEvent.change(screen.getByLabelText('选择本机记忆包'), { target: { files: [file] } })

    const detailLink = await screen.findByRole('button', { name: '查看数据记录导入明细' })
    fireEvent.click(detailLink)

    expect(useAppStore.getState()).toMatchObject({ windowMode: 'bake', bakeTab: 'data' })
    const activitySummary = screen.getByText('输入 5 行，去重 3 行，实际导入 2 行')
    fireEvent.click(activitySummary.closest('summary')!)
    expect(screen.getByText('恢复目录')).toBeInTheDocument()
    expect(screen.getByText('/tmp/memorybread-test/.memory-bread')).toBeInTheDocument()
    expect(screen.getByText('输入数据行数')).toBeInTheDocument()
    expect(screen.getByText('去重行数')).toBeInTheDocument()
    expect(screen.getByText('实际导入行数')).toBeInTheDocument()
  })

  it('失败的备份操作也会写入流水', async () => {
    mocks.exportMemoryPackage.mockRejectedValue(new Error('磁盘空间不足'))
    render(<MemoryBackupSection />)

    fireEvent.click(screen.getByRole('button', { name: '导出记忆包' }))

    expect(await screen.findAllByText('磁盘空间不足')).toHaveLength(2)
    expect(screen.getByText('失败')).toBeInTheDocument()
    expect(JSON.parse(window.localStorage.getItem('memory-bread_backup_activity_v1') || '[]')).toMatchObject([{
      title: '导出记忆包',
      status: 'failed',
      summary: '磁盘空间不足',
    }])
  })

  it('完整备份按判重报告合并并补充本机缺失的客户端设置', async () => {
    mocks.importMemoryPackage.mockResolvedValue({
      file_sha256: 'file-sha256',
      payload_sha256: 'payload-sha256',
      dry_run: false,
      database_replaced: false,
      local_files: { incoming: 2, written: 2, unchanged: 0, conflicts: 0 },
      client_state: { 'memory-bread_creation_tools_v1': '[{"id":"restored-tool"}]' },
      capture_refs: { name: 'capture_refs', incoming: 0, inserted: 0, updated: 0, skipped: 0 },
      tables: [{ name: 'creation_skills', incoming: 3, inserted: 3, updated: 0, skipped: 0 }],
    })
    render(<MemoryBackupSection />)
    const file = new File(['{}'], 'complete.mbmemory.json', { type: 'application/json' })
    Object.defineProperty(file, 'text', { value: vi.fn().mockResolvedValue('{}') })
    fireEvent.change(screen.getByLabelText('选择本机记忆包'), { target: { files: [file] } })

    expect(await screen.findByText(/合并完成/)).toHaveTextContent('新增 3')
    expect(window.localStorage.getItem('memory-bread_creation_tools_v1')).toContain('restored-tool')
    expect(invoke).not.toHaveBeenCalledWith('restart_application')
  })

  it('导入客户端设置时保留本机已有值', async () => {
    window.localStorage.setItem('memory-bread_creation_tools_v1', '[{"id":"local-tool"}]')
    mocks.importMemoryPackage.mockResolvedValue({
      file_sha256: 'file-sha256',
      payload_sha256: 'payload-sha256',
      dry_run: false,
      database_replaced: false,
      local_files: { incoming: 0, written: 0, unchanged: 0, conflicts: 0 },
      client_state: { 'memory-bread_creation_tools_v1': '[{"id":"backup-tool"}]' },
      capture_refs: { name: 'capture_refs', incoming: 0, inserted: 0, updated: 0, skipped: 0 },
      tables: [],
    })
    render(<MemoryBackupSection />)
    const file = new File(['{}'], 'merge.mbmemory.json', { type: 'application/json' })
    Object.defineProperty(file, 'text', { value: vi.fn().mockResolvedValue('{}') })
    fireEvent.change(screen.getByLabelText('选择本机记忆包'), { target: { files: [file] } })

    await screen.findByText(/合并完成/)
    expect(window.localStorage.getItem('memory-bread_creation_tools_v1')).toContain('local-tool')
    expect(window.localStorage.getItem('memory-bread_creation_tools_v1')).not.toContain('backup-tool')
  })

  it('不再出现在采集页面', async () => {
    render(<RepositoryPanel />)

    await waitFor(() => expect(mocks.fetchMemories).toHaveBeenCalled())
    expect(screen.queryByText('记忆备份')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '导出记忆包' })).not.toBeInTheDocument()
  })
})
