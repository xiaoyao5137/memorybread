import React, { useCallback, useEffect, useState } from 'react'
import { listen } from '@tauri-apps/api/event'
import { invoke } from '@tauri-apps/api/core'
import {
  CREATION_MODEL_PREFERENCE_KEY,
  loadCreationModels,
  normalizeCreationModels,
  useAppStore,
} from './store/useAppStore'
import FloatingBuddy          from './components/FloatingBuddy'
import RagPanel               from './components/RagPanel.v2'
import CreationPanel          from './components/CreationPanel'
import RepositoryPanel        from './components/RepositoryPanel'
import ModelManager           from './components/ModelManager'
import PrivacyPanel           from './components/PrivacyPanel'
import ActionConfirm          from './components/ActionConfirm'
import Settings               from './components/Settings'
import DebugPanel             from './components/DebugPanel'
import ScheduledTasksPanel    from './components/ScheduledTasksPanel'
import MonitorPanel           from './components/MonitorPanel'
import BakePanel              from './components/BakePanel'
import DiaryPanel             from './components/DiaryPanel'
import IntegrationPanel       from './components/IntegrationPanel'
import OnboardingWizard       from './components/OnboardingWizard'
import AuthPanel              from './components/AuthPanel'
import AchievementCelebration from './components/AchievementCelebration'
import SystemFloatingAssist   from './components/SystemFloatingAssist'
import AboutPanel             from './components/AboutPanel'
import SoftwareUpdateNotice   from './components/SoftwareUpdateNotice'
import PanelErrorBoundary     from './components/PanelErrorBoundary'
import { cloudSessionIsInvalid, fetchConsoleSummary, fetchCurrentUser } from './utils/authApi'
import { syncEligibleBreadcrumbRules } from './utils/breadcrumbRules'
import {
  FLOATING_ASSIST_ENABLED_KEY,
  readFloatingAssistAutoTaskConfig,
  type FloatingAssistAutoTaskConfig,
  writeFloatingAssistAutoTaskConfig,
} from './utils/floatingAssistAutoTask'
import { startGlobalShortcutRuntime } from './utils/interactionSettings'
import { synchronizeWorkProfile } from './utils/workProfileCloud'
import { getAppMetadata } from './utils/appMetadata'
import {
  fetchSoftwareUpdate,
  registerCurrentDevice,
  SOFTWARE_UPDATE_REQUEST_EVENT,
  shouldShowSoftwareUpdate,
  snoozeSoftwareUpdate,
  type SoftwareUpdateCheck,
} from './utils/softwareUpdate'
import { fetchInitializationStatus, initializationIsReady } from './utils/initialization'
import { createOptionalCloudRequestSignal, optionalCloudIsReachable } from './utils/optionalCloud'
import type { AccountProfileSection, BreadcrumbAward } from './types'

const WORK_PROFILE_SYNC_INTERVAL_MS = 5 * 60 * 1000
const ACHIEVEMENT_SYNC_INTERVAL_MS = 5 * 60 * 1000
const SOFTWARE_UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000

interface AccountNavigationRequest {
  section: AccountProfileSection
  highlightedAchievementKeys: string[]
}

const hasConfiguredCreationModel = (configs: Array<{ enabled?: boolean; apiKey?: string }>) =>
  configs.some(config => Boolean(config.enabled || config.apiKey))

const parseReferenceId = (docKey?: string | null) => {
  if (!docKey) return null
  const match = String(docKey).match(/:(\d+)$/)
  return match ? match[1] : null
}

