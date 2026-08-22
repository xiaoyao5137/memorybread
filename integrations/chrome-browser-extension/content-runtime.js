if (!globalThis.__memorybreadContentRuntimeInstalled) {
  globalThis.__memorybreadContentRuntimeInstalled = true

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== 'memorybread.extract') return false
    extractPage(message.job).then(sendResponse).catch(error => sendResponse({
      status: 'failed',
      error_code: error?.code || 'EXTRACTION_FAILED',
      error_message: String(error?.message || error || '页面提取失败'),
    }))
    return true
  })
}

async function extractPage(job) {
  const maxCharacters = Math.max(1000, Math.min(Number(job.max_characters || 80000), 120000))
  const maxSegments = Math.max(1, Math.min(Number(job.max_segments || 20), 30))
  const deadline = Math.min(Number(job.deadline_ms || Date.now() + 60000), Date.now() + 70000)
  // 为正文/表格提取保留独立预算。自然语言指标只是选择偏好，不能因为
  // 未逐字命中而把整个任务耗到 deadline，最终连一次 DOM 采集都不执行。
  const extractionReserveMs = Math.max(3000, Number(job.extraction_reserve_ms || 10000))
  const readinessDeadline = Math.min(
    deadline,
    Math.max(Date.now() + 1000, deadline - extractionReserveMs),
  )
  const readiness = await waitForReportReadiness(readinessDeadline, job.requested_metrics || [])

  const title = document.title || ''
  const initialText = visibleText()
  if (/登录|sign\s*in|log\s*in/i.test(`${title}\n${initialText.slice(0, 1200)}`) && initialText.length < 2500) {
    throw extractionError('AUTH_REQUIRED', '页面需要先在 Chrome 中登录')
  }

  const segments = initialText ? [initialText] : []
  const seen = new Set()
  if (initialText) seen.add(initialText.slice(0, 2000) + initialText.slice(-2000))
  let reachedEnd = false
  for (let index = 0; index < maxSegments && Date.now() < deadline; index += 1) {
    const text = visibleText()
    const fingerprint = text.slice(0, 2000) + text.slice(-2000)
    if (!seen.has(fingerprint)) {
      seen.add(fingerprint)
      segments.push(text)
    }
    const viewport = Math.max(window.innerHeight, 1)
    const maximum = Math.max(0, document.documentElement.scrollHeight - viewport)
    if (window.scrollY >= maximum - 3) {
      reachedEnd = true
      break
    }
    window.scrollTo({top: Math.min(maximum, window.scrollY + Math.floor(viewport * 0.85)), behavior: 'instant'})
    await delay(220)
  }

  const tables = Array.from(document.querySelectorAll('table')).slice(0, 24).map(table => (
    Array.from(table.rows).slice(0, 200).map(row => (
      Array.from(row.cells).slice(0, 40).map(cell => (cell.innerText || '').trim())
    ))
  )).filter(table => table.some(row => row.some(cell => cell)))
  const tableText = tables.map(table => (
    table.map(row => row.filter(Boolean).join(' ')).filter(Boolean).join('\n')
  )).filter(Boolean).join('\n')
  const merged = (mergeSegments(segments) || tableText).slice(0, maxCharacters)
  const truncated = merged.length >= maxCharacters || !reachedEnd
  return {
    status: truncated ? 'partial' : 'complete',
    title,
    url: location.href,
    content_text: merged,
    structured_data: {
      tables,
      requested_metrics: job.requested_metrics || [],
      page_state: readiness,
    },
    completeness: {
      status: truncated ? 'partial' : 'complete',
      reached_end: reachedEnd,
      stable_passes: readiness.stable_passes,
      segment_count: segments.length,
      truncated,
    },
  }
}

