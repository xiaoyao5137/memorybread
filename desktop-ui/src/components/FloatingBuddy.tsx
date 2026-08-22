/**
 * FloatingBuddy v2 - 主窗口左侧导航
 *
 * 设计约束：
 * 1. 使用 SVG 图标替代 Emoji
 * 2. 修复 hover 时所有图标放大的问题
 * 3. 遵循设计规范
 * 4. 多级分组菜单：吃面包 / 烤面包 / 面包机
 * 5. 账号入口固定在侧栏底部，不使用悬浮卡片
 */

import React, { useState } from 'react'
import { ArrowDownToLine, ChevronLeft, ChevronRight, Loader2, RotateCw } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { type WindowMode } from '../types'
import { getRunModeLabel, getUserDisplayName } from '../utils/accountDisplay'
import type { SoftwareUpdateCheck } from '../utils/softwareUpdate'
import { softwareUpdateSessionBusy, useSoftwareUpdateSession } from '../utils/softwareUpdateSession'
import { BreadAppIcon, type BreadAppIconName } from './icons/BreadIcons'
import './FloatingBuddy.v2.css'

interface FloatingBuddyProps {
  className?: string
  softwareUpdate?: SoftwareUpdateCheck | null
  onSoftwareUpdateClick?: () => void
}

interface MenuItem {
  mode: WindowMode
  label: string
  testId: string
  icon: BreadAppIconName
}

interface MenuGroup {
  groupLabel: string
  items: MenuItem[]
}

const MENU_GROUPS: MenuGroup[] = [
  {
    groupLabel: '工作',
    items: [
      {
        mode: 'rag',
        label: '咨询',
        testId: 'buddy-avatar',
        icon: 'consult'
      },
      {
        mode: 'creation',
        label: '创作',
        testId: 'creation-btn',
        icon: 'creation'
      },
      {
        mode: 'tasks',
        label: '任务',
        testId: 'tasks-btn',
        icon: 'tasks'
      }
    ]
  },
  {
    groupLabel: '知识库',
    items: [
      {
        mode: 'bake',
        label: '记忆',
        testId: 'bake-btn',
        icon: 'memory'
      },
      {
        mode: 'knowledge',
        label: '采集',
        testId: 'knowledge-btn',
        icon: 'capture'
      },
      {
        mode: 'diary',
        label: '日记',
        testId: 'diary-btn',
        icon: 'profile'
      },
      {
        mode: 'integration',
        label: '集成',
        testId: 'integration-btn',
        icon: 'integration'
      },
    ]
  },
  {
    groupLabel: '系统',
    items: [
      {
        mode: 'models',
        label: '模型',
        testId: 'models-btn',
        icon: 'models'
      },
      {
        mode: 'privacy',
        label: '隐私',
        testId: 'privacy-btn',
        icon: 'privacy'
      },
      {
        mode: 'monitor',
        label: '监控',
        testId: 'monitor-btn',
        icon: 'monitor'
      },
      {
        mode: 'settings',
        label: '设置',
        testId: 'settings-btn',
        icon: 'settings'
      },
    ]
  }
]

const SIDEBAR_COLLAPSED_KEY = 'memory-bread_sidebar_collapsed'

const loadSidebarCollapsed = () => {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true'
  } catch {
    return false
  }
}

const getAccountInitials = (label: string): string => {
  const normalized = label.trim()
  if (!normalized) return '记'

  const firstCharacter = Array.from(normalized)[0]
  if (/\p{Script=Han}/u.test(firstCharacter)) return firstCharacter

  const words = normalized.split(/\s+/).filter(Boolean)
  if (words.length > 1) {
    return words
      .slice(0, 2)
      .map((word) => Array.from(word)[0])
      .join('')
      .toUpperCase()
  }

  return Array.from(normalized).slice(0, 2).join('').toUpperCase()
}

