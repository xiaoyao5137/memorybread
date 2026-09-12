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

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
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
    } catch (err) {
      console.error('加载隐私设置失败:', err)
      setError(toUserFacingError(err, '隐私设置加载失败'))
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

  const addBlacklist = async () => {
    if (!newApp.bundle_id || !newApp.app_name) {
      alert('请填写 Bundle ID 和应用名称')
      return
    }
    try {
      const res = await fetch(`${API_BASE}/api/privacy/blacklist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newApp)
      })
      if (!res.ok) throw new Error('添加失败')
      setNewApp({ bundle_id: '', app_name: '', reason: '' })
      loadData()
    } catch (err) {
      console.error('添加黑名单失败:', err)
      alert('添加失败')
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
                        {item.bundle_id}
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
                  <p>填写应用标识后，该应用窗口会被立即排除在采集范围外。</p>
                </div>
                <div className="privacy-form-grid">
                  <label className="privacy-form-field">
                    <span>Bundle ID</span>
                    <input
                      type="text"
                      placeholder="如 com.tencent.xinWeChat"
                      value={newApp.bundle_id}
                      onChange={(e) => setNewApp({ ...newApp, bundle_id: e.target.value })}
                    />
                  </label>
                  <label className="privacy-form-field">
                    <span>应用名称</span>
                    <input
                      type="text"
                      placeholder="如 微信"
                      value={newApp.app_name}
                      onChange={(e) => setNewApp({ ...newApp, app_name: e.target.value })}
                    />
                  </label>
                  <label className="privacy-form-field">
                    <span>排除原因 <small>选填</small></span>
                    <input
                      type="text"
                      placeholder="如 包含私密聊天内容"
                      value={newApp.reason}
                      onChange={(e) => setNewApp({ ...newApp, reason: e.target.value })}
                    />
                  </label>
                  <button type="button" className="privacy-add-btn" onClick={addBlacklist}>
                    添加应用
                  </button>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

export default PrivacyPanel
