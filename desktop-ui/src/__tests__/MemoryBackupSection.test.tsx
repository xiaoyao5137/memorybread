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

beforeEach(() => {
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
        format_version: 1,
        schema_version: 1,
        exported_at_ms: 1,
        source_db_path: '/tmp/memorybread-test/.memory-bread/memory.db',
        excluded_tables: [],
        excluded_capture_columns: [],
        payload_sha256: 'payload-sha256',
        table_summaries: [
          { name: 'timelines', row_count: 3, identity_columns: ['id'] },
          { name: 'bake_sops', row_count: 2, identity_columns: ['id'] },
          { name: 'data_sources', row_count: 4, identity_columns: ['canonical_key'] },
          { name: 'data_snapshots', row_count: 4, identity_columns: ['id'] },
          { name: 'data_source_links', row_count: 7, identity_columns: ['source_ref_key'] },
        ],
      },
    })

    render(<MemoryBackupSection />)
    fireEvent.click(screen.getByRole('button', { name: '导出记忆包' }))

    const notice = await screen.findByText(/记忆包已保存：/)
    expect(notice).toHaveTextContent('memory-package-1.mbmemory.json')
    expect(notice).toHaveTextContent('操作 2')
    expect(notice).toHaveTextContent('数据记录 4')
    expect(notice).toHaveTextContent('数据快照 4')
    expect(notice).toHaveTextContent('数据来源关系 7')
    expect(notice).not.toHaveTextContent('data_sources')
    fireEvent.click(screen.getByRole('button', { name: '打开文件夹' }))

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith('open_export_folder', {
        path: '/tmp/memorybread-test/.memory-bread/backups/memory-package-1.mbmemory.json',
      })
    })
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

  it('备份导入结果可进入对应数据明细', async () => {
    mocks.importMemoryPackage.mockResolvedValue({
      file_sha256: 'file-sha256',
      payload_sha256: 'payload-sha256',
      dry_run: false,
      capture_refs: { name: 'capture_refs', incoming: 0, inserted: 0, updated: 0, skipped: 0 },
      tables: [{ name: 'data_sources', incoming: 2, inserted: 2, updated: 0, skipped: 0 }],
    })
    render(<MemoryBackupSection />)
    const file = new File(['{}'], 'memory.mbmemory.json', { type: 'application/json' })
    Object.defineProperty(file, 'text', { value: vi.fn().mockResolvedValue('{}') })
    fireEvent.change(screen.getByLabelText('选择本机记忆包'), { target: { files: [file] } })

    const detailLink = await screen.findByRole('button', { name: '查看数据记录导入明细' })
    fireEvent.click(detailLink)

    expect(useAppStore.getState()).toMatchObject({ windowMode: 'bake', bakeTab: 'data' })
  })

  it('不再出现在采集页面', async () => {
    render(<RepositoryPanel />)

    await waitFor(() => expect(mocks.fetchMemories).toHaveBeenCalled())
    expect(screen.queryByText('记忆备份')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '导出记忆包' })).not.toBeInTheDocument()
  })
})