const App: React.FC = () => {
  const searchParams = new URLSearchParams(window.location.search)
  const isFloatingAssistWindow = searchParams.get('view') === 'floating-assist'
  if (isFloatingAssistWindow) {
    // 原生层已经负责悬浮窗口的显隐。这里必须立即挂载视觉主体，避免 sidecar
    // 尚未就绪或临时离线时只剩一个空白原生窗口；实际咨询仍由各自请求处理错误。
    return <SystemFloatingAssist />
  }
  const {
    windowMode,
    setWindowMode,
    setBakeTab,
    setRepositoryTab,
    setSelectedMemoryId,
    setSelectedCaptureId,
    setSelectedTemplateId,
    setSelectedKnowledgeId,
    setSelectedSopId,
    setRepositoryMemoryFocusId,
    setBakeTemplateFocusId,
    setBakeKnowledgeFocusId,
    setBakeSopFocusId,
    setBakeTemplateOffset,
    setBakeTemplateLimit,
    setBakeKnowledgeOffset,
    setBakeKnowledgeLimit,
    setBakeSopOffset,
    setBakeSopLimit,
    setRepositoryCaptureSourceCaptureId,
    pushBakeNavigationTarget,
    clearBakeNavigationStack,
    hasCompletedSetup,
    setHasCompletedSetup,
    apiBaseUrl,
    adminApiBaseUrl,
    authToken,
    currentUser,
    serviceEnvironment,
    debugModeEnabled,
    setCreationModelConfigs,
    setAuthSession,
    setCloudBalance,
    setCloudSubscription,
    clearAuthSession,
  } = useAppStore()

  const [achievementCelebrations, setAchievementCelebrations] = useState<BreadcrumbAward[][]>([])
  const [accountNavigation, setAccountNavigation] = useState<AccountNavigationRequest | null>(null)
  const [softwareUpdate, setSoftwareUpdate] = useState<SoftwareUpdateCheck | null>(null)
  const [softwareUpdateNoticeOpen, setSoftwareUpdateNoticeOpen] = useState(false)
  // v2 完成标记只会在全量质检与冒烟测试通过后写入。已完成用户启动时
  // 直接放行主界面，再在后台核验 sidecar，避免 DMG 冷启动被本地服务就绪时间阻塞。
  const [initializationValidated, setInitializationValidated] = useState(hasCompletedSetup)

  const showOnboarding = !initializationValidated || !hasCompletedSetup
  const activeAchievementCelebration = achievementCelebrations[0] ?? null

  const handleInitializationValidated = useCallback((ready: boolean) => {
    setInitializationValidated(ready)
  }, [])

  useEffect(() => {
    if (showOnboarding) return
    const verify = () => {
      void fetchInitializationStatus()
        .then(next => {
          if (!initializationIsReady(next)) {
            setHasCompletedSetup(false)
            setInitializationValidated(false)
          }
        })
        .catch(() => {
          // 短暂的 sidecar 重启不应清除已经通过质检的本地完成标记。
        })
    }
    verify()
    const interval = window.setInterval(verify, 30_000)
    window.addEventListener('focus', verify)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', verify)
    }
  }, [setHasCompletedSetup, showOnboarding])

  const dismissAchievementCelebration = useCallback(() => {
    setAchievementCelebrations((queue) => queue.slice(1))
  }, [])

  const viewCelebratedAchievements = useCallback(() => {
    if (activeAchievementCelebration) {
      setAccountNavigation({
        section: 'achievements',
        highlightedAchievementKeys: activeAchievementCelebration.map(
          ({ breadcrumb }) => breadcrumb.breadcrumb_key,
        ),
      })
    }
    setAchievementCelebrations((queue) => queue.slice(1))
    setWindowMode('account')
  }, [activeAchievementCelebration, setWindowMode])

  const handleAccountNavigationConsumed = useCallback(() => {
    setAccountNavigation(null)
  }, [])

  useEffect(() => {
    if (showOnboarding) return undefined
    return startGlobalShortcutRuntime(async action => {
      if (action === 'recognize_screen_task') {
        await invoke('trigger_floating_assist_action', { action })
        return
      }

      setWindowMode(action === 'open_creation' ? 'creation' : 'rag')
      await invoke('show_main_panel_from_floating_assist').catch(() => {})
    })
  }, [setWindowMode, showOnboarding])

  useEffect(() => {
    let cancelled = false
    const cleanups: Array<() => void> = []

    const syncCaptureMenuState = async (): Promise<boolean> => {
      try {
        const response = await fetch(`${apiBaseUrl}/api/runtime/status`)
        if (!response.ok) return false
        const status = await response.json() as { capture_enabled: boolean }
        if (!cancelled) {
          await invoke('set_capture_menu_state', { enabled: status.capture_enabled })
        }
        return true
      } catch {
        // 浏览器预览或 Core Engine 尚未启动时保持菜单默认关闭。
        return false
      }
    }

    const syncCaptureMenuUntilReady = async () => {
      for (let attempt = 0; attempt < 12 && !cancelled; attempt += 1) {
        if (await syncCaptureMenuState()) return
        await new Promise(resolve => window.setTimeout(resolve, 5000))
      }
    }

    const registerTrayEvents = async () => {
      try {
        const floatingAssistEnabled = localStorage.getItem(FLOATING_ASSIST_ENABLED_KEY) === 'true'
        const autoTaskConfig = readFloatingAssistAutoTaskConfig()
        const autoTaskDetectionEnabled = floatingAssistEnabled && autoTaskConfig.enabled
        await invoke('set_floating_assist_menu_state', { enabled: floatingAssistEnabled })
        await invoke('set_floating_assist_auto_task_menu_state', {
          checked: autoTaskDetectionEnabled,
          enabled: floatingAssistEnabled,
        })
        if (floatingAssistEnabled) {
          await invoke('set_floating_assist_visible', { enabled: true })
        }
        cleanups.push(await listen('tray-navigate-settings', () => {
          setWindowMode('settings')
        }))
        cleanups.push(await listen('tray-navigate-about', () => {
          setWindowMode('about')
        }))
        cleanups.push(await listen<boolean>('tray-floating-assist-changed', event => {
          localStorage.setItem(FLOATING_ASSIST_ENABLED_KEY, String(event.payload))
          if (!event.payload) {
            writeFloatingAssistAutoTaskConfig({
              ...readFloatingAssistAutoTaskConfig(),
              enabled: false,
            })
          }
          invoke('set_floating_assist_auto_task_menu_state', {
            checked: event.payload && readFloatingAssistAutoTaskConfig().enabled,
            enabled: event.payload,
          }).catch(() => {})
        }))
        cleanups.push(await listen<boolean | FloatingAssistAutoTaskConfig>('floating-assist-auto-task-changed', event => {
          if (typeof event.payload === 'boolean') {
            writeFloatingAssistAutoTaskConfig({
              ...readFloatingAssistAutoTaskConfig(),
              enabled: Boolean(event.payload),
            })
          } else {
            writeFloatingAssistAutoTaskConfig(event.payload)
          }
        }))
        cleanups.push(await listen<boolean>('tray-capture-changed', async event => {
          const requested = event.payload
          try {
            const response = await fetch(`${apiBaseUrl}/api/runtime/status`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ capture_enabled: requested }),
            })
            if (!response.ok) throw new Error(`runtime status update failed: ${response.status}`)
          } catch {
            await syncCaptureMenuState()
          }
        }))
        void syncCaptureMenuUntilReady()
      } catch {
        // 普通浏览器环境没有 Tauri event runtime。
      }
    }

    void registerTrayEvents()
    return () => {
      cancelled = true
      cleanups.forEach(cleanup => cleanup())
    }
  }, [apiBaseUrl, setWindowMode])

  useEffect(() => {
    let cancelled = false

    const loadCreationModelPreference = async () => {
      try {
        const resp = await fetch(`${apiBaseUrl}/preferences`)
        if (!resp.ok) return
        const data = await resp.json()
        const pref = (data.preferences || []).find((item: { key: string }) => item.key === CREATION_MODEL_PREFERENCE_KEY)
        const localConfigs = loadCreationModels()
        if (pref?.value) {
          const configs = normalizeCreationModels(JSON.parse(pref.value))
          if (!hasConfiguredCreationModel(configs) && hasConfiguredCreationModel(localConfigs)) {
            if (!cancelled) setCreationModelConfigs(localConfigs)
            await fetch(`${apiBaseUrl}/preferences/${encodeURIComponent(CREATION_MODEL_PREFERENCE_KEY)}`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ value: JSON.stringify(localConfigs) }),
            })
            return
          }
          if (!cancelled) setCreationModelConfigs(configs)
          return
        }

        await fetch(`${apiBaseUrl}/preferences/${encodeURIComponent(CREATION_MODEL_PREFERENCE_KEY)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: JSON.stringify(localConfigs) }),
        })
      } catch {
        // 保留本地配置作为离线兜底。
      }
    }

    void loadCreationModelPreference()
    return () => { cancelled = true }
  }, [apiBaseUrl, setCreationModelConfigs])

  useEffect(() => {
    if (!authToken || showOnboarding) return undefined
    let cancelled = false
    let validating = false
    const lifecycleController = new AbortController()
    const validateSession = async () => {
      if (cancelled || validating || !optionalCloudIsReachable()) return
      validating = true
      const request = createOptionalCloudRequestSignal(lifecycleController.signal)
      try {
        const user = await fetchCurrentUser(adminApiBaseUrl, authToken, request.signal)
        if (!cancelled) {
          setAuthSession({
            access_token: authToken,
            expires_at: useAppStore.getState().authExpiresAt || new Date(Date.now() + 30 * 86400_000).toISOString(),
            user,
          })
        }
        const summary = await fetchConsoleSummary(
          adminApiBaseUrl,
          authToken,
          request.signal,
        ).catch(() => null)
        if (!cancelled && summary) {
          setCloudBalance(summary.balance ?? null)
          setCloudSubscription(summary.current_subscription ?? null)
        }
      } catch (error) {
        // 断网、超时和云端故障只让账户增强暂时降级；只有明确的鉴权拒绝才注销。
        if (!cancelled && cloudSessionIsInvalid(error)) clearAuthSession()
      } finally {
        request.dispose()
        validating = false
      }
    }
    void validateSession()
    window.addEventListener('online', validateSession)
    return () => {
      cancelled = true
      lifecycleController.abort()
      window.removeEventListener('online', validateSession)
    }
  }, [adminApiBaseUrl, authToken, clearAuthSession, setAuthSession, setCloudBalance, setCloudSubscription, showOnboarding])

  useEffect(() => {
    if (showOnboarding) return undefined
    let cancelled = false
    let checking = false
    const lifecycleController = new AbortController()

    const checkForUpdate = async () => {
      if (checking || cancelled || !optionalCloudIsReachable()) return
      checking = true
      const request = createOptionalCloudRequestSignal(lifecycleController.signal)
      try {
        const metadata = await getAppMetadata()
        const update = await fetchSoftwareUpdate(adminApiBaseUrl, metadata, 'stable', request.signal)
        if (!cancelled) {
          setSoftwareUpdate(update.update_available && update.release ? update : null)
          if (shouldShowSoftwareUpdate(update)) setSoftwareUpdateNoticeOpen(true)
        }
      } catch {
        // 软件更新检查不阻断本地能力；联网恢复后会自动重试。
      } finally {
        request.dispose()
        checking = false
      }
    }
    const checkWhenVisible = () => {
      if (document.visibilityState === 'visible') void checkForUpdate()
    }

    void checkForUpdate()
    const interval = window.setInterval(() => void checkForUpdate(), SOFTWARE_UPDATE_CHECK_INTERVAL_MS)
    window.addEventListener('online', checkForUpdate)
    document.addEventListener('visibilitychange', checkWhenVisible)
    return () => {
      cancelled = true
      lifecycleController.abort()
      window.clearInterval(interval)
      window.removeEventListener('online', checkForUpdate)
      document.removeEventListener('visibilitychange', checkWhenVisible)
    }
  }, [adminApiBaseUrl, serviceEnvironment, showOnboarding])

  useEffect(() => {
    const openRequestedUpdate = (event: Event) => {
      const update = (event as CustomEvent<SoftwareUpdateCheck>).detail
      if (!update?.update_available || !update.release) return
      setSoftwareUpdate(update)
      setSoftwareUpdateNoticeOpen(true)
    }
    window.addEventListener(SOFTWARE_UPDATE_REQUEST_EVENT, openRequestedUpdate)
    return () => window.removeEventListener(SOFTWARE_UPDATE_REQUEST_EVENT, openRequestedUpdate)
  }, [])

  useEffect(() => {
    if (!authToken || !currentUser || showOnboarding) return undefined
    let cancelled = false
    let reporting = false
    const lifecycleController = new AbortController()
    const reportVersion = async () => {
      if (cancelled || reporting || !optionalCloudIsReachable()) return
      reporting = true
      const request = createOptionalCloudRequestSignal(lifecycleController.signal)
      try {
        await registerCurrentDevice(adminApiBaseUrl, authToken, {
          environment: serviceEnvironment,
          userId: currentUser.id,
        }, request.signal)
      } catch {
        // 设备版本上报是可重试的后台动作，不影响离线使用。
      } finally {
        request.dispose()
        reporting = false
      }
    }
    void reportVersion()
    const interval = window.setInterval(() => void reportVersion(), SOFTWARE_UPDATE_CHECK_INTERVAL_MS)
    window.addEventListener('online', reportVersion)
    return () => {
      cancelled = true
      lifecycleController.abort()
      window.clearInterval(interval)
      window.removeEventListener('online', reportVersion)
    }
  }, [adminApiBaseUrl, authToken, currentUser?.id, serviceEnvironment, showOnboarding])

  useEffect(() => {
    if (!authToken || !currentUser || showOnboarding) return undefined
    let cancelled = false
    let syncing = false
    const lifecycleController = new AbortController()

    const sync = async () => {
      if (cancelled || syncing || !optionalCloudIsReachable()) return
      syncing = true
      const request = createOptionalCloudRequestSignal(lifecycleController.signal)
      try {
        await synchronizeWorkProfile({
          apiBaseUrl,
          adminApiBaseUrl,
          authToken,
          userId: currentUser.id,
          signal: request.signal,
        })
      } catch {
        // 本地画像始终可用；网络恢复或下个周期会自动重试云端同步。
      } finally {
        request.dispose()
        syncing = false
      }
    }
    const syncWhenVisible = () => {
      if (document.visibilityState === 'visible') void sync()
    }

    void sync()
    const interval = window.setInterval(() => void sync(), WORK_PROFILE_SYNC_INTERVAL_MS)
    window.addEventListener('online', sync)
    document.addEventListener('visibilitychange', syncWhenVisible)
    return () => {
      cancelled = true
      lifecycleController.abort()
      window.clearInterval(interval)
      window.removeEventListener('online', sync)
      document.removeEventListener('visibilitychange', syncWhenVisible)
    }
  }, [adminApiBaseUrl, apiBaseUrl, authToken, currentUser?.id, serviceEnvironment, showOnboarding])

  useEffect(() => {
    setAchievementCelebrations([])
    setAccountNavigation(null)
  }, [authToken, serviceEnvironment])

  useEffect(() => {
    if (!authToken || !currentUser || showOnboarding) return undefined
    const controller = new AbortController()
    let syncInFlight = false

    const syncAchievements = async () => {
      if (controller.signal.aborted || syncInFlight) return
      syncInFlight = true
      try {
        const awards = await syncEligibleBreadcrumbRules({
          adminApiBaseUrl,
          apiBaseUrl,
          authToken,
          signal: controller.signal,
        })
        if (!controller.signal.aborted && awards.length > 0) {
          setAchievementCelebrations((queue) => [...queue, awards])
        }
      } catch {
        // 面包屑结算是本地增强；本地核心恢复后会自动重试。
      } finally {
        syncInFlight = false
      }
    }
    const syncWhenVisible = () => {
      if (document.visibilityState === 'visible') void syncAchievements()
    }

    void syncAchievements()
    const interval = window.setInterval(() => void syncAchievements(), ACHIEVEMENT_SYNC_INTERVAL_MS)
    window.addEventListener('online', syncAchievements)
    document.addEventListener('visibilitychange', syncWhenVisible)
    return () => {
      controller.abort()
      window.clearInterval(interval)
      window.removeEventListener('online', syncAchievements)
      document.removeEventListener('visibilitychange', syncWhenVisible)
    }
  }, [adminApiBaseUrl, apiBaseUrl, authToken, currentUser?.id, serviceEnvironment, showOnboarding])

  // 监听查看采集记录事件
  useEffect(() => {
    const handleViewCapture = (event: CustomEvent) => {
      if (!debugModeEnabled) return
      const { captureId } = event.detail
      setWindowMode('debug')
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent('scroll-to-capture', {
          detail: { captureId }
        }))
      }, 100)
    }

    window.addEventListener('view-capture', handleViewCapture as EventListener)
    return () => {
      window.removeEventListener('view-capture', handleViewCapture as EventListener)
    }
  }, [debugModeEnabled, setWindowMode])

  useEffect(() => {
    const openReferenceDetail = (detail: any) => {
      const { type, captureId, knowledgeId, artifactId, documentId, docKey } = detail || {}
      const parsedTargetId = parseReferenceId(docKey)
      const targetId = String(documentId ?? artifactId ?? parsedTargetId ?? '')
      const hasTargetId = targetId.trim().length > 0
      const setRagBackTarget = (enabled: boolean) => {
        if (enabled) {
          pushBakeNavigationTarget({ windowMode: 'rag' })
        } else {
          clearBakeNavigationStack()
        }
      }

      if (type === 'document') {
        setRagBackTarget(hasTargetId)
        setBakeTab('templates')
        setBakeTemplateOffset(0)
        setBakeTemplateLimit(100)
        setBakeTemplateFocusId(targetId || null)
        setSelectedTemplateId(targetId || null)
        setWindowMode('bake')
        return
      }
      if (type === 'bake_knowledge') {
        setRagBackTarget(hasTargetId)
        setBakeTab('knowledge')
        setBakeKnowledgeOffset(0)
        setBakeKnowledgeLimit(1000)
        setBakeKnowledgeFocusId(targetId || null)
        setSelectedKnowledgeId(targetId || null)
        setWindowMode('bake')
        return
      }
      if (type === 'operation' || type === 'action') {
        setRagBackTarget(hasTargetId)
        setBakeTab('sop')
        setBakeSopOffset(0)
        setBakeSopLimit(1000)
        setBakeSopFocusId(targetId || null)
        setSelectedSopId(targetId || null)
        setWindowMode('bake')
        return
      }
      if (type === 'knowledge' && knowledgeId) {
        pushBakeNavigationTarget({ windowMode: 'rag' })
        setWindowMode('knowledge')
        setRepositoryTab('memory')
        setRepositoryMemoryFocusId(String(knowledgeId))
        setSelectedMemoryId(String(knowledgeId))
        return
      }
      if (captureId) {
        pushBakeNavigationTarget({ windowMode: 'rag' })
        setWindowMode('knowledge')
        setRepositoryTab('capture')
        setRepositoryCaptureSourceCaptureId(String(captureId))
        setSelectedCaptureId(String(captureId))
      }
    }

    const handleViewReference = (event: CustomEvent) => {
      openReferenceDetail(event.detail)
    }

    let tauriCleanup: (() => void) | null = null
    void listen<any>('floating-assist-open-reference', event => {
      openReferenceDetail(event.payload)
    }).then(cleanup => {
      tauriCleanup = cleanup
    }).catch(() => {})

    window.addEventListener('view-rag-reference', handleViewReference as EventListener)
    return () => {
      window.removeEventListener('view-rag-reference', handleViewReference as EventListener)
      tauriCleanup?.()
    }
  }, [
    clearBakeNavigationStack,
    pushBakeNavigationTarget,
    setBakeKnowledgeLimit,
    setBakeKnowledgeOffset,
    setBakeSopLimit,
    setBakeSopOffset,
    setBakeTab,
    setBakeTemplateLimit,
    setBakeTemplateOffset,
    setRepositoryCaptureSourceCaptureId,
    setRepositoryMemoryFocusId,
    setRepositoryTab,
    setBakeKnowledgeFocusId,
    setSelectedCaptureId,
    setSelectedKnowledgeId,
    setSelectedMemoryId,
    setSelectedSopId,
    setSelectedTemplateId,
    setBakeSopFocusId,
    setBakeTemplateFocusId,
    setWindowMode,
  ])

  if (showOnboarding) {
    return (
      <div className="app" data-testid="app-root">
        <OnboardingWizard onStatusValidated={handleInitializationValidated} />
        <ActionConfirm />
      </div>
    )
  }

  return (
    <div className="app" data-testid="app-root">
      <FloatingBuddy
        softwareUpdate={softwareUpdate}
        onSoftwareUpdateClick={() => setSoftwareUpdateNoticeOpen(true)}
      />

      <main className="app-content">
        <PanelErrorBoundary resetKey={`${windowMode}:${debugModeEnabled}`}>
          {windowMode === 'rag'       && <RagPanel />}
          {windowMode === 'creation'  && <CreationPanel />}
          {windowMode === 'knowledge' && <RepositoryPanel />}
          {windowMode === 'models'    && <ModelManager />}
          {windowMode === 'privacy'   && <PrivacyPanel />}
          {windowMode === 'settings'  && <Settings />}
          {debugModeEnabled && windowMode === 'debug' && <DebugPanel />}
          {windowMode === 'tasks'     && <ScheduledTasksPanel />}
          {windowMode === 'monitor'   && <MonitorPanel />}
          {windowMode === 'bake'      && <BakePanel />}
          {windowMode === 'diary'     && <DiaryPanel />}
          {windowMode === 'integration' && <IntegrationPanel />}
          {windowMode === 'about'     && <AboutPanel />}
          {(windowMode === 'account' || windowMode === 'messages') && (
            <AuthPanel
              highlightedAchievementKeys={accountNavigation?.highlightedAchievementKeys}
              initialProfileSection={windowMode === 'messages' ? 'messages' : accountNavigation?.section}
              key={windowMode}
              onInitialProfileSectionHandled={handleAccountNavigationConsumed}
            />
          )}
        </PanelErrorBoundary>
      </main>

      {activeAchievementCelebration && (
        <AchievementCelebration
          awards={activeAchievementCelebration}
          onDismiss={dismissAchievementCelebration}
          onViewCards={viewCelebratedAchievements}
        />
      )}
      {softwareUpdate && softwareUpdateNoticeOpen && (
        <SoftwareUpdateNotice
          update={softwareUpdate}
          onDismiss={() => {
            snoozeSoftwareUpdate(softwareUpdate.latest_version)
            setSoftwareUpdateNoticeOpen(false)
          }}
        />
      )}
      <ActionConfirm />
    </div>
  )
}

export default App