async function waitForReportReadiness(deadline, requestedMetrics, options = {}) {
  const startedAt = Date.now()
  const patientWaitMs = Math.max(0, Number(options.patientWaitMs ?? options.minimumWaitMs ?? 8000))
  const pollIntervalMs = Math.max(10, Number(options.pollIntervalMs ?? 750))
  const fastStablePasses = Math.max(1, Number(options.fastStablePasses ?? 2))
  const patientStablePasses = Math.max(
    fastStablePasses,
    Number(options.patientStablePasses ?? options.requiredStablePasses ?? 3),
  )
  let previous = ''
  let stablePasses = 0
  let completionMode = 'timeout'
  let snapshot = reportReadinessSnapshot(requestedMetrics)
  while (Date.now() < deadline) {
    snapshot = reportReadinessSnapshot(requestedMetrics)
    const current = snapshot.text_fingerprint
    stablePasses = current && current === previous ? stablePasses + 1 : 0
    previous = current
    // 完整数据已经出现时不受 8 秒耐心等待约束。连续两次读取稳定是为了
    // 避免恰好撞上局部重绘；加载标记仍存在时绝不走快速返回。
    if (
      stablePasses >= fastStablePasses
      && !snapshot.likely_loading
      && snapshot.data_complete
    ) {
      completionMode = 'fast_complete'
      break
    }
    const patientWaitElapsed = Date.now() - startedAt >= patientWaitMs
    if (
      patientWaitElapsed
      && stablePasses >= patientStablePasses
      && !snapshot.likely_loading
      && snapshot.content_ready
    ) {
      completionMode = 'patient_partial'
      break
    }
    await delay(Math.min(pollIntervalMs, Math.max(0, deadline - Date.now())))
  }
  return {
    loading_marker_count: snapshot.loading_marker_count,
    numeric_token_count: snapshot.numeric_token_count,
    matched_requested_metric_count: snapshot.matched_requested_metric_count,
    requested_metric_value_count: snapshot.requested_metric_value_count,
    likely_loading: snapshot.likely_loading,
    content_ready: snapshot.content_ready,
    data_complete: snapshot.data_complete,
    completion_mode: completionMode,
    readiness_wait_ms: Math.max(0, Date.now() - startedAt),
    readiness_timed_out: Date.now() >= deadline,
    stable_passes: stablePasses,
  }
}

function reportReadinessSnapshot(requestedMetrics) {
  const text = visibleText().slice(0, 20000)
  const normalizedText = normalizeMetricText(text)
  const requested = Array.from(new Set((requestedMetrics || [])
    .map(normalizeMetricText)
    .filter(Boolean)))
  const matchedRequestedMetricCount = requested.filter(metric => normalizedText.includes(metric)).length
  const requestedMetricValueCount = requested.filter((metric) => {
    let searchFrom = 0
    while (searchFrom < normalizedText.length) {
      const metricIndex = normalizedText.indexOf(metric, searchFrom)
      if (metricIndex < 0) return false
      const contextStart = Math.max(0, metricIndex - 80)
      const contextEnd = Math.min(normalizedText.length, metricIndex + metric.length + 160)
      const context = normalizedText.slice(contextStart, metricIndex)
        + normalizedText.slice(metricIndex + metric.length, contextEnd)
      if (/\d/.test(context)) return true
      searchFrom = metricIndex + metric.length
    }
    return false
  }).length
  const loadingMarkerCount = (
    text.match(/(?:正在)?(?:数据)?(?:加载|载入|查询|计算|刷新)中(?:[.。…]*)|loading(?:[.。…]*)/ig) || []
  ).length
  const numericTokenCount = (text.match(/(?:^|\s|[^\w])[-+]?\d[\d,.]*(?:%|ms|s|k|m|b|万|亿)?(?=$|\s|[^\w])/ig) || []).length
  const structuredNumericRowCount = Array.from(document.querySelectorAll?.('table tr') || [])
    .slice(0, 400)
    .filter(row => /\d/.test(String(row.innerText || row.textContent || '')))
    .length
  const contentReady = structuredNumericRowCount > 0
    || requestedMetricValueCount > 0
    || (numericTokenCount >= 2 && text.length >= 100)
  const dataComplete = requested.length > 0
    ? requestedMetricValueCount >= requested.length
    : contentReady
  return {
    text_fingerprint: normalizedText.slice(0, 12000),
    loading_marker_count: loadingMarkerCount,
    numeric_token_count: numericTokenCount,
    matched_requested_metric_count: matchedRequestedMetricCount,
    requested_metric_value_count: requestedMetricValueCount,
    requested_metric_count: requested.length,
    structured_numeric_row_count: structuredNumericRowCount,
    likely_loading: loadingMarkerCount > 0,
    content_ready: contentReady,
    data_complete: dataComplete,
  }
}

function normalizeMetricText(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/tokens?/g, 'token')
    .replace(/[^a-z0-9\u3400-\u9fff]+/g, '')
}

function visibleText() {
  return String(document.body?.innerText || '')
    .replace(/\u0000/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{4,}/g, '\n\n\n')
    .trim()
}

function mergeSegments(segments) {
  const lines = []
  const seen = new Set()
  for (const segment of segments) {
    for (const line of segment.split('\n')) {
      const normalized = line.trim()
      if (!normalized || seen.has(normalized)) continue
      seen.add(normalized)
      lines.push(normalized)
    }
  }
  return lines.join('\n')
}

function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)) }
function extractionError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