const FloatingBuddy: React.FC<FloatingBuddyProps> = ({ className = '', softwareUpdate, onSoftwareUpdateClick }) => {
  const { windowMode, setWindowMode, clearBakeNavigationStack, currentUser, cloudSubscription, localNickname } = useAppStore()
  const updateSession = useSoftwareUpdateSession()
  const [collapsed, setCollapsed] = useState(loadSidebarCollapsed)
  const accountLabel = getUserDisplayName(currentUser, localNickname)
  const runModeLabel = getRunModeLabel(currentUser, cloudSubscription)
  const accountInitials = getAccountInitials(accountLabel)
  const handleNavigate = (mode: WindowMode) => {
    clearBakeNavigationStack()
    setWindowMode(mode)
  }

  const toggleCollapsed = () => {
    setCollapsed(value => {
      const next = !value
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next))
      } catch { /* localStorage may be unavailable in restricted environments */ }
      return next
    })
  }

  return (
    <aside
      className={`floating-buddy-v2${collapsed ? ' floating-buddy-v2--collapsed' : ''} ${className}`.trim()}
      data-testid="floating-buddy"
      data-collapsed={collapsed}
    >
      <div className="buddy-sidebar-header">
        <div className="buddy-sidebar-logo">
          <img
            src="/brand/memorybread-bread-mark.png"
            alt="记忆面包"
            className="buddy-sidebar-logo-img"
          />
        </div>
        <div className="buddy-sidebar-title-group">
          <h1 className="buddy-sidebar-title">记忆面包</h1>
          <p className="buddy-sidebar-subtitle">品尝新知识</p>
        </div>
        <button
          type="button"
          className="buddy-sidebar-collapse"
          onClick={toggleCollapsed}
          aria-label={collapsed ? '展开左侧菜单' : '折叠左侧菜单'}
          aria-expanded={!collapsed}
          title={collapsed ? '展开菜单' : '折叠菜单'}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>

      <nav className="buddy-actions" aria-label="主菜单">
        {MENU_GROUPS.map((group) => (
          <div key={group.groupLabel} className="buddy-menu-group">
            <div className="buddy-menu-group__label">{group.groupLabel}</div>
            {group.items.map((item) => {
              const isActive = windowMode === item.mode
              return (
                <button
                  key={item.mode}
                  className={`buddy-action-btn ${isActive ? 'buddy-action-btn--active' : ''}`}
                  data-testid={item.testId}
                  onClick={() => handleNavigate(item.mode)}
                  aria-label={item.label}
                  title={item.label}
                  type="button"
                >
                  <span className="buddy-action-btn__icon" aria-hidden="true">
                    <BreadAppIcon name={item.icon} size={24} />
                  </span>
                  <span className="buddy-action-btn__label">{item.label}</span>
                </button>
              )
            })}
          </div>
        ))}
      </nav>

      <footer className="buddy-sidebar-footer">
        {softwareUpdate?.update_available && softwareUpdate.release && (() => {
          const sessionMatches = updateSession.version === softwareUpdate.latest_version
          const sessionPhase = sessionMatches ? updateSession.phase : 'idle'
          const sessionBusy = softwareUpdateSessionBusy(sessionPhase)
          const sessionPercent = sessionMatches ? updateSession.progress?.percent ?? null : null
          const entryState = sessionBusy ? 'busy' : sessionPhase
          const entryCopy = sessionBusy
            ? `更新中${sessionPercent != null ? ` ${sessionPercent}%` : ''}`
            : sessionPhase === 'ready_to_restart' ? '待重启完成更新'
            : sessionPhase === 'failed' ? '更新失败'
            : softwareUpdate.is_mandatory ? '需要更新' : '更新可用'
          const entryAction = sessionBusy ? '后台' : sessionPhase === 'ready_to_restart' ? '重启' : sessionPhase === 'failed' ? '重试' : '更新'
          const entryTitle = sessionBusy
            ? '正在后台更新，点击查看详情'
            : sessionPhase === 'ready_to_restart' ? '更新已就绪，点击重启完成'
            : sessionPhase === 'failed' ? '更新失败，点击查看详情'
            : softwareUpdate.is_mandatory ? '需要更新软件' : '有可用软件更新'
          return (
            <button
              className={`buddy-update-entry ${softwareUpdate.is_mandatory ? 'buddy-update-entry--mandatory' : ''} ${entryState !== 'idle' ? `buddy-update-entry--${entryState}` : ''}`}
              data-testid="software-update-entry"
              onClick={onSoftwareUpdateClick}
              type="button"
              title={entryTitle}
              aria-label={entryTitle}
            >
              <span className="buddy-update-entry__icon" aria-hidden="true">
                {sessionBusy ? <Loader2 className="buddy-update-entry__spinner" size={17} /> : sessionPhase === 'ready_to_restart' ? <RotateCw size={17} /> : <ArrowDownToLine size={17} />}
              </span>
              <span className="buddy-update-entry__copy">
                <strong>{entryCopy}</strong>
                <span>v{softwareUpdate.latest_version}</span>
              </span>
              <span className="buddy-update-entry__action">{entryAction}</span>
              {sessionBusy && sessionPercent != null && (
                <span className="buddy-update-entry__progress" aria-hidden="true"><i style={{ width: `${sessionPercent}%` }} /></span>
              )}
            </button>
          )
        })()}
        <button
          className={`buddy-account-entry ${windowMode === 'account' || windowMode === 'messages' ? 'buddy-account-entry--active' : ''}`}
          data-testid="account-entry"
          type="button"
          aria-current={windowMode === 'account' || windowMode === 'messages' ? 'page' : undefined}
          aria-label={`打开${accountLabel}的个人中心`}
          title={`${accountLabel} · ${runModeLabel}`}
          onClick={() => handleNavigate('account')}
        >
          <span className="buddy-account-entry__avatar" data-testid="account-avatar" aria-hidden="true">
            {accountInitials}
          </span>
          <span className="buddy-account-entry__identity">
            <strong>{accountLabel}</strong>
            <span>{runModeLabel}</span>
          </span>
          <ChevronRight className="buddy-account-entry__chevron" size={16} aria-hidden="true" />
        </button>
      </footer>
    </aside>
  )
}

export default FloatingBuddy
