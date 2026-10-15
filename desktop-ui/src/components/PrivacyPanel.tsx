import React, { useEffect, useState } from 'react'
import type { AppBlacklistRecord, PrivacyFilterRecord } from '../types'
import { toUserFacingError } from '../utils/userFacingError'
import { BreadAppIcon, BreadToolIcon } from './icons/BreadIcons'
import './PrivacyPanel.css'
import { getLocalServiceBaseUrl } from '../utils/localServices'

const API_BASE = getLocalServiceBaseUrl('core')

const FILTER_DESCRIPTIONS: Record<string, string> = {
  chat: '检测密码、验证码、账号等聊天敏感内容',
  pii: '检测身份证、银行卡、手机号、邮箱等身份信息',
  policy: '检测涉密、机密、内部文件等政策敏感信息',
  other: '其它兜底性质的敏感内容过滤'
}

const PrivacyPanel: React.FC = () => {
  const [blacklist, setBlacklist] = useState<AppBlacklistRecord[]>([])
  const [filters, setFilters] = useState<PrivacyFilterRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [newApp, setNewApp] = useState({ bundle_id: '', app_name: '', reason: '' })
  const [addError, setAddError] = useState<string | null>(null)
  const [addSuccess, setAddSuccess] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async (): Promise<boolean> => {
    try {
      const [blacklistRes, filtersRes] = await Promise.all([
        fetch(`${API_BASE}/api/privacy/blacklist`),
        fetch(`${API_BASE}/api/privacy/filters`)
      ])

      if (!blacklistRes.ok || !filtersRes.ok) {
        throw new Error('API 请求失败')
      }

      const blacklistData = await blacklistRes.json()
      const filtersData = await filtersRes.json()

      setBlacklist(blacklistData.data || [])
      setFilters(filtersData.data || [])
      setError(null)
      return true
    } catch (err) {
      console.error('加载隐私设置失败:', err)
      setError(toUserFacingError(err, '隐私设置加载失败'))
      return false
    } finally {
      setLoading(false)
    }
  }

  const toggleBlacklist = async (id: number, enabled: boolean) => {
    try {
      const res = await fetch(`${API_BASE}/api/privacy/blacklist/${id}/enabled`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      })
      if (!res.ok) throw new Error('更新失败')
      setBlacklist(prev => prev.map(item =>
        item.id === id ? { ...item, enabled } : item
      ))
    } catch (err) {
      console.error('更新黑名单失败:', err)
      alert('更新失败')
    }
  }

  const deleteBlacklist = async (id: number) => {
    if (!confirm('确定删除此应用？')) return
    try {
      const res = await fetch(`${API_BASE}/api/privacy/blacklist/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('删除失败')
      setBlacklist(prev => prev.filter(item => item.id !== id))
    } catch (err) {
      console.error('删除黑名单失败:', err)
      alert('删除失败')
    }
  }

  const addBlacklist = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (adding) return

    const appName = newApp.app_name.trim()
    const bundleId = newApp.bundle_id.trim()
    const reason = newApp.reason.trim()
    setAddSuccess(null)
    if (!appName) {
      setAddError('请填写应用名称')
      return
    }

    setAdding(true)
    setAddError(null)
    try {
      const res = await fetch(`${API_BASE}/api/privacy/blacklist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          app_name: appName,
          ...(bundleId ? { bundle_id: bundleId } : {}),
          ...(reason ? { reason } : {})
        })
      })
      if (!res.ok) {
        const result = await res.json().catch(() => null)
        throw new Error(typeof result?.message === 'string' ? result.message : '添加失败')
      }
      setNewApp({ bundle_id: '', app_name: '', reason: '' })
      if (await loadData()) setAddSuccess(`已添加“${appName}”`)
    } catch (err) {
      console.error('添加黑名单失败:', err)
      setAddError(toUserFacingError(err, '添加失败，请稍后重试'))
    } finally {
      setAdding(false)
    }
  }

  const toggleFilter = async (filterType: string, enabled: boolean) => {
    try {
      const res = await fetch(`${API_BASE}/api/privacy/filters/${filterType}/enabled`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      })
      if (!res.ok) throw new Error('更新失败')
      setFilters(prev => prev.map(item =>
        item.filter_type === filterType ? { ...item, enabled } : item
      ))
    } catch (err) {
      console.error('更新过滤规则失败:', err)
      alert('更新失败')
    }
  }

  if (loading) {
    return (
      <div className="privacy-panel privacy-panel--state" role="status">
        <div className="privacy-state-card">
          <span className="privacy-state-dot" aria-hidden="true" />
          <span>正在加载隐私设置…</span>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="privacy-panel privacy-panel--state">
        <div className="privacy-state-card privacy-state-card--error" role="alert">
          <BreadToolIcon name="retry" size={22} aria-hidden="true" />
          <div>
            <strong>隐私设置加载失败</strong>
            <p>{error}</p>
          </div>
          <button type="button" className="privacy-retry-btn" onClick={loadData}>重新加载</button>
        </div>
      </div>
    )
  }

  return (
    <div className="privacy-panel">
      <div className="privacy-header">
        <div className="privacy-title-group">
          <span className="privacy-title-icon" aria-hidden="true">
            <BreadAppIcon name="privacy" size={24} />
          </span>
          <div>
            <h1>隐私设置</h1>
            <p>管理应用黑名单和敏感内容过滤规则</p>
          </div>
        </div>
        <div className="privacy-notice">
          <BreadAppIcon className="privacy-notice-icon" name="privacy" size={22} aria-hidden="true" />
          <span className="privacy-notice-text">
            拦截的内容会直接丢弃，不会存档，只统计条数。本地和云端都不会有记录，可以绝对放心使用。
          </span>
        </div>
      </div>

      <div className="privacy-grid">
        <section className="privacy-section" aria-labelledby="privacy-filter-heading">
          <div className="privacy-card">
            <div className="privacy-card-header">
              <div>
                <h2 id="privacy-filter-heading">敏感内容过滤</h2>
                <p>自动检测并抹除敏感信息，保留其他内容</p>
              </div>
              <span className="privacy-count">{filters.length} 项规则</span>
            </div>
            <div className="privacy-card-content">
              {filters.map(filter => (
                <div key={filter.id} className="privacy-list-item">
                  <div className="privacy-list-info">
                    <div className="privacy-item-name">{filter.filter_name}</div>
                    <div className="privacy-item-description">
                      {FILTER_DESCRIPTIONS[filter.filter_type] || '使用配置规则检测并过滤敏感内容'}
                      <span className="privacy-stat"> · 本周已拦截 {filter.week_blocked ?? 0} 条</span>
                    </div>
                  </div>
                  <label className="privacy-switch">
                    <span className="privacy-switch-label">{filter.enabled ? '已开启' : '已关闭'}</span>
                    <input
                      type="checkbox"
                      checked={filter.enabled}
                      aria-label={`${filter.filter_name}${filter.enabled ? '已开启' : '已关闭'}`}
                      onChange={(e) => toggleFilter(filter.filter_type, e.target.checked)}
                    />
                    <span className="privacy-slider" aria-hidden="true" />
                  </label>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="privacy-section" aria-labelledby="privacy-blacklist-heading">
          <div className="privacy-card">
            <div className="privacy-card-header">
              <div>
                <h2 id="privacy-blacklist-heading">应用黑名单</h2>
                <p>这些应用的窗口内容将不会被采集</p>
              </div>
              <span className="privacy-count">{blacklist.length} 个应用</span>
            </div>
            <div className="privacy-card-content">
              <div className="privacy-blacklist-list">
                {blacklist.map(item => (
                  <div key={item.id} className="privacy-list-item">
                    <div className="privacy-list-info">
                      <div className="privacy-item-name">{item.app_name}</div>
                      <div className="privacy-item-description">
                        {item.bundle_id?.trim() || '按应用名称匹配'}
                        <span className="privacy-stat"> · 本周已拦截 {item.week_blocked ?? 0} 次</span>
                      </div>
                      {item.reason && <div className="privacy-item-reason">{item.reason}</div>}
                    </div>
                    <div className="privacy-list-actions">
                      <label className="privacy-switch">
                        <span className="privacy-switch-label">{item.enabled ? '已开启' : '已关闭'}</span>
                        <input
                          type="checkbox"
                          checked={item.enabled}
                          aria-label={`${item.app_name}${item.enabled ? '已开启' : '已关闭'}`}
                          onChange={(e) => toggleBlacklist(item.id, e.target.checked)}
                        />
                        <span className="privacy-slider" aria-hidden="true" />
                      </label>
                      <button
                        type="button"
                        className="privacy-delete-btn"
                        onClick={() => deleteBlacklist(item.id)}
                        title={`删除 ${item.app_name}`}
                        aria-label={`删除 ${item.app_name}`}
                      >
                        <BreadToolIcon name="clear" size={16} aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>

              <div className="privacy-add-form">
                <div className="privacy-form-heading">
                  <h3>添加新应用</h3>
                  <p>填写应用名称即可按名称排除采集；Bundle ID 选填，可辅助精确匹配。</p>
                </div>
                <form className="privacy-form-grid" onSubmit={addBlacklist} noValidate>
                  <label className="privacy-form-field">
                    <span>Bundle ID <small>选填</small></span>
                    <input
                      type="text"
                      placeholder="如 com.tencent.xinWeChat"
                      value={newApp.bundle_id}
                      onChange={(e) => {
                        setNewApp({ ...newApp, bundle_id: e.target.value })
                        setAddError(null)
                      }}
                    />
                  </label>
                  <label className="privacy-form-field">
                    <span>应用名称 <small>必填</small></span>
                    <input
                      type="text"
                      placeholder="如 微信"
                      value={newApp.app_name}
                      required
                      aria-invalid={Boolean(addError && !newApp.app_name.trim())}
                      onChange={(e) => {
                        setNewApp({ ...newApp, app_name: e.target.value })
                        setAddError(null)
                      }}
                    />
                  </label>
                  <label className="privacy-form-field">
                    <span>排除原因 <small>选填</small></span>
                    <input
                      type="text"
                      placeholder="如 包含私密聊天内容"
                      value={newApp.reason}
                      onChange={(e) => {
                        setNewApp({ ...newApp, reason: e.target.value })
                        setAddError(null)
                      }}
                    />
                  </label>
                  <button type="submit" className="privacy-add-btn" disabled={adding}>
                    {adding ? '正在添加…' : '添加应用'}
                  </button>
                  {addError && <p className="privacy-form-feedback privacy-form-feedback--error" role="alert">{addError}</p>}
                  {addSuccess && <p className="privacy-form-feedback privacy-form-feedback--success" role="status">{addSuccess}</p>}
                </form>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

export default PrivacyPanel
