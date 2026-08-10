/**
 * DebugPanel v2 — 调试面板（优化版）
 *
 * 改进：
 * 1. 使用 SVG 图标替代 Emoji
 * 2. 保持 Image3 的优秀布局和配色
 * 3. 优化图标视觉效果
 */

import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  useFetchDebugLogContent,
  useFetchDebugLogFiles,
} from '../hooks/useApi'
import { useAppStore } from '../store/useAppStore'
import type { CaptureRecord, DebugLogContent, DebugLogFile } from '../types'
import { enableInitializationTestMode } from '../utils/initialization'
import { useConfirmDialog } from './useConfirmDialog'

interface DebugPanelProps {
  className?: string
}

interface VectorStatus {
  capture_id: number
  vectorized: boolean
  point_id: string | null
}

interface SystemStats {
  total_captures: number
  total_vectorized: number
  db_size_mb: number
  last_capture_ts: number | null
}

const DebugPanel: React.FC<DebugPanelProps> = ({ className = '' }) => {
  const { apiBaseUrl, setHasCompletedSetup, setWindowMode } = useAppStore()
  const { confirm: confirmDestructive, dialog: confirmDialog } = useConfirmDialog()
  const fetchDebugLogFiles = useFetchDebugLogFiles()
  const fetchDebugLogContent = useFetchDebugLogContent()

  const [captures, setCaptures] = useState<CaptureRecord[]>([])
  const [vectorStatus, setVectorStatus] = useState<VectorStatus[]>([])
  const [stats, setStats] = useState<SystemStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [refreshInterval, setRefreshInterval] = useState(5000)
  const [selectedCapture, setSelectedCapture] = useState<CaptureRecord | null>(null)
  const [highlightCaptureId, setHighlightCaptureId] = useState<number | null>(null)
  const [clearingQueue, setClearingQueue] = useState(false)
  const [clearQueueResult, setClearQueueResult] = useState<string | null>(null)
  const [logFiles, setLogFiles] = useState<DebugLogFile[]>([])
  const [selectedLogKey, setSelectedLogKey] = useState('')
  const selectedLogKeyRef = useRef('')
  const [selectedLogContent, setSelectedLogContent] = useState<DebugLogContent | null>(null)
  const [logLoading, setLogLoading] = useState(false)
  const [logError, setLogError] = useState<string | null>(null)
  const [initializationTestConfirming, setInitializationTestConfirming] = useState(false)
  const [initializationTestStarting, setInitializationTestStarting] = useState(false)
  const [initializationTestError, setInitializationTestError] = useState('')

  // 获取最新采集记录
  const fetchCaptures = useCallback(async () => {
    try {
      const res = await fetch(`${apiBaseUrl}/api/captures?limit=20`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setCaptures(data.captures || [])
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [apiBaseUrl])

  // 获取向量化状态
  const fetchVectorStatus = useCallback(async () => {
    try {
      const res = await fetch(`${apiBaseUrl}/api/vector/status`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setVectorStatus(data.items || [])
    } catch (e) {
      console.error('获取向量状态失败:', e)
    }
  }, [apiBaseUrl])

  // 获取系统统计
  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`${apiBaseUrl}/api/stats`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setStats(data)
    } catch (e) {
      console.error('获取统计信息失败:', e)
    }
  }, [apiBaseUrl])

  const loadLogContent = useCallback(async (key: string, files: DebugLogFile[] = logFiles) => {
    setSelectedLogKey(key)
    selectedLogKeyRef.current = key

    if (!key) {
      setSelectedLogContent(null)
      setLogError(null)
      return
    }

    const selected = files.find((item) => item.key === key)
    if (selected && !selected.exists) {
      setSelectedLogContent(null)
      setLogError(null)
      return
    }

    setLogLoading(true)
    try {
      const data = await fetchDebugLogContent(key)
      setSelectedLogContent(data)
      setLogError(null)
    } catch (e) {
      setSelectedLogContent(null)
      setLogError(e instanceof Error ? e.message : String(e))
    } finally {
      setLogLoading(false)
    }
  }, [fetchDebugLogContent, logFiles])

  const refreshLogs = useCallback(async () => {
    setLogLoading(true)
    try {
      const items = await fetchDebugLogFiles()
      setLogFiles(items)

      const currentKey = selectedLogKeyRef.current
      const nextKey = items.some((item) => item.key === currentKey)
        ? currentKey
        : (items[0]?.key ?? '')

      setSelectedLogKey(nextKey)
      selectedLogKeyRef.current = nextKey

      if (!nextKey) {
        setSelectedLogContent(null)
        setLogError(null)
        return
      }

      const selected = items.find((item) => item.key === nextKey)
      if (!selected?.exists) {
        setSelectedLogContent(null)
        setLogError(null)
        return
      }

      const data = await fetchDebugLogContent(nextKey)
      setSelectedLogContent(data)
      setLogError(null)
    } catch (e) {
      setSelectedLogContent(null)
      setLogError(e instanceof Error ? e.message : String(e))
    } finally {
      setLogLoading(false)
    }
  }, [fetchDebugLogContent, fetchDebugLogFiles])

  // 刷新所有数据
  const refreshAll = useCallback(async () => {
    setLoading(true)
    await Promise.all([fetchCaptures(), fetchVectorStatus(), fetchStats(), refreshLogs()])
    setLoading(false)
  }, [fetchCaptures, fetchVectorStatus, fetchStats, refreshLogs])

  // 初始加载
  useEffect(() => {
    refreshAll()
  }, [refreshAll])

  // 自动刷新
  useEffect(() => {
    if (!autoRefresh) return
    const timer = setInterval(refreshAll, refreshInterval)
    return () => clearInterval(timer)
  }, [autoRefresh, refreshInterval, refreshAll])

  // 监听滚动到指定采集记录的事件
  useEffect(() => {
    const handleScrollToCapture = (event: CustomEvent) => {
      const { captureId } = event.detail
      setHighlightCaptureId(captureId)

      const capture = captures.find((c) => c.id === captureId)
      if (capture) {
        setSelectedCapture(capture)
      }

      setTimeout(() => {
        setHighlightCaptureId(null)
      }, 3000)
    }

    window.addEventListener('scroll-to-capture', handleScrollToCapture as EventListener)
    return () => {
      window.removeEventListener('scroll-to-capture', handleScrollToCapture as EventListener)
    }
  }, [captures])

  const handleClose = () => setWindowMode('buddy')

  const handleClearExtractionQueue = async () => {
    if (!(await confirmDestructive({ title: '清空所有待提炼内容？', description: '此操作将跳过所有历史积压，无法恢复。' }))) return
    setClearingQueue(true)
    setClearQueueResult(null)
    try {
      const res = await fetch(`${apiBaseUrl}/api/debug/clear-extraction-queue`, { method: 'POST' })
      const data = await res.json()
      if (res.ok) {
        setClearQueueResult(`已清空 ${data.cleared} 条待提炼记录`)
      } else {
        setClearQueueResult(`清空失败: ${data.error || '未知错误'}`)
      }
    } catch (e) {
      setClearQueueResult(`请求失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setClearingQueue(false)
    }
  }

  const handleEnableInitializationTestMode = async () => {
    setInitializationTestStarting(true)
    setInitializationTestError('')
    try {
      await enableInitializationTestMode()
      setInitializationTestConfirming(false)
      setHasCompletedSetup(false)
    } catch (cause) {
      setInitializationTestError(cause instanceof Error ? cause.message : '初始化测试模式开启失败')
    } finally {
      setInitializationTestStarting(false)
    }
  }

  const requestInitializationTestMode = () => {
    setInitializationTestError('')
    setInitializationTestConfirming(true)
  }

  const cancelInitializationTestMode = useCallback(() => {
    if (initializationTestStarting) return
    setInitializationTestConfirming(false)
    setInitializationTestError('')
  }, [initializationTestStarting])

  useEffect(() => {
    if (!initializationTestConfirming) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') cancelInitializationTestMode()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [cancelInitializationTestMode, initializationTestConfirming])

  const formatTimestamp = (ts: number | undefined | null) => {
    if (!ts) return '无'
    const date = new Date(ts)
    return date.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  }

  const formatBytes = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  }

  const getVectorStatusForCapture = (captureId: number) => {
    return vectorStatus.find((v) => v.capture_id === captureId)
  }

  const selectedLogMeta = logFiles.find((item) => item.key === selectedLogKey) ?? null

  return (
    <div className={`min-h-screen bg-gray-50 p-6 ${className}`} data-testid="debug-panel">
      {/* 标题栏 */}
      <div className="bg-white rounded-lg shadow-sm p-4 mb-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            {/* 调试图标 - wrench.and.screwdriver */}
            <svg
              width="24"
              height="24"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-gray-700"
            >
              <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
            </svg>
            <h1 className="text-2xl font-bold text-gray-800">调试面板</h1>
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="rounded"
              />
              <span>自动刷新</span>
            </label>

            <select
              value={refreshInterval}
              onChange={(e) => setRefreshInterval(Number(e.target.value))}
              disabled={!autoRefresh}
              className="text-sm border rounded px-2 py-1"
            >
              <option value={1000}>1秒</option>
              <option value={2000}>2秒</option>
              <option value={5000}>5秒</option>
              <option value={10000}>10秒</option>
            </select>

            <button
              onClick={refreshAll}
              disabled={loading}
              className="px-4 py-1 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:bg-gray-300 text-sm flex items-center gap-2"
            >
              {/* 刷新图标 */}
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className={loading ? 'animate-spin' : ''}
              >
                <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                <path d="M3 3v5h5" />
                <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
                <path d="M16 16h5v5" />
              </svg>
              {loading ? '刷新中...' : '刷新'}
            </button>

            <button
              onClick={handleClose}
              className="px-4 py-1 bg-gray-500 text-white rounded hover:bg-gray-600 text-sm flex items-center gap-2"
            >
              {/* X 图标 */}
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M18 6 6 18" />
                <path d="m6 6 12 12" />
              </svg>
              关闭
            </button>
          </div>
        </div>
      </div>

      <section className="bg-amber-50 border border-amber-200 rounded-lg shadow-sm p-6 mb-6" data-testid="initialization-test-mode-card">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="max-w-3xl">
            <div className="flex items-center gap-2 mb-2">
              <svg
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="text-amber-800"
                aria-hidden
              >
                <path d="M10 2v7.31" />
                <path d="M14 9.3V2" />
                <path d="M8.5 2h7" />
                <path d="M14 9.3a6 6 0 1 1-4 0" />
                <path d="M5.52 16h12.96" />
              </svg>
              <h2 className="text-xl font-semibold text-amber-950">初始化测试模式</h2>
            </div>
            <p className="text-sm leading-6 text-amber-900">
              在独立运行时目录、模型目录、端口和临时数据库中强制执行完整首次初始化，
              用于验证安装、判重、质检以及采集、提炼、咨询、创作测试。真实模型和记忆库保持不变。
            </p>
            <p className="text-xs text-amber-700 mt-2">
              开启后应用会立即回到初始化门禁；完成或失败后可在那里关闭模式并自动清理沙箱。
            </p>
          </div>
          <button
            type="button"
            onClick={requestInitializationTestMode}
            disabled={initializationTestStarting}
            className="px-4 py-2 rounded bg-amber-800 text-white hover:bg-amber-900 disabled:bg-gray-300 text-sm"
          >
            {initializationTestStarting ? '正在创建沙箱…' : '开启初始化测试模式'}
          </button>
        </div>
        {initializationTestError && (
          <p className="mt-3 text-sm text-red-700" role="alert">{initializationTestError}</p>
        )}
      </section>

      {initializationTestConfirming && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="initialization-test-confirm-title"
          aria-describedby="initialization-test-confirm-description"
          data-testid="initialization-test-confirm-dialog"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) cancelInitializationTestMode()
          }}
        >
          <div className="w-full max-w-lg rounded-xl border border-amber-200 bg-white p-6 shadow-2xl">
            <h2
              id="initialization-test-confirm-title"
              className="text-xl font-semibold text-amber-950"
            >
              确认开启初始化测试模式
            </h2>
            <p
              id="initialization-test-confirm-description"
              className="mt-3 text-sm leading-6 text-gray-700"
            >
              应用将暂时忽略真实的本地 AI、模型和数据库，并在独立沙箱中重新安装和质检。
              关闭模式时会清理临时内容，真实工作环境不会被修改。
            </p>
            {initializationTestError && (
              <p className="mt-3 text-sm text-red-700" role="alert">
                {initializationTestError}
              </p>
            )}
            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={cancelInitializationTestMode}
                disabled={initializationTestStarting}
                autoFocus
                className="rounded border border-gray-300 bg-white px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void handleEnableInitializationTestMode()}
                disabled={initializationTestStarting}
                aria-busy={initializationTestStarting}
                className="rounded bg-amber-800 px-4 py-2 text-sm text-white hover:bg-amber-900 disabled:cursor-not-allowed disabled:bg-gray-300"
              >
                {initializationTestStarting ? '正在创建沙箱…' : '确认开启'}
              </button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-6 flex items-center gap-2">
          {/* 警告图标 */}
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
            <path d="M12 9v4" />
            <path d="M12 17h.01" />
          </svg>
          {error}
        </div>
      )}

      {/* 系统统计 */}
      {stats && (
        <section className="bg-white rounded-lg shadow-sm p-6 mb-6">
          <div className="flex items-center gap-2 mb-4">
            {/* 统计图标 - bar-chart */}
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-gray-700"
            >
              <line x1="12" x2="12" y1="20" y2="10" />
              <line x1="18" x2="18" y1="20" y2="4" />
              <line x1="6" x2="6" y1="20" y2="16" />
            </svg>
            <h2 className="text-xl font-semibold text-gray-800">系统统计</h2>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
            <div className="bg-blue-50 rounded p-4">
              <div className="text-sm text-gray-600 mb-1">总采集数</div>
              <div className="text-2xl font-bold text-blue-600">{stats.total_captures}</div>
            </div>
            <div className="bg-green-50 rounded p-4">
              <div className="text-sm text-gray-600 mb-1">已向量化</div>
              <div className="text-2xl font-bold text-green-600">{stats.total_vectorized}</div>
            </div>
            <div className="bg-purple-50 rounded p-4">
              <div className="text-sm text-gray-600 mb-1">向量化率</div>
              <div className="text-2xl font-bold text-purple-600">
                {stats.total_captures > 0
                  ? ((stats.total_vectorized / stats.total_captures) * 100).toFixed(1)
                  : 0}
                %
              </div>
            </div>
            <div className="bg-yellow-50 rounded p-4">
              <div className="text-sm text-gray-600 mb-1">数据库大小</div>
              <div className="text-2xl font-bold text-yellow-600">
                {stats.db_size_mb.toFixed(2)} MB
              </div>
            </div>
            <div className="bg-pink-50 rounded p-4">
              <div className="text-sm text-gray-600 mb-1">最后采集</div>
              <div className="text-lg font-bold text-pink-600">
                {stats.last_capture_ts ? formatTimestamp(stats.last_capture_ts) : '无'}
              </div>
            </div>
          </div>
        </section>
      )}

      {/* 后台操作 */}
      <section className="bg-white rounded-lg shadow-sm p-6 mb-6">
        <h2 className="text-xl font-semibold text-gray-800 mb-4">后台操作</h2>
        <div className="flex items-center gap-4 flex-wrap">
          <button
            onClick={handleClearExtractionQueue}
            disabled={clearingQueue}
            className="px-4 py-2 bg-orange-500 text-white rounded hover:bg-orange-600 disabled:bg-gray-300 text-sm"
          >
            {clearingQueue ? '清空中...' : '清空提炼队列'}
          </button>
          {clearQueueResult && (
            <span className="text-sm text-gray-600">{clearQueueResult}</span>
          )}
        </div>
        <p className="text-xs text-gray-400 mt-2">跳过当前积压的待提炼内容，让新的记录优先处理。</p>
      </section>

      {/* 关键排查日志 */}
      <section className="bg-white rounded-lg shadow-sm p-6 mb-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-800">关键排查日志</h2>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <select
              value={selectedLogKey}
              onChange={(e) => void loadLogContent(e.target.value)}
              disabled={logFiles.length === 0 || logLoading}
              className="text-sm border rounded px-3 py-2 min-w-[220px]"
            >
              {logFiles.length === 0 ? (
                <option value="">暂无关键日志</option>
              ) : (
                logFiles.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}{item.exists ? '' : '（文件不存在）'}
                  </option>
                ))
              )}
            </select>

            <button
              onClick={() => void refreshLogs()}
              disabled={logLoading}
              className="px-4 py-2 bg-slate-700 text-white rounded hover:bg-slate-800 disabled:bg-gray-300 text-sm"
            >
              {logLoading ? '刷新中...' : '刷新日志'}
            </button>
          </div>
        </div>

        {selectedLogMeta && (
          <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-gray-500 mb-3">
            <span>文件: {selectedLogMeta.label}</span>
            <span>存在: {selectedLogMeta.exists ? '是' : '否'}</span>
            <span>大小: {formatBytes(selectedLogMeta.size_bytes)}</span>
            <span>更新时间: {formatTimestamp(selectedLogMeta.modified_at)}</span>
            {selectedLogContent && (
              <span>本次返回: {formatBytes(selectedLogContent.returned_bytes)}</span>
            )}
          </div>
        )}

        {logError && (
          <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            读取日志失败：{logError}
          </div>
        )}

        {!selectedLogMeta && (
          <div className="text-sm text-gray-500 py-8 text-center">暂无可查看的关键日志。</div>
        )}

        {selectedLogMeta && !selectedLogMeta.exists && !logError && (
          <div className="text-sm text-gray-500 py-8 text-center">
            当前日志文件尚未生成：{selectedLogMeta.label}
          </div>
        )}

        {selectedLogContent && !logError && (
          <div>
            {selectedLogContent.truncated && (
              <div className="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                当前仅显示最新 {formatBytes(selectedLogContent.returned_bytes)}，完整文件大小 {formatBytes(selectedLogContent.total_size_bytes)}。
              </div>
            )}
            <pre className="bg-slate-950 text-slate-100 rounded p-4 text-xs overflow-auto max-h-[480px] whitespace-pre-wrap break-words">
              {selectedLogContent.content || '日志为空'}
            </pre>
          </div>
        )}

        {selectedLogMeta?.exists && !selectedLogContent && !logLoading && !logError && (
          <div className="text-sm text-gray-500 py-8 text-center">日志暂无内容。</div>
        )}
      </section>

      {/* 实时采集记录 */}
      <section className="bg-white rounded-lg shadow-sm p-6 mb-6">
        <div className="flex items-center gap-2 mb-4">
          {/* 相机图标 - camera */}
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-gray-700"
          >
            <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z" />
            <circle cx="12" cy="13" r="3" />
          </svg>
          <h2 className="text-xl font-semibold text-gray-800">最新采集记录（最近20条）</h2>
        </div>

        <div className="overflow-x-auto">
          {captures.length === 0 ? (
            <div className="text-center text-gray-500 py-8">暂无采集记录</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-100">
                <tr>
                  <th className="px-3 py-2 text-left">ID</th>
                  <th className="px-3 py-2 text-left">时间</th>
                  <th className="px-3 py-2 text-left">应用</th>
                  <th className="px-3 py-2 text-left">窗口标题</th>
                  <th className="px-3 py-2 text-center">AX文本</th>
                  <th className="px-3 py-2 text-center">OCR</th>
                  <th className="px-3 py-2 text-center">输入</th>
                  <th className="px-3 py-2 text-center">向量化</th>
                </tr>
              </thead>
              <tbody>
                {captures.map((cap) => {
                  const vs = getVectorStatusForCapture(cap.id)
                  const isHighlighted = cap.id === highlightCaptureId

                  return (
                    <tr
                      key={cap.id}
                      className={`border-b hover:bg-gray-50 cursor-pointer ${
                        isHighlighted ? 'bg-yellow-100' : ''
                      }`}
                      onClick={() =>
                        setSelectedCapture(selectedCapture?.id === cap.id ? null : cap)
                      }
                    >
                      <td className="px-3 py-2">{cap.id}</td>
                      <td className="px-3 py-2">{formatTimestamp(cap.ts)}</td>
                      <td className="px-3 py-2">{cap.app_name || '上下文缺失'}</td>
                      <td className="px-3 py-2 max-w-xs truncate">{cap.win_title || '无窗口标题'}</td>
                      <td className="px-3 py-2 text-center">
                        {cap.ax_text ? `✓ ${cap.ax_text.length}字` : '-'}
                      </td>
                      <td className="px-3 py-2 text-center">
                        {cap.ocr_text ? `✓ ${cap.ocr_text.length}字` : '-'}
                      </td>
                      <td className="px-3 py-2 text-center">
                        {cap.input_text ? `✓ ${cap.input_text.length}字` : '-'}
                      </td>
                      <td className="px-3 py-2 text-center">
                        {vs?.vectorized ? (
                          <span className="text-green-600 font-semibold">✓</span>
                        ) : (
                          <span className="text-gray-400">-</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* 详情展开 */}
        {selectedCapture && (
          <div className="mt-4 p-4 bg-gray-50 rounded border">
            <h3 className="font-semibold mb-2">采集详情 (ID: {selectedCapture.id})</h3>
            <div className="space-y-2 text-sm">
              {selectedCapture.ax_text && (
                <div>
                  <strong>AX 文本 (字符数: {selectedCapture.ax_text.length}):</strong>
                  <pre className="mt-1 p-2 bg-white rounded text-xs overflow-auto max-h-40">
                    {selectedCapture.ax_text}
                  </pre>
                </div>
              )}
              {selectedCapture.ocr_text && (
                <div>
                  <strong>OCR 文本 (字符数: {selectedCapture.ocr_text.length}):</strong>
                  <pre className="mt-1 p-2 bg-white rounded text-xs overflow-auto max-h-40">
                    {selectedCapture.ocr_text}
                  </pre>
                </div>
              )}
              {selectedCapture.input_text && (
                <div>
                  <strong>用户输入 (字符数: {selectedCapture.input_text.length}):</strong>
                  <pre className="mt-1 p-2 bg-white rounded text-xs overflow-auto max-h-40">
                    {selectedCapture.input_text}
                  </pre>
                </div>
              )}
            </div>
          </div>
        )}
      </section>
      {confirmDialog}
    </div>
  )
}

export default DebugPanel
