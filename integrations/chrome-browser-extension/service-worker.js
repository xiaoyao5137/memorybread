const NATIVE_HOST = 'cn.memorybread.browser_bridge'
const EXTENSION_VERSION = chrome.runtime.getManifest().version
const POLL_INTERVAL_MS = 800
const HEARTBEAT_INTERVAL_MS = 5000
const PREVIEW_INTERVAL_MS = 900
const MAX_PREVIEW_BASE64_LENGTH = 900000
let nativePort = null
let pollTimer = null
let heartbeatTimer = null
let reconnectTimer = null
let busy = false

function connectNative() {
  clearTimeout(reconnectTimer)
  if (nativePort) {
    sendHeartbeat()
    poll()
    return
  }
  let port
  try {
    port = chrome.runtime.connectNative(NATIVE_HOST)
  } catch {
    scheduleReconnect()
    return
  }
  nativePort = port
  port.onMessage.addListener(handleNativeResponse)
  port.onDisconnect.addListener(() => {
    if (nativePort !== port) return
    nativePort = null
    clearInterval(pollTimer)
    pollTimer = null
    clearInterval(heartbeatTimer)
    heartbeatTimer = null
    scheduleReconnect()
  })
  sendHeartbeat()
  if (!pollTimer) {
    pollTimer = setInterval(poll, POLL_INTERVAL_MS)
  }
  if (!heartbeatTimer) {
    heartbeatTimer = setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS)
  }
  poll()
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer)
  reconnectTimer = setTimeout(connectNative, 3000)
}

function poll() {
  if (!nativePort || busy) return
  nativePort.postMessage({type: 'poll', extension_version: EXTENSION_VERSION})
}

function sendHeartbeat() {
  nativePort?.postMessage({type: 'heartbeat', extension_version: EXTENSION_VERSION})
}

function sendProgress(job, progress) {
  nativePort?.postMessage({
    type: 'progress',
    extension_version: EXTENSION_VERSION,
    progress: {
      browser_job_id: job.browser_job_id,
      status: 'running',
      ...progress,
    },
  })
}

async function handleNativeResponse(message) {
  if (!message?.job || busy) return
  busy = true
  try {
    const result = await executeJob(message.job)
    nativePort?.postMessage({type: 'result', extension_version: EXTENSION_VERSION, result})
  } finally {
    busy = false
  }
}

async function executeJob(job) {
  let tabId = null
  let preview = null
  try {
    if (Date.now() >= Number(job.deadline_ms || 0)) throw jobError('JOB_EXPIRED', '任务已经过期')
    const targetUrl = new URL(job.url)
    if (!['http:', 'https:'].includes(targetUrl.protocol)) throw jobError('URL_INVALID', '只允许读取 HTTP(S) 页面')
    sendProgress(job, {stage: 'opening', url: job.url})
    const tab = await chrome.tabs.create({url: job.url, active: false, pinned: false})
    tabId = tab.id
    if (tabId == null) throw jobError('BACKGROUND_TAB_BLOCKED', '无法创建后台标签')
    preview = await startLivePreview(job, tabId)
    preview.setStage('loading')
    await waitForTab(tabId, Math.min(30000, Math.max(1000, job.deadline_ms - Date.now())))
    const loadedTab = await chrome.tabs.get(tabId)
    preview.setStage('reading', loadedTab.title || '', loadedTab.url || job.url)
    await preview.capture()
    await chrome.scripting.executeScript({target: {tabId, allFrames: false}, files: ['content-runtime.js']})
    const result = await chrome.tabs.sendMessage(tabId, {type: 'memorybread.extract', job})
    preview.setStage('finalizing', result?.title || loadedTab.title || '', result?.url || loadedTab.url || job.url)
    await preview.capture()
    return {browser_job_id: job.browser_job_id, ...result}
  } catch (error) {
    return {
      browser_job_id: job.browser_job_id,
      status: 'failed',
      error_code: error?.code || 'EXTENSION_SCRAPE_FAILED',
      error_message: String(error?.message || error || 'Chrome 扩展读取失败'),
    }
  } finally {
    if (preview) await preview.stop()
    if (tabId != null) {
      try { await chrome.tabs.remove(tabId) } catch {}
    }
  }
}

async function startLivePreview(job, tabId) {
  const debuggee = {tabId}
  let attached = false
  let capturing = false
  let stage = 'opening'
  let title = ''
  let url = job.url
  let timer = null

  try {
    await chrome.debugger.attach(debuggee, '1.3')
    attached = true
    await chrome.debugger.sendCommand(debuggee, 'Page.enable')
  } catch {
    sendProgress(job, {stage, title, url})
  }

  const capture = async () => {
    if (!attached || capturing) return
    capturing = true
    try {
      const response = await chrome.debugger.sendCommand(debuggee, 'Page.captureScreenshot', {
        format: 'jpeg',
        quality: 48,
        fromSurface: true,
        captureBeyondViewport: false,
      })
      const previewBase64 = typeof response?.data === 'string' ? response.data : ''
      sendProgress(job, {
        stage,
        title,
        url,
        ...(previewBase64 && previewBase64.length <= MAX_PREVIEW_BASE64_LENGTH
          ? {preview_base64: previewBase64, preview_mime_type: 'image/jpeg'}
          : {}),
      })
    } catch {
      sendProgress(job, {stage, title, url})
    } finally {
      capturing = false
    }
  }

  const setStage = (nextStage, nextTitle = title, nextUrl = url) => {
    stage = nextStage
    title = nextTitle
    url = nextUrl
    sendProgress(job, {stage, title, url})
  }

  if (attached) {
    timer = setInterval(() => void capture(), PREVIEW_INTERVAL_MS)
    await capture()
  }

  return {
    capture,
    setStage,
    stop: async () => {
      if (timer) clearInterval(timer)
      if (attached) {
        await capture()
        try { await chrome.debugger.detach(debuggee) } catch {}
      }
    },
  }
}

function waitForTab(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => finish(jobError('NAVIGATION_TIMEOUT', '后台页面加载超时')), timeoutMs)
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === 'complete') finish()
    }
    function finish(error) {
      clearTimeout(timeout)
      chrome.tabs.onUpdated.removeListener(listener)
      error ? reject(error) : resolve()
    }
    chrome.tabs.onUpdated.addListener(listener)
    chrome.tabs.get(tabId).then(tab => {
      if (tab.status === 'complete') finish()
    }).catch(() => finish(jobError('TAB_CLOSED', '后台标签已关闭')))
  })
}

function jobError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}

chrome.runtime.onInstalled.addListener(connectNative)
chrome.runtime.onStartup.addListener(connectNative)
chrome.alarms.create('memorybread-reconnect', {periodInMinutes: 0.5})
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== 'memorybread-reconnect') return
  if (!nativePort) connectNative()
  else {
    sendHeartbeat()
    poll()
  }
})
connectNative()
