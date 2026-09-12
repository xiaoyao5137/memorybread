/**
 * Settings v2 — 设置页（优化版）
 *
 * 改进：
 * 1. 使用卡片式布局，增加视觉层级
 * 2. 使用 SVG 图标替代 Emoji
 * 3. 优化表单样式和间距
 * 4. 添加图标和描述文字
 */

import React, { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '../store/useAppStore'
import { useFetchPreferences, useUpdatePreference } from '../hooks/useApi'
import type { PreferenceRecord } from '../types'
import { getLocalServiceBaseUrl } from '../utils/localServices'

interface SettingsProps {
  className?: string
}

const Settings: React.FC<SettingsProps> = ({ className = '' }) => {
  const {
    apiBaseUrl,
    sidecarVersion,
    setApiBaseUrl,
    setWindowMode,
  } = useAppStore()

  const [preferences, setPreferences] = useState<PreferenceRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [apiUrlInput, setApiUrlInput] = useState(apiBaseUrl)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)

  const fetchPrefs = useFetchPreferences()
  const updatePref = useUpdatePreference()

  useEffect(() => {
    setLoading(true)
    fetchPrefs()
      .then(setPreferences)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [fetchPrefs])

  const handleSaveApiUrl = useCallback(() => {
    setApiBaseUrl(apiUrlInput.trim())
    setSaveMsg('API 地址已更新')
    setTimeout(() => setSaveMsg(null), 2000)
  }, [apiUrlInput, setApiBaseUrl])

  const handlePrefChange = useCallback(
    async (key: string, value: string) => {
      try {
        const updated = await updatePref(key, value)
        setPreferences((prev) =>
          prev.map((p) => (p.key === key ? { ...p, value: updated.value } : p))
        )
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [updatePref]
  )

  const handleClose = () => setWindowMode('buddy')

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
        {/* API 服务配置 */}
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
                placeholder={getLocalServiceBaseUrl('core')}
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
            {saveMsg && (
              <div className="settings-v2__success-msg" data-testid="save-msg">
                ✓ {saveMsg}
              </div>
            )}
          </div>
        </section>

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
            {preferences.slice(0, 10).map((pref) => (
              <div
                key={pref.key}
                className="settings-v2__pref-item"
                data-testid={`pref-row-${pref.key}`}
              >
                <label htmlFor={`pref-${pref.key}`} className="settings-v2__pref-label">
                  {pref.key}
                </label>
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
            ))}
          </div>
        </section>

        {/* 开发者工具 */}
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
              <h2 className="settings-v2__card-title">开发者工具</h2>
              <p className="settings-v2__card-desc">
                查看实时采集记录、向量化状态和系统性能指标
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
        </section>

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

          <div className="settings-v2__version-list">
            <div className="settings-v2__version-item" data-testid="sidecar-version">
              <span className="settings-v2__version-label">AI Sidecar</span>
              <span className="settings-v2__version-value">{sidecarVersion}</span>
            </div>
            <div className="settings-v2__version-item" data-testid="app-version">
              <span className="settings-v2__version-label">Desktop UI</span>
              <span className="settings-v2__version-value">0.1.0</span>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

export default Settings
