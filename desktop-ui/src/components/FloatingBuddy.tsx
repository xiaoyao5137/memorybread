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

import React from 'react'
import { ArrowDownToLine, ChevronRight, LogIn } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { type WindowMode } from '../types'
import { getRunModeLabel, getUserDisplayName } from '../utils/accountDisplay'
import type { SoftwareUpdateCheck } from '../utils/softwareUpdate'
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
  const { windowMode, setWindowMode, clearBakeNavigationStack, currentUser, cloudSubscription } = useAppStore()
  const accountLabel = getUserDisplayName(currentUser)
  const runModeLabel = getRunModeLabel(currentUser, cloudSubscription)
  const accountInitials = getAccountInitials(accountLabel)
  const handleNavigate = (mode: WindowMode) => {
    clearBakeNavigationStack()
    setWindowMode(mode)
  }

  return (
    <aside
      className={`floating-buddy-v2 ${className}`}
      data-testid="floating-buddy"
    >
      <div className="buddy-sidebar-header">
        <div className="buddy-sidebar-logo">
          <img src="/logo.png" alt="记忆面包" className="buddy-sidebar-logo-img" />
        </div>
        <div className="buddy-sidebar-title-group">
          <h1 className="buddy-sidebar-title">记忆面包</h1>
          <p className="buddy-sidebar-subtitle">品尝新知识</p>
        </div>
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
        {softwareUpdate?.update_available && softwareUpdate.release && (
          <button
            className={`buddy-update-entry ${softwareUpdate.is_mandatory ? 'buddy-update-entry--mandatory' : ''}`}
            data-testid="software-update-entry"
            onClick={onSoftwareUpdateClick}
            type="button"
          >
            <span className="buddy-update-entry__icon" aria-hidden="true"><ArrowDownToLine size={17} /></span>
            <span className="buddy-update-entry__copy">
              <strong>{softwareUpdate.is_mandatory ? '需要更新' : '更新可用'}</strong>
              <span>v{softwareUpdate.latest_version}</span>
            </span>
            <span className="buddy-update-entry__action">更新</span>
          </button>
        )}
        <button
          className={`buddy-account-entry ${windowMode === 'account' || windowMode === 'messages' ? 'buddy-account-entry--active' : ''}`}
          data-testid="account-entry"
          type="button"
          aria-current={windowMode === 'account' || windowMode === 'messages' ? 'page' : undefined}
          aria-label={currentUser ? `打开${accountLabel}的用户账户` : '未登录，打开登录'}
          onClick={() => handleNavigate('account')}
        >
          <span className="buddy-account-entry__avatar" data-testid="account-avatar" aria-hidden="true">
            {currentUser ? accountInitials : <LogIn size={17} />}
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
