/**
 * Settings v2 — 设置页（优化版）
 *
 * 改进：
 * 1. 使用卡片式布局，增加视觉层级
 * 2. 使用 SVG 图标替代 Emoji
 * 3. 优化表单样式和间距
 * 4. 添加图标和描述文字
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useAppStore } from '../store/useAppStore'
import {
  useFetchConfigChecks,
  useFetchPreferences,
  useRunCaptureCleanup,
  useRunConfigCheckAction,
  useRunScreenshotCleanup,
  useUpdatePreference,
} from '../hooks/useApi'
import type { ConfigCheckItem, ConfigCheckStatus, PreferenceRecord } from '../types'
import { toUserFacingError } from '../utils/userFacingError'
import { useAppMetadata } from '../utils/appMetadata'
import { fetchSoftwareUpdate, requestSoftwareUpdate, type SoftwareUpdateCheck } from '../utils/softwareUpdate'
import { useImeCompositionGuard } from '../hooks/useImeCompositionGuard'
import InteractionSettings from './InteractionSettings'
import './Settings.v2.css'

interface SettingsProps {
  className?: string
}

const USER_VISIBLE_PREFERENCE_KEYS = new Set<string>([
  'privacy.capture_interval_sec',
  'privacy.screenshot_keep_days',
  'privacy.capture_retention_days',
])

const Settings: React.FC<SettingsProps> = ({ className = '' }) => {
  const appMetadata = useAppMetadata()
  const identityImeGuard = useImeCompositionGuard<HTMLInputElement>()
  const CAPTURE_INTERVAL_KEY = 'privacy.capture_interval_sec'
  const SCREENSHOT_KEEP_DAYS_KEY = 'privacy.screenshot_keep_days'
  const CAPTURE_RETENTION_DAYS_KEY = 'privacy.capture_retention_days'
  const ENERGY_SAVING_MODE_KEY = 'performance.energy_saving_mode'
  const USER_IDENTITY_KEY = 'user.identity_keywords'
  const DEFAULT_API_BASE = 'http://localhost:7070'

  const {
    apiBaseUrl,
    adminApiBaseUrl,
    gatewayApiBaseUrl,
    debugModeEnabled,
    serviceEnvironment,
    currentUser,
    setApiBaseUrl,
    setDebugModeEnabled,
    setServiceEnvironment,
    setWindowMode,
  } = useAppStore()

  const canConfigureLocalService =
    currentUser?.feature_flags?.includes('local_service_settings') ?? false

  const [preferences, setPreferences] = useState<PreferenceRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [apiUrlInput, setApiUrlInput] = useState(apiBaseUrl)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [identityInput, setIdentityInput] = useState('')
  const [identitySaved, setIdentitySaved] = useState(false)
  const [cleanupRunning, setCleanupRunning] = useState(false)
  const [captureCleanupRunning, setCaptureCleanupRunning] = useState(false)
  const [configChecks, setConfigChecks] = useState<ConfigCheckItem[]>([])
  const [configChecksLoading, setConfigChecksLoading] = useState(false)
  const [configActionRunning, setConfigActionRunning] = useState<string | null>(null)
  const [softwareUpdate, setSoftwareUpdate] = useState<SoftwareUpdateCheck | null>(null)
  const [softwareUpdateChecking, setSoftwareUpdateChecking] = useState(false)

  const fetchPrefs = useFetchPreferences()
  const fetchConfigChecks = useFetchConfigChecks()
  const runConfigCheckAction = useRunConfigCheckAction()
  const updatePref = useUpdatePreference()
  const runScreenshotCleanup = useRunScreenshotCleanup()
  const runCaptureCleanup = useRunCaptureCleanup()

  const sortedPreferences = useMemo(() => {
    return preferences
      .filter((pref) => USER_VISIBLE_PREFERENCE_KEYS.has(pref.key))
      .sort((a, b) => {
        if (a.key === CAPTURE_INTERVAL_KEY) return -1
        if (b.key === CAPTURE_INTERVAL_KEY) return 1
        if (a.key === SCREENSHOT_KEEP_DAYS_KEY) return -1
        if (b.key === SCREENSHOT_KEEP_DAYS_KEY) return 1
        if (a.key === CAPTURE_RETENTION_DAYS_KEY) return -1
        if (b.key === CAPTURE_RETENTION_DAYS_KEY) return 1
        return a.key.localeCompare(b.key)
      })
  }, [
    preferences,
    CAPTURE_INTERVAL_KEY,
    SCREENSHOT_KEEP_DAYS_KEY,
    CAPTURE_RETENTION_DAYS_KEY,
  ])

  const energySavingEnabled = useMemo(() => {
    const pref = preferences.find((item) => item.key === ENERGY_SAVING_MODE_KEY)
    return !pref || pref.value.trim().toLowerCase() !== 'false'
  }, [preferences, ENERGY_SAVING_MODE_KEY])

  useEffect(() => {
    setLoading(true)
    fetchPrefs()
      .then((prefs) => {
        setPreferences(prefs)
        const identityPref = prefs.find((p) => p.key === USER_IDENTITY_KEY)
        if (identityPref) setIdentityInput(identityPref.value)
      })
      .catch((cause) => setError(toUserFacingError(cause, '设置读取失败')))
      .finally(() => setLoading(false))
  }, [fetchPrefs])

  const refreshConfigChecks = useCallback(async () => {
    setConfigChecksLoading(true)
    setError(null)
    try {
      const items = await fetchConfigChecks()
      setConfigChecks(items)
    } catch (e) {
      setError(toUserFacingError(e, '运行状态读取失败'))
    } finally {
      setConfigChecksLoading(false)
    }
  }, [fetchConfigChecks])

  useEffect(() => {
    void refreshConfigChecks()
  }, [refreshConfigChecks])

  const handleCheckSoftwareUpdate = useCallback(async () => {
    setSoftwareUpdateChecking(true)
    setError(null)
    try {
      setSoftwareUpdate(await fetchSoftwareUpdate(adminApiBaseUrl, appMetadata))
    } catch (cause) {
      setError(toUserFacingError(cause, '软件更新检查失败'))
    } finally {
      setSoftwareUpdateChecking(false)
    }
  }, [adminApiBaseUrl, appMetadata])

  const handleSaveApiUrl = useCallback(() => {
    const trimmed = apiUrlInput.trim()

    if (!trimmed) {
      setApiBaseUrl(DEFAULT_API_BASE)
      setError(null)
      setApiUrlInput(DEFAULT_API_BASE)
      setSaveMsg('API 地址已恢复默认值')
      setTimeout(() => setSaveMsg(null), 2000)
      return
    }

    try {
      const url = new URL(trimmed)
      if (url.protocol !== 'http:' && url.protocol !== 'https:') {
        throw new Error('仅支持 http/https 地址')
      }
      setApiBaseUrl(url.toString().replace(/\/$/, ''))
      setError(null)
      setSaveMsg('API 地址已更新')
      setTimeout(() => setSaveMsg(null), 2000)
    } catch {
      setError('API 地址格式无效，请输入完整的 http:// 或 https:// 地址')
      setSaveMsg(null)
    }
  }, [DEFAULT_API_BASE, apiUrlInput, setApiBaseUrl])

  const handlePrefChange = useCallback(
    async (key: string, value: string) => {
      try {
        const updated = await updatePref(key, value)
        setPreferences((prev) => {
          if (prev.some((item) => item.key === key)) {
            return prev.map((item) =>
              item.key === key ? { ...item, value: updated.value } : item
            )
          }
          return [
            ...prev,
            {
              id: updated.id ?? 0,
              key,
              value: updated.value,
              source: updated.source ?? 'user',
              confidence: updated.confidence ?? 1,
              updated_at: updated.updated_at,
            },
          ]
        })
        if (key === CAPTURE_INTERVAL_KEY) {
          setSaveMsg('文字识别与截图频率已保存，需重启应用后生效')
          setTimeout(() => setSaveMsg(null), 3000)
        } else if (key === SCREENSHOT_KEEP_DAYS_KEY) {
          setSaveMsg('图片过期时间已保存，将在下一次后台清理时生效')
          setTimeout(() => setSaveMsg(null), 3000)
        } else if (key === CAPTURE_RETENTION_DAYS_KEY) {
          setSaveMsg('采集记录过期时间已保存，将在下一次后台清理时生效')
          setTimeout(() => setSaveMsg(null), 3000)
        } else if (key === ENERGY_SAVING_MODE_KEY) {
          setSaveMsg(value === 'true' ? '节能模式已开启' : '节能模式已关闭')
          setTimeout(() => setSaveMsg(null), 3000)
        }
      } catch (e) {
        setError(toUserFacingError(e, '设置保存失败'))
      }
    },
    [
      CAPTURE_INTERVAL_KEY,
      SCREENSHOT_KEEP_DAYS_KEY,
      CAPTURE_RETENTION_DAYS_KEY,
      ENERGY_SAVING_MODE_KEY,
      updatePref,
    ]
  )

  const handleRunScreenshotCleanup = useCallback(async () => {
    setCleanupRunning(true)
    setError(null)
    try {
      const result = await runScreenshotCleanup()
      const freedMb = Math.round(result.freed_bytes / 1024 / 1024)
      setSaveMsg(`截图清理完成：删除 ${result.deleted_count} 个文件，释放约 ${freedMb} MB（保留 ${result.keep_days} 天）`)
      setTimeout(() => setSaveMsg(null), 5000)
      const prefs = await fetchPrefs()
      setPreferences(prefs)
    } catch (e) {
      setError(toUserFacingError(e, '截图清理失败'))
    } finally {
      setCleanupRunning(false)
    }
  }, [fetchPrefs, runScreenshotCleanup])

  const handleRunCaptureCleanup = useCallback(async () => {
    setCaptureCleanupRunning(true)
    setError(null)
    try {
      const result = await runCaptureCleanup()
      const freedMb = Math.round(result.freed_bytes / 1024 / 1024)
      setSaveMsg(`采集记录清理完成：删除 ${result.deleted_count} 条记录、${result.deleted_screenshot_count} 个截图，释放约 ${freedMb} MB（保留 ${result.retention_days} 天）`)
      setTimeout(() => setSaveMsg(null), 5000)
      const prefs = await fetchPrefs()
      setPreferences(prefs)
    } catch (e) {
      setError(toUserFacingError(e, '采集记录清理失败'))
    } finally {
      setCaptureCleanupRunning(false)
    }
  }, [fetchPrefs, runCaptureCleanup])

  const handleConfigCheckAction = useCallback(async (
    id: string,
    action: 'verify' | 'install' | 'delete',
  ) => {
    const actionKey = `${id}:${action}`
    setConfigActionRunning(actionKey)
    setError(null)
    try {
      const result = await runConfigCheckAction(id, action)
      setSaveMsg(toUserFacingError(result.message, '操作已完成'))
      setTimeout(() => setSaveMsg(null), 5000)
      await refreshConfigChecks()
    } catch (e) {
      setError(toUserFacingError(e, '操作失败，请稍后重试'))
    } finally {
      setConfigActionRunning(null)
    }
  }, [refreshConfigChecks, runConfigCheckAction])

  const handleClose = () => setWindowMode('buddy')

  const handleSaveIdentity = useCallback(async () => {
    const val = identityInput.trim()
    if (!val) return
    try {
      await updatePref(USER_IDENTITY_KEY, val)
      setIdentitySaved(true)
      setTimeout(() => setIdentitySaved(false), 2000)
    } catch (e) {
      setError(toUserFacingError(e, '身份信息保存失败'))
    }
  }, [identityInput, updatePref, USER_IDENTITY_KEY])

  const statusLabel = (status: ConfigCheckStatus) => {
    switch (status) {
      case 'ok':
        return '可用'
      case 'warning':
        return '需确认'
      case 'failed':
        return '不可用'
      case 'unsupported':
        return '不支持'
      default:
        return status
    }
  }

  return (
    <div className={`settings-v2 ${className}`} data-testid="settings-page">
      {/* 标题栏 */}
      <div className="settings-v2__header">
        <div className="settings-v2__title-group">
          {/* 设置图标 */}
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
          <h1 className="settings-v2__title">设置</h1>
        </div>

        <button
          className="settings-v2__close-btn"
          data-testid="settings-close"
          onClick={handleClose}
          type="button"
          aria-label="关闭设置"
        >
          {/* X 图标 */}
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
            <path d="M18 6 6 18" />
            <path d="m6 6 12 12" />
          </svg>
        </button>
      </div>

      <div className="settings-v2__content">
        {saveMsg && (
          <div className="settings-v2__success-msg" data-testid="save-msg">
            ✓ {saveMsg}
          </div>
        )}

        {/* 我是谁 */}
        <section className="settings-v2__card settings-v2__card--identity" data-testid="settings-identity-section">
          <div className="settings-v2__card-header">
            <div className="settings-v2__card-icon settings-v2__card-icon--green">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="8" r="4" />
                <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
              </svg>
            </div>
            <div>
              <h2 className="settings-v2__card-title">我是谁</h2>
              <p className="settings-v2__card-desc">告诉记忆面包你的身份，让它准确识别哪些内容是你自己的工作产出</p>
            </div>
          </div>

          <div className="settings-v2__form-group">
            <label htmlFor="identity-input" className="settings-v2__label">
              你的名字 / 昵称 / 网名
            </label>
            <p className="settings-v2__pref-help">
              多个名称用逗号分隔，例如：张三,zhangsan,老张。记忆面包会用这些信息区分屏幕上"你做的事"和"别人做的事"，避免把无关内容写入你的工作记录。
            </p>
            <div className="settings-v2__input-group">
              <input
                id="identity-input"
                data-testid="identity-input"
                type="text"
                className="settings-v2__input"
                value={identityInput}
                onChange={(e) => setIdentityInput(e.target.value)}
                placeholder="输入你的名字或昵称，多个用逗号分隔"
                onCompositionStart={identityImeGuard.onCompositionStart}
                onCompositionEnd={identityImeGuard.onCompositionEnd}
                onBlur={identityImeGuard.onBlur}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !identityImeGuard.isImeEvent(e)) {
                    e.preventDefault()
                    void handleSaveIdentity()
                  }
                }}
              />
              <button
                data-testid="identity-save"
                onClick={handleSaveIdentity}
                type="button"
                className="settings-v2__btn settings-v2__btn--primary"
                disabled={!identityInput.trim()}
              >
                保存
              </button>
            </div>
            {identitySaved && (
              <div className="settings-v2__success-msg">✓ 身份信息已保存</div>
            )}
            {!identityInput.trim() && !loading && (
              <div className="settings-v2__identity-hint">
                ⚠️ 尚未设置身份信息，建议在使用前先完成设置
              </div>
            )}
          </div>
        </section>

        <InteractionSettings />

        {/* 配置检测 */}
        <section className="settings-v2__card" data-testid="settings-config-checks-section">
          <div className="settings-v2__card-header">
            <div className="settings-v2__card-icon settings-v2__card-icon--blue">
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
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </div>
            <div>
              <h2 className="settings-v2__card-title">配置检测</h2>
              <p className="settings-v2__card-desc">检查采集、浏览器文本提取和 OCR 兜底依赖的本机配置</p>
            </div>
          </div>

          <div className="settings-v2__config-toolbar">
            <button
              type="button"
              className="settings-v2__btn settings-v2__btn--secondary"
              onClick={() => void refreshConfigChecks()}
              disabled={configChecksLoading}
            >
              {configChecksLoading ? '检测中...' : '全部验证'}
            </button>
          </div>

          <div className="settings-v2__check-list">
            {configChecks.map((item) => (
              <div className="settings-v2__check-item" key={item.id} data-testid={`config-check-${item.id}`}>
                <div className="settings-v2__check-main">
                  <div className="settings-v2__check-title-row">
                    <span className="settings-v2__check-name">{item.name}</span>
                    <span className={`settings-v2__check-status settings-v2__check-status--${item.status}`}>
                      {statusLabel(item.status)}
                    </span>
                  </div>
                  <div className="settings-v2__check-desc">{item.description}</div>
                  <div className="settings-v2__check-message">{item.message}</div>
                  {item.details.length > 0 && (
                    <ul className="settings-v2__check-details">
                      {item.details.map((detail, index) => (
                        <li key={`${item.id}-${index}`}>{detail}</li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="settings-v2__check-actions">
                  <button
                    type="button"
                    className="settings-v2__btn settings-v2__btn--secondary"
                    onClick={() => void handleConfigCheckAction(item.id, 'verify')}
                    disabled={configActionRunning === `${item.id}:verify`}
                  >
                    验证
                  </button>
                  <button
                    type="button"
                    className="settings-v2__btn settings-v2__btn--secondary"
                    onClick={() => void handleConfigCheckAction(item.id, 'install')}
                    disabled={!item.can_install || configActionRunning === `${item.id}:install`}
                  >
                    配置
                  </button>
                  <button
                    type="button"
                    className="settings-v2__btn settings-v2__btn--secondary"
                    onClick={() => void handleConfigCheckAction(item.id, 'delete')}
                    disabled={!item.can_delete || configActionRunning === `${item.id}:delete`}
                  >
                    移除
                  </button>
                </div>
              </div>
            ))}
            {!configChecksLoading && configChecks.length === 0 && (
              <div className="settings-v2__loading">暂无检测项</div>
            )}
          </div>
        </section>

        {/* API 服务配置 */}
        {canConfigureLocalService && debugModeEnabled && (
          <section className="settings-v2__card" data-testid="settings-api-section">
            <div className="settings-v2__card-header">
              <div className="settings-v2__card-icon settings-v2__card-icon--blue">
                {/* server 图标 */}
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
                  <rect width="20" height="8" x="2" y="2" rx="2" ry="2" />
                  <rect width="20" height="8" x="2" y="14" rx="2" ry="2" />
                  <line x1="6" x2="6.01" y1="6" y2="6" />
                  <line x1="6" x2="6.01" y1="18" y2="18" />
                </svg>
              </div>
              <div>
                <h2 className="settings-v2__card-title">本机服务</h2>
                <p className="settings-v2__card-desc">配置记忆面包本机服务连接地址</p>
              </div>
            </div>

            <div className="settings-v2__form-group">
              <label htmlFor="api-url-input" className="settings-v2__label">
                服务地址
              </label>
              <div className="settings-v2__input-group">
                <input
                  id="api-url-input"
                  data-testid="api-url-input"
                  type="text"
                  className="settings-v2__input"
                  value={apiUrlInput}
                  onChange={(e) => setApiUrlInput(e.target.value)}
                  placeholder="http://localhost:7070"
                />
                <button
                  data-testid="api-url-save"
                  onClick={handleSaveApiUrl}
                  type="button"
                  className="settings-v2__btn settings-v2__btn--primary"
                >
                  保存
                </button>
              </div>
            </div>
          </section>
        )}

        {/* 个性化偏好 */}
        <section className="settings-v2__card" data-testid="settings-prefs-section">
          <div className="settings-v2__card-header">
            <div className="settings-v2__card-icon settings-v2__card-icon--purple">
              {/* sliders 图标 */}
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
                <line x1="4" x2="4" y1="21" y2="14" />
                <line x1="4" x2="4" y1="10" y2="3" />
                <line x1="12" x2="12" y1="21" y2="12" />
                <line x1="12" x2="12" y1="8" y2="3" />
                <line x1="20" x2="20" y1="21" y2="16" />
                <line x1="20" x2="20" y1="12" y2="3" />
                <line x1="2" x2="6" y1="14" y2="14" />
                <line x1="10" x2="14" y1="8" y2="8" />
                <line x1="18" x2="22" y1="16" y2="16" />
              </svg>
            </div>
            <div>
              <h2 className="settings-v2__card-title">个性化偏好</h2>
              <p className="settings-v2__card-desc">自定义应用行为和显示方式</p>
            </div>
          </div>

          {loading && (
            <div className="settings-v2__loading" data-testid="prefs-loading">
              加载中...
            </div>
          )}
          {error && (
            <div className="settings-v2__error" data-testid="prefs-error">
              ⚠️ {error}
            </div>
          )}

          <div className="settings-v2__pref-list">
            <label
              className="settings-v2__toggle-row settings-v2__toggle-row--standalone"
              htmlFor="energy-saving-mode-toggle"
            >
              <span>
                <strong>节能模式</strong>
                <small>
                  充电时保持最大提炼吞吐；使用电池时降低频率和并发；电量不高于 20%
                  时暂停后台时间线与 bake 提炼。事件与定时 capture 采集始终继续，积压会在充电后自动追赶。
                </small>
              </span>
              <input
                id="energy-saving-mode-toggle"
                data-testid="energy-saving-mode-toggle"
                type="checkbox"
                checked={energySavingEnabled}
                onChange={(event) => {
                  void handlePrefChange(
                    ENERGY_SAVING_MODE_KEY,
                    event.target.checked ? 'true' : 'false'
                  )
                }}
              />
            </label>

            {sortedPreferences.map((pref) => {
              const isCaptureInterval = pref.key === CAPTURE_INTERVAL_KEY
              const isScreenshotKeepDays = pref.key === SCREENSHOT_KEEP_DAYS_KEY
              const isCaptureRetentionDays = pref.key === CAPTURE_RETENTION_DAYS_KEY
              return (
                <div
                  key={pref.key}
                  className="settings-v2__pref-item"
                  data-testid={`pref-row-${pref.key}`}
                >
                  <label htmlFor={`pref-${pref.key}`} className="settings-v2__pref-label">
                    {isCaptureInterval
                      ? 'OCR / 截图频率（秒）'
                      : isScreenshotKeepDays
                        ? '图片过期时间（天）'
                        : isCaptureRetentionDays
                          ? '采集记录过期时间（天）'
                          : pref.key}
                  </label>
                  {isCaptureInterval && (
                    <p className="settings-v2__pref-help">
                      控制文字识别和截图采集频率。默认值会保持当前配置，修改后需重启应用生效。
                    </p>
                  )}
                  {isScreenshotKeepDays && (
                    <p className="settings-v2__pref-help">
                      控制图片缓存的过期时间。超过该天数的本地截图文件会自动删除，并清空对应 capture 记录中的截图路径。
                    </p>
                  )}
                  {isCaptureRetentionDays && (
                    <p className="settings-v2__pref-help">
                      控制原始采集记录的过期时间。超过该天数的采集记录会自动清空，时间线、知识、操作记录等提炼物会继续保留。
                    </p>
                  )}
                  {isScreenshotKeepDays && (
                    <div className="settings-v2__input-group" style={{ marginBottom: 8 }}>
                      <button
                        type="button"
                        className="settings-v2__btn settings-v2__btn--secondary"
                        onClick={handleRunScreenshotCleanup}
                        disabled={cleanupRunning}
                      >
                        {cleanupRunning ? '清理中...' : '立即清理一次'}
                      </button>
                    </div>
                  )}
                  {isCaptureRetentionDays && (
                    <div className="settings-v2__input-group" style={{ marginBottom: 8 }}>
                      <button
                        type="button"
                        className="settings-v2__btn settings-v2__btn--secondary"
                        onClick={handleRunCaptureCleanup}
                        disabled={captureCleanupRunning}
                      >
                        {captureCleanupRunning ? '清理中...' : '立即清理采集记录'}
                      </button>
                    </div>
                  )}
                  {!isCaptureInterval && !isScreenshotKeepDays && !isCaptureRetentionDays && (
                    <div className="settings-v2__pref-key">{pref.key}</div>
                  )}
                  <input
                    id={`pref-${pref.key}`}
                    type="text"
                    className="settings-v2__pref-input"
                    defaultValue={pref.value}
                    onBlur={(e) => {
                      if (e.target.value !== pref.value) {
                        handlePrefChange(pref.key, e.target.value)
                      }
                    }}
                  />
                </div>
              )
            })}
          </div>
        </section>

        {/* 开发者工具只在显式 Debug 启动时出现，普通用户不可自行开启。 */}
        {debugModeEnabled && (
          <section className="settings-v2__card" data-testid="settings-debug-section">
          <div className="settings-v2__card-header">
            <div className="settings-v2__card-icon settings-v2__card-icon--orange">
              {/* wrench 图标 */}
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
                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
              </svg>
            </div>
            <div>
              <h2 className="settings-v2__card-title">开发者模式</h2>
              <p className="settings-v2__card-desc">
                在固定的测试与正式服务环境间切换，或查看实时采集和处理状态
              </p>
            </div>
          </div>

          <button
            data-testid="open-debug-btn"
            onClick={() => setWindowMode('debug')}
            type="button"
            className="settings-v2__btn settings-v2__btn--secondary"
          >
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
              <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
            </svg>
            打开调试面板
          </button>

          <label className="settings-v2__toggle-row" htmlFor="debug-mode-toggle">
            <span>
              <strong>调试模式</strong>
              <small>Debug 启动默认使用本机测试环境；关闭后强制恢复阿里云正式环境。</small>
            </span>
            <input
              id="debug-mode-toggle"
              data-testid="debug-mode-toggle"
              type="checkbox"
              checked={debugModeEnabled}
              onChange={(event) => setDebugModeEnabled(event.target.checked)}
            />
          </label>

          {debugModeEnabled && (
            <>
              <div className="settings-v2__environment-row">
                <span className="settings-v2__environment-copy">
                  <strong>服务环境</strong>
                  <small>测试使用本机 18080/18090；正式使用 memorybread.work 域名。</small>
                </span>
                <div
                  className="settings-v2__environment-options"
                  role="group"
                  aria-label="选择服务环境"
                >
                  <button
                    type="button"
                    data-testid="service-environment-production"
                    className={`settings-v2__environment-option${serviceEnvironment === 'production' ? ' settings-v2__environment-option--active' : ''}`}
                    aria-pressed={serviceEnvironment === 'production'}
                    onClick={() => setServiceEnvironment('production')}
                  >
                    正式
                  </button>
                  <button
                    type="button"
                    data-testid="service-environment-staging"
                    className={`settings-v2__environment-option${serviceEnvironment === 'staging' ? ' settings-v2__environment-option--active' : ''}`}
                    aria-pressed={serviceEnvironment === 'staging'}
                    onClick={() => setServiceEnvironment('staging')}
                  >
                    测试
                  </button>
                </div>
              </div>
              <div className="settings-v2__debug-routes" aria-label="调试服务环境绑定">
                <span>Environment <strong>{serviceEnvironment}</strong></span>
                <span>Core <strong>{apiBaseUrl}</strong></span>
                <span>Account <strong>{adminApiBaseUrl}</strong></span>
                <span>Cloud Creation <strong>{gatewayApiBaseUrl}</strong></span>
              </div>
            </>
          )}
          </section>
        )}

        {/* 版本信息 */}
        <section className="settings-v2__card" data-testid="settings-version-section">
          <div className="settings-v2__card-header">
            <div className="settings-v2__card-icon settings-v2__card-icon--gray">
              {/* info 图标 */}
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
                <circle cx="12" cy="12" r="10" />
                <path d="M12 16v-4" />
                <path d="M12 8h.01" />
              </svg>
            </div>
            <div>
              <h2 className="settings-v2__card-title">版本信息</h2>
            </div>
          </div>

          <div className="settings-v2__version-foot">
            <div className="settings-v2__version-list">
            <div className="settings-v2__version-item" data-testid="app-version">
              <span className="settings-v2__version-label">当前版本</span>
              <span className="settings-v2__version-value">v{appMetadata.version}</span>
            </div>
            {softwareUpdate && (
              <div className="settings-v2__version-item">
                <span className="settings-v2__version-label">更新状态</span>
                <span className="settings-v2__version-value">
                  {softwareUpdate.update_available ? `v${softwareUpdate.latest_version} 可下载` : '已是最新版本'}
                </span>
              </div>
            )}
            </div>
            <div className="settings-v2__version-actions">
            <button className="settings-v2__btn settings-v2__btn--secondary" onClick={() => setWindowMode('about')} type="button">
              关于记忆面包
            </button>
            <button className="settings-v2__btn settings-v2__btn--secondary" disabled={softwareUpdateChecking} onClick={() => void handleCheckSoftwareUpdate()} type="button">
              {softwareUpdateChecking ? '检查中' : '检查更新'}
            </button>
            {softwareUpdate?.update_available && softwareUpdate.release && (
              <button className="settings-v2__btn settings-v2__btn--primary" onClick={() => requestSoftwareUpdate(softwareUpdate)} type="button">
                {appMetadata.update_supported ? '立即更新' : appMetadata.distribution === 'app_store' ? '前往 App Store' : '下载安装包'}
              </button>
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

export default Settings
