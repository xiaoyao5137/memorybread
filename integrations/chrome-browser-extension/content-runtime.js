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
  const interaction = await applyPageInteraction(
    job,
    Math.min(readinessDeadline, Date.now() + 12000),
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

  const internalScroll = await collectScrollableContainers(
    Math.min(deadline, Date.now() + 30000),
    Math.max(1, maxSegments - segments.length),
  )
  for (const text of internalScroll.segment_texts) {
    const fingerprint = text.slice(0, 2000) + text.slice(-2000)
    if (text && !seen.has(fingerprint)) {
      seen.add(fingerprint)
      segments.push(text)
    }
  }
  interaction.collection_status = internalScroll.complete ? 'complete' : 'partial'

  const relationPages = await collectRelationPages(
    Math.min(deadline, Date.now() + 20000),
  )
  for (const text of relationPages.page_texts) {
    const fingerprint = text.slice(0, 2000) + text.slice(-2000)
    if (text && !seen.has(fingerprint)) {
      seen.add(fingerprint)
      segments.push(text)
    }
  }
  const tables = relationPages.tables
  const tableText = tables.map(table => (
    table.map(row => row.filter(Boolean).join(' ')).filter(Boolean).join('\n')
  )).filter(Boolean).join('\n')
  const merged = (mergeSegments(segments) || tableText).slice(0, maxCharacters)
  const truncated = merged.length >= maxCharacters
    || !reachedEnd
    || !internalScroll.complete
    || relationPages.pagination.dataset_complete !== true
  return {
    status: truncated ? 'partial' : 'complete',
    title,
    url: location.href,
    content_text: merged,
    structured_data: {
      tables,
      requested_metrics: job.requested_metrics || [],
      interaction,
      interaction_result: interaction,
      scroll_collection: {
        adapter: 'semantic_scroll_containers.v1',
        container_count: internalScroll.container_count,
        segments_captured: internalScroll.segment_texts.length,
        complete: internalScroll.complete,
      },
      pagination: relationPages.pagination,
      page_state: readiness,
    },
    completeness: {
      status: truncated ? 'partial' : 'complete',
      reached_end: reachedEnd && internalScroll.complete,
      stable_passes: readiness.stable_passes,
      segment_count: segments.length,
      truncated,
    },
  }
}

async function collectScrollableContainers(deadline, maxSegments) {
  const candidates = Array.from(document.querySelectorAll('body *')).slice(0, 6000)
    .filter(node => {
      if (!elementUsable(node)) return false
      const vertical = Number(node.scrollHeight || 0) - Number(node.clientHeight || 0)
      const horizontal = Number(node.scrollWidth || 0) - Number(node.clientWidth || 0)
      const style = globalThis.getComputedStyle?.(node)
      // 文本省略号的 overflow:hidden 也有 scrollWidth，但不是报表滚动区。
      // 避免这些窄标签耗尽分段预算。
      return (vertical > 8 && (!style || /^(auto|scroll|overlay)$/.test(style.overflowY)))
        || (horizontal > 8 && (!style || /^(auto|scroll|overlay)$/.test(style.overflowX)))
    })
    .sort((left, right) => {
      const leftRange = Math.max(Number(left.scrollHeight || 0) - Number(left.clientHeight || 0), Number(left.scrollWidth || 0) - Number(left.clientWidth || 0))
      const rightRange = Math.max(Number(right.scrollHeight || 0) - Number(right.clientHeight || 0), Number(right.scrollWidth || 0) - Number(right.clientWidth || 0))
      return rightRange - leftRange
    })
    .slice(0, 12)
  const segmentTexts = []
  let remaining = Math.max(1, Number(maxSegments || 1))
  let complete = true
  for (const node of candidates) {
    if (Date.now() >= deadline || remaining <= 0) { complete = false; break }
    const originalTop = Number(node.scrollTop || 0)
    const originalLeft = Number(node.scrollLeft || 0)
    const topMaximum = Math.max(0, Number(node.scrollHeight || 0) - Number(node.clientHeight || 0))
    const leftMaximum = Math.max(0, Number(node.scrollWidth || 0) - Number(node.clientWidth || 0))
    const topStep = Math.max(1, Math.floor(Number(node.clientHeight || 1) * 0.85))
    const leftStep = Math.max(1, Math.floor(Number(node.clientWidth || 1) * 0.85))
    const positions = []
    for (let top = 0; top < topMaximum && positions.length < remaining; top += topStep) positions.push([Math.min(top, topMaximum), 0])
    if (topMaximum > 0 && !positions.some(item => item[0] === topMaximum)) positions.push([topMaximum, 0])
    for (let left = leftStep; left < leftMaximum && positions.length < remaining; left += leftStep) positions.push([topMaximum, Math.min(left, leftMaximum)])
    if (leftMaximum > 0 && !positions.some(item => item[1] === leftMaximum)) positions.push([topMaximum, leftMaximum])
    const scheduledPositions = positions.slice(0, remaining)
    let capturedPositions = 0
    for (const [top, left] of scheduledPositions) {
      node.scrollTop = top
      node.scrollLeft = left
      node.dispatchEvent?.(new Event('scroll', {bubbles: true}))
      const stable = await waitForScrollContent(node, Math.min(deadline, Date.now() + 5000))
      if (!stable) complete = false
      const text = mergeSegments([String(node.innerText || node.textContent || ''), visibleText()])
      if (text) segmentTexts.push(text)
      capturedPositions += 1
      remaining -= 1
      if (Date.now() >= deadline || remaining <= 0) break
    }
    if (capturedPositions < positions.length
        || Number(node.scrollTop || 0) < topMaximum - 3
        || Number(node.scrollLeft || 0) < leftMaximum - 3) complete = false
    node.scrollTop = originalTop
    node.scrollLeft = originalLeft
  }
  return {segment_texts: segmentTexts, container_count: candidates.length, complete}
}

async function waitForScrollContent(node, deadline) {
  let previous = ''
  let stablePasses = 0
  const started = Date.now()
  while (Date.now() < deadline) {
    await delay(Math.min(120, Math.max(0, deadline - Date.now())))
    const text = String(node.innerText || node.textContent || '')
    stablePasses = text && text === previous ? stablePasses + 1 : 0
    previous = text
    const loading = /(?:正在)?(?:数据)?(?:加载|载入|查询|计算|刷新|渲染)中|loading|rendering/i.test(text)
    if (!loading && stablePasses >= 2 && Date.now() - started >= 350) return true
  }
  return false
}

function captureRelationTables() {
  return Array.from(document.querySelectorAll('table,[role="table"],[role="grid"]'))
    .slice(0, 24)
    .map(table => {
      const nativeRows = table.rows ? Array.from(table.rows) : []
      const rows = nativeRows.length
        ? nativeRows
        : Array.from(table.querySelectorAll?.('[role="row"]') || [])
      return rows.slice(0, 500).map(row => {
        const nativeCells = row.cells ? Array.from(row.cells) : []
        const cells = nativeCells.length
          ? nativeCells
          : Array.from(row.querySelectorAll?.(
            '[role="columnheader"],[role="rowheader"],[role="cell"],[role="gridcell"]',
          ) || [])
        return cells.slice(0, 64).map(cell => String(cell.innerText || cell.textContent || '').trim())
      })
    })
    .filter(table => table.some(row => row.some(cell => cell)))
}

async function collectRelationPages(deadline, options = {}) {
  const maxPages = Math.max(1, Math.min(Number(options.maxPages || 25), 50))
  const tables = []
  const pageTexts = []
  const visited = new Set()
  let startedAtFirstPage = true
  let reachedLastPage = false
  let paginationDetected = false
  let totalRows = null
  let originalPageControl = null

  for (let pageIndex = 0; pageIndex < maxPages && Date.now() < deadline; pageIndex += 1) {
    const pageTables = captureRelationTables()
    tables.push(...pageTables)
    pageTexts.push(visibleText())
    const state = relationPaginationState()
    const capturedRows = pageTables.reduce(
      (count, table) => count + Math.max(0, table.length - 1),
      0,
    )
    if (!state) {
      totalRows = extractRelationTotalRows(pageTexts[pageTexts.length - 1])
      reachedLastPage = totalRows == null || capturedRows >= totalRows
      break
    }
    paginationDetected = true
    if (pageIndex === 0) {
      startedAtFirstPage = state.at_first_page
      originalPageControl = state.current_control
    }
    totalRows = state.total_rows ?? totalRows
    const pageKey = state.page_key || relationTablesFingerprint(pageTables)
    if (visited.has(pageKey)) break
    visited.add(pageKey)
    if (!state.next_control || state.next_disabled) {
      reachedLastPage = true
      break
    }
    const before = relationTablesFingerprint(pageTables)
    state.next_control.click()
    if (!await waitForRelationPageChange(before, deadline)) break
  }

  if (originalPageControl && visited.size > 1) {
    originalPageControl.click()
  }
  const capturedRowCount = tables.reduce(
    (count, table) => count + Math.max(0, table.length - 1),
    0,
  )
  const datasetComplete = paginationDetected
    ? startedAtFirstPage
      && reachedLastPage
      && (totalRows == null || capturedRowCount >= totalRows)
    : reachedLastPage && (totalRows == null || capturedRowCount >= totalRows)
  return {
    tables,
    page_texts: pageTexts,
    pagination: {
      adapter: 'semantic_dom_pagination.v1',
      pages_captured: Math.max(1, paginationDetected ? visited.size : 1),
      started_at_first_page: startedAtFirstPage,
      reached_last_page: reachedLastPage,
      dataset_complete: datasetComplete,
      captured_rows: capturedRowCount,
      total_rows: totalRows,
    },
  }
}

function relationPaginationState() {
  const tables = Array.from(document.querySelectorAll(
    'table,[role="table"],[role="grid"]',
  )).filter(elementUsable)
  if (!tables.length) return null
  let scopes = Array.from(document.querySelectorAll(
    'nav,[role="navigation"],[class*="pagination" i],[class*="pager" i]',
  )).filter(elementUsable).filter(scope => /\d/.test(
    String(scope.innerText || scope.textContent || ''),
  ))
  if (!scopes.length) return null
  const lastTable = tables[tables.length - 1]
  scopes = scopes.sort((left, right) => {
    const tableRect = lastTable.getBoundingClientRect?.() || {bottom: 0}
    const leftRect = left.getBoundingClientRect?.() || {top: 0}
    const rightRect = right.getBoundingClientRect?.() || {top: 0}
    return Math.abs(leftRect.top - tableRect.bottom) - Math.abs(rightRect.top - tableRect.bottom)
  })
  const scope = scopes[0]
  const controls = Array.from(scope.querySelectorAll?.('button,a,[role="button"]') || [])
    .filter(elementUsable)
  const current = controls.find(node => (
    String(node.getAttribute?.('aria-current') || '').toLowerCase() === 'page'
    || /(?:^|[\s_-])(?:active|selected|current)(?:[\s_-]|$)/i.test(String(node.className || ''))
  ))
  const previous = controls.find(node => relationControlKind(node) === 'previous')
  const next = controls.find(node => relationControlKind(node) === 'next')
  const scopeText = String(scope.innerText || scope.textContent || '').replace(/\s+/g, ' ').trim()
  return {
    current_control: current || null,
    next_control: next || null,
    next_disabled: !next || relationControlDisabled(next),
    at_first_page: !previous || relationControlDisabled(previous),
    page_key: String(current?.innerText || current?.textContent || '').trim() || scopeText.slice(0, 160),
    total_rows: extractRelationTotalRows(scopeText),
  }
}

function relationControlKind(node) {
  const label = [
    node.getAttribute?.('aria-label'),
    node.getAttribute?.('title'),
    node.innerText,
    node.textContent,
  ].map(value => String(value || '').replace(/\s+/g, ' ').trim()).find(Boolean) || ''
  if (/^(?:previous|prev|上一页|上页|‹|<|←)$/i.test(label)) return 'previous'
  if (/^(?:next|下一页|下页|›|>|→)$/i.test(label)) return 'next'
  return 'other'
}

function relationControlDisabled(node) {
  return Boolean(
    node?.disabled
    || node?.getAttribute?.('disabled') != null
    || String(node?.getAttribute?.('aria-disabled') || '').toLowerCase() === 'true'
    || /(?:^|\s)(?:disabled|is-disabled)(?:\s|$)/i.test(String(node?.className || ''))
  )
}

function extractRelationTotalRows(value) {
  const text = String(value || '').replace(/,/g, '')
  const match = text.match(/(?:共|total)\s*(\d+)\s*(?:条|项|rows?|records?)?/i)
    || text.match(/(\d+)\s*(?:条|项|rows?|records?)\s*(?:记录|数据)?/i)
  return match ? Number(match[1]) : null
}

function relationTablesFingerprint(tables) {
  return JSON.stringify((tables || []).map(table => ({
    rows: table.length,
    first: table[0],
    last: table[table.length - 1],
  })))
}

async function waitForRelationPageChange(previousFingerprint, deadline) {
  while (Date.now() < deadline) {
    await delay(Math.min(250, Math.max(0, deadline - Date.now())))
    const current = relationTablesFingerprint(captureRelationTables())
    if (current && current !== previousFingerprint) return true
  }
  return false
}

async function applyPageInteraction(job, deadline) {
  const actions = []
  const plan = resolveInteractionPlan(job)
  const result = {
    schema_version: 'memorybread.page-interaction-result.v1',
    status: 'completed',
    plan_source: job.interaction_plan ? 'explicit' : 'objective_compatibility',
    steps: [],
    view_status: 'verified',
    collection_status: 'not_started',
    metric_results: [],
    incidental_claims: [],
    actions,
    expected_period: {
      start: normalizeCalendarDate(job.expected_period_start),
      end: normalizeCalendarDate(job.expected_period_end),
    },
    period_verified: false,
  }
  for (const step of plan.steps.slice(0, 12)) {
    if (Date.now() >= deadline) {
      result.steps.push(failedInteractionStep(step, 'INTERACTION_PAGE_UNSTABLE', 0))
      result.status = 'failed'
      result.view_status = 'unverified'
      break
    }
    const stepResult = await executeInteractionStep(step, result, deadline)
    result.steps.push(stepResult)
    if (stepResult.status !== 'completed') {
      result.status = 'failed'
      result.view_status = 'unverified'
      break
    }
    if (step.action === 'scroll_collect' || step.action === 'paginate') {
      result.collection_status = 'pending'
    } else if (step.action === 'collect' && result.collection_status === 'not_started') {
      result.collection_status = 'pending'
    }
  }
  return result
}

function resolveInteractionPlan(job) {
  const explicit = job?.interaction_plan
  if (explicit?.schema_version === 'memorybread.page-interaction-plan.v1'
      && explicit.safety_mode === 'read_only'
      && Array.isArray(explicit.steps)) return explicit
  const steps = []
  const tabIndex = requestedTabIndex(job?.objective)
  if (tabIndex != null) {
    steps.push({
      id: 'activate-target-view',
      action: 'activate',
      target: {role_hints: ['tab', 'navigation_item'], ordinal: tabIndex + 1},
      postconditions: (job?.requested_metrics || []).length
        ? [{kind: 'text_present', any: job.requested_metrics}]
        : [{kind: 'selected', target_ref: 'self'}],
    })
  }
  const start = normalizeCalendarDate(job?.expected_period_start)
  const end = normalizeCalendarDate(job?.expected_period_end)
  if (start && end) {
    steps.push({
      id: 'set-period', action: 'set_date_range', value: {start, end},
      postconditions: [{kind: 'date_range_displayed', source: 'page'}],
    })
  }
  steps.push({id: 'collect-scroll-containers', action: 'scroll_collect', scope: 'all_scroll_containers'})
  steps.push({id: 'collect', action: 'collect', scope: 'page'})
  return {schema_version: 'memorybread.page-interaction-plan.v1', safety_mode: 'read_only', steps}
}

async function executeInteractionStep(step, interaction, deadline) {
  const action = String(step?.action || '')
  if (!['wait_for', 'activate', 'set_value', 'select_option', 'set_date_range', 'expand', 'scroll_collect', 'paginate', 'collect'].includes(action)) {
    return failedInteractionStep(step, 'INTERACTION_PLAN_INVALID', 0)
  }
  if (action === 'set_date_range') {
    const verified = await applyDateRangeStep(step, interaction, deadline)
    return verified
      ? completedInteractionStep(step, 1)
      : failedInteractionStep(step, 'INTERACTION_POSTCONDITION_FAILED', 1)
  }
  if (action === 'scroll_collect' || action === 'paginate' || action === 'collect') {
    return completedInteractionStep(step, 0)
  }

  let resolution = resolveSemanticTarget(step.target || {})
  // document complete 不代表 SPA 已挂载业务控件。只在“尚未找到”时等待，
  // 不试点歧义候选，也不重复点击已经执行过的动作。
  while (resolution.error === 'INTERACTION_TARGET_NOT_FOUND' && Date.now() < deadline) {
    await delay(Math.min(150, Math.max(0, deadline - Date.now())))
    resolution = resolveSemanticTarget(step.target || {})
  }
  if (resolution.error) return failedInteractionStep(step, resolution.error, resolution.candidate_count)
  const node = resolution.node
  const label = interactionNodeLabel(node)
  if (interactionActionBlocked(label)) {
    return failedInteractionStep(step, 'INTERACTION_ACTION_BLOCKED', resolution.candidate_count)
  }
  if (action === 'wait_for') {
    const verified = await waitForInteractionPostconditions(step, node, deadline)
    return verified
      ? completedInteractionStep(step, resolution.candidate_count)
      : failedInteractionStep(step, 'INTERACTION_POSTCONDITION_FAILED', resolution.candidate_count)
  }
  if (action === 'set_value') {
    setGenericControlValue(node, step.value)
  } else if (action === 'select_option') {
    selectGenericOption(node, step.value)
  } else {
    node.click?.()
    interaction.actions.push({kind: action, label: label.slice(0, 80)})
  }
  const verified = await waitForInteractionPostconditions(step, node, deadline)
  if (verified) return completedInteractionStep(step, resolution.candidate_count)

  // 页面可能在首次点击后重建 DOM。只允许重新发现一个不同节点再试一次。
  resolution = resolveSemanticTarget(step.target || {}, node)
  if (!resolution.error && resolution.node && !interactionActionBlocked(interactionNodeLabel(resolution.node))) {
    if (action === 'set_value') setGenericControlValue(resolution.node, step.value)
    else if (action === 'select_option') selectGenericOption(resolution.node, step.value)
    else resolution.node.click?.()
    if (await waitForInteractionPostconditions(step, resolution.node, deadline)) {
      return {...completedInteractionStep(step, resolution.candidate_count), attempts: 2}
    }
  }
  return failedInteractionStep(step, 'INTERACTION_POSTCONDITION_FAILED', resolution.candidate_count, 2)
}

function completedInteractionStep(step, candidateCount) {
  return {id: String(step?.id || ''), action: String(step?.action || ''), status: 'completed', attempts: 1, candidate_count: candidateCount}
}

function failedInteractionStep(step, errorCode, candidateCount, attempts = 1) {
  return {id: String(step?.id || ''), action: String(step?.action || ''), status: 'failed', attempts, candidate_count: candidateCount, error_code: errorCode}
}

function interactionNodeRole(node) {
  const explicit = String(node?.getAttribute?.('role') || '').toLowerCase()
  if (explicit) return explicit === 'menuitem' ? 'navigation_item' : explicit
  // 目录项的文字也可能带 tab 类名，不能把 treeitem 的后代当作页签。
  if (node?.closest?.('[role="treeitem"]')?.getAttribute?.('role') === 'treeitem') return 'treeitem'
  const tag = String(node?.tagName || '').toLowerCase()
  const evidence = `${node?.className || ''} ${node?.getAttribute?.('aria-current') || ''}`.toLowerCase()
  if (tag === 'button') return 'button'
  if (tag === 'select' || /combobox|select/.test(evidence)) return 'combobox'
  if (tag === 'input') return 'input'
  if (/tab/.test(evidence)
      && !/(?:tabs?)[_-]+(?:container|content|body|pane|panel|header|nav|box|wrap|scroll)(?:[_\s-]|$)/.test(evidence)) return 'tab'
  if (/menu|nav|sidebar/.test(evidence) || node?.getAttribute?.('aria-current')) return 'navigation_item'
  return tag === 'a' ? 'link' : 'interactive'
}

function interactionNodeLabel(node) {
  const nodeId = String(node?.id || node?.getAttribute?.('id') || '')
  const connectedLabel = nodeId
    ? Array.from(document.querySelectorAll('label')).find(label => String(label.htmlFor || label.getAttribute?.('for') || '') === nodeId)
    : null
  const wrappingLabel = node?.closest?.('label')
  return [
    node?.getAttribute?.('aria-label'), node?.getAttribute?.('title'),
    node?.getAttribute?.('placeholder'), connectedLabel?.innerText,
    wrappingLabel?.innerText, node?.innerText, node?.textContent, node?.value,
  ].map(value => String(value || '').replace(/\s+/g, ' ').trim()).find(Boolean) || ''
}

function normalizeInteractionLabel(value) {
  return String(value || '').toLowerCase().replace(/tokens?/g, 'token').replace(/[^a-z0-9\u3400-\u9fff]+/g, '')
}

function resolveSemanticTarget(target, excludedNode = null) {
  const selector = [
    '[role="tab"]', '[role="button"]', '[role="menuitem"]', '[role="option"]',
    '[role="combobox"]', 'button', 'a', 'input', 'select', '[aria-selected]',
    '[aria-current]', '[aria-haspopup]', '[tabindex]', '[class*="tab" i]',
    '[class*="menu" i]', '[class*="nav" i]', '[class*="sidebar" i]',
  ].join(',')
  const nodes = Array.from(new Set(Array.from(document.querySelectorAll(selector))))
    .filter(node => node !== excludedNode && elementUsable(node) && interactionNodeLabel(node))
    .slice(0, 2000)
  const roles = (target.role_hints || []).map(value => String(value).toLowerCase())
  const labels = (target.labels || []).map(normalizeInteractionLabel).filter(Boolean)
  const withinLabels = (target.within?.labels || []).map(normalizeInteractionLabel).filter(Boolean)
  let candidates = nodes.map(node => {
    const role = interactionNodeRole(node)
    const label = normalizeInteractionLabel(interactionNodeLabel(node))
    let score = roles.length ? (roles.includes(role) ? 30 : -20) : 0
    for (const expected of labels) {
      // 标签命中不能抹掉 role 证据。看板目录中的 treeitem 与顶部 tab
      // 经常同名；将两类证据相加后，目标 role 才能稳定胜出。
      if (label === expected) score += 100
      else if (label.includes(expected) || expected.includes(label)) score += 70
    }
    if (withinLabels.length) {
      let ancestor = node.parentElement
      let matched = false
      for (let depth = 0; ancestor && depth < 6; depth += 1, ancestor = ancestor.parentElement) {
        const text = normalizeInteractionLabel(ancestor.innerText || ancestor.textContent)
        if (withinLabels.some(expected => text.includes(expected))) { matched = true; break }
      }
      score += matched ? 25 : -25
    }
    return {node, role, label, score}
  }).filter(item => item.score >= (labels.length ? 50 : roles.length ? 0 : 10))

  // CSS 只能提供候选线索。含有多个不同控件的容器不是控件；同一控件的
  // 包装层、标题和文字节点也只能计数一次。仅合并存在包含关系的同名
  // 节点，不能把两个独立的同名按钮合并，从而掩盖真实歧义。
  candidates = candidates.filter(item => !candidates.some(other => (
    other.node !== item.node && item.node.contains?.(other.node)
    && other.label !== item.label && other.role === item.role
  )))
  candidates.sort((a, b) => b.score - a.score
    || Number(Boolean(b.node.getAttribute?.('role'))) - Number(Boolean(a.node.getAttribute?.('role')))
    || Number(interactionNodeSelected(b.node)) - Number(interactionNodeSelected(a.node)))
  const controls = []
  for (const item of candidates) {
    if (controls.some(other => other.label === item.label && other.role === item.role
      && (other.node.contains?.(item.node) || item.node.contains?.(other.node)))) continue
    controls.push(item)
  }
  candidates = controls

  if (target.ordinal != null && !labels.length) {
    const ordinal = Math.max(1, Number(target.ordinal))
    const groups = new Map()
    for (const item of candidates) {
      // 从父层开始，以同级控件的共同容器分组。closest('[class*=tabs]')
      // 会命中 tabs__item 自身，把每个包装层误当成一个独立页签组。
      let group = item.node.parentElement
      while (group && !candidates.some(other => other.node !== item.node
        && other.role === item.role
        && (other.node.parentElement === group || group.contains?.(other.node)))) {
        if (['tablist', 'navigation', 'menu'].includes(String(group.getAttribute?.('role') || ''))) break
        // 单项控件组不能越过自身边界与页面其他区域拼成一个“多项组”。
        if (/(?:^|[\s_-])(?:tabs|tablist|navigation|menu|nav)(?:[\s_-]|$)/i.test(String(group.className || ''))) break
        group = group.parentElement
      }
      if (!group) continue
      if (!groups.has(group)) groups.set(group, [])
      if (!groups.get(group).some(existing => existing.node === item.node)) groups.get(group).push(item)
    }
    const eligible = Array.from(groups.entries()).map(([group, rawItems]) => {
      const ordered = rawItems.sort((left, right) => {
        const a = left.node.getBoundingClientRect?.() || {top: 0, left: 0}
        const b = right.node.getBoundingClientRect?.() || {top: 0, left: 0}
        return Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left
      })
      const uniqueItems = ordered
      const selectedCount = uniqueItems.filter(item => interactionNodeSelected(item.node)).length
      return {
        group,
        items: uniqueItems,
        score: Math.max(...uniqueItems.map(item => item.score))
          + (uniqueItems.every(item => roles.includes(item.role)) ? 20 : 0)
          + (selectedCount ? 40 : 0),
      }
    }).filter(group => group.items.length >= ordinal)
      .sort((a, b) => b.score - a.score || b.items.length - a.items.length)
    if (!eligible.length) return {error: 'INTERACTION_TARGET_NOT_FOUND', candidate_count: candidates.length}
    if (eligible.length > 1 && eligible[0].score === eligible[1].score && eligible[0].group !== eligible[1].group) {
      return {error: 'INTERACTION_TARGET_AMBIGUOUS', candidate_count: candidates.length}
    }
    return {node: eligible[0].items[ordinal - 1].node, candidate_count: candidates.length}
  }
  candidates.sort((left, right) => right.score - left.score)
  if (!candidates.length) return {error: 'INTERACTION_TARGET_NOT_FOUND', candidate_count: 0}
  if (candidates.length > 1 && candidates[0].score - candidates[1].score < 10) {
    const sameControl = candidates[0].node.contains?.(candidates[1].node)
      || candidates[1].node.contains?.(candidates[0].node)
    if (!sameControl) return {error: 'INTERACTION_TARGET_AMBIGUOUS', candidate_count: candidates.length}
  }
  return {node: candidates[0].node, candidate_count: candidates.length}
}

function interactionActionBlocked(label) {
  return /(?:删除|保存|提交|发送|审批|创建|购买|上传|下载|发布|delete|save|submit|send|approve|create|purchase|upload|download|publish)/i.test(String(label || ''))
}

async function waitForInteractionPostconditions(step, node, deadline) {
  const conditions = Array.isArray(step.postconditions) ? step.postconditions : []
  if (!conditions.length) return true
  let previous = ''
  let stablePasses = 0
  while (Date.now() < deadline) {
    const text = visibleText()
    const fingerprint = normalizeMetricText(text).slice(0, 12000)
    stablePasses = fingerprint && fingerprint === previous ? stablePasses + 1 : 0
    previous = fingerprint
    const satisfied = conditions.every(condition => {
      const kind = String(condition.kind || '')
      if (kind === 'selected') return interactionNodeSelected(node)
      if (kind === 'expanded') return interactionNodeExpanded(node)
      if (kind === 'text_present') {
        const haystack = normalizeMetricText(text)
        return (condition.any || []).some(value => haystack.includes(normalizeMetricText(value)))
      }
      if (kind === 'value_displayed') {
        const displayed = [
          node?.value,
          ...Array.from(node?.selectedOptions || []).map(option => option?.innerText || option?.textContent),
          interactionNodeLabel(node),
        ].map(normalizeInteractionLabel).join(' ')
        return displayed.includes(normalizeInteractionLabel(step.value))
      }
      if (kind === 'date_range_displayed') return true // 由 set_date_range 的专用验证处理。
      if (kind === 'data_stable') return stablePasses >= Math.max(1, Number(condition.minimum_stable_passes || 2))
      return false
    })
    if (satisfied) return true
    await delay(Math.min(200, Math.max(0, deadline - Date.now())))
  }
  return false
}

function interactionNodeSelected(node) {
  return String(node?.getAttribute?.('aria-selected') || '').toLowerCase() === 'true'
    || String(node?.getAttribute?.('aria-current') || '').toLowerCase() === 'page'
    || /(?:^|[\s_-])(?:is[-_])?(?:active|selected|current)(?:[\s_-]|$)/i.test(String(node?.className || ''))
}

function interactionNodeExpanded(node) {
  return String(node?.getAttribute?.('aria-expanded') || '').toLowerCase() === 'true'
    || /(?:^|\s)(?:is-)?expanded(?:\s|$)/i.test(String(node?.className || ''))
}

function setGenericControlValue(node, value) {
  const next = typeof value === 'object' && value != null ? String(value.value ?? '') : String(value ?? '')
  const prototype = globalThis.HTMLInputElement?.prototype
  const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, 'value')
  if (descriptor?.set) descriptor.set.call(node, next)
  else node.value = next
  for (const eventName of ['input', 'change', 'blur']) node.dispatchEvent?.(new Event(eventName, {bubbles: true}))
}

function selectGenericOption(node, value) {
  const wanted = normalizeInteractionLabel(typeof value === 'object' && value != null ? value.value : value)
  const option = Array.from(node.options || node.querySelectorAll?.('[role="option"],option') || [])
    .find(item => normalizeInteractionLabel(interactionNodeLabel(item)) === wanted)
  if (option) {
    if ('value' in node && 'value' in option) node.value = option.value
    else option.click?.()
    node.dispatchEvent?.(new Event('change', {bubbles: true}))
  }
}

async function applyDateRangeStep(step, interaction, deadline) {
  const expectedStart = normalizeCalendarDate(step?.value?.start || interaction.expected_period.start)
  const expectedEnd = normalizeCalendarDate(step?.value?.end || interaction.expected_period.end)
  if (!expectedStart || !expectedEnd) return false
  const inputs = dateInputCandidates()
  if (inputs.length >= 2) {
    setDateInputValue(inputs[0], expectedStart)
    setDateInputValue(inputs[1], expectedEnd)
    interaction.actions.push({kind: 'period_range', start: expectedStart, end: expectedEnd})
  } else if (inputs.length === 1) {
    setDateInputValue(inputs[0], expectedEnd)
    interaction.actions.push({kind: 'period_single', date: expectedEnd})
  } else {
    // 一些 BI 把日期范围渲染为一个 div，而不是 input。若页面已经显示
    // 目标起止日期，直接确认当前筛选并执行查询，避免再次打开日历后只
    // 选择结束日而破坏原有范围。
    const displays = dateDisplayCandidates()
    const currentDates = extractCalendarDates(displays.length
      ? displays[0].innerText || displays[0].textContent
      : visibleText())
    if (currentDates.includes(expectedStart) && currentDates.includes(expectedEnd)) {
      interaction.actions.push({kind: 'period_current', start: expectedStart, end: expectedEnd})
      interaction.period_verified = true
      clickPeriodApplyControl(interaction)
      return true
    }
    const trigger = displays[0]
    if (trigger) {
      const isRange = extractCalendarDates(trigger.innerText || trigger.textContent).length >= 2
      trigger.click?.()
      interaction.actions.push({kind: 'period_open'})
      if (isRange) {
        // 范围组件往往在展开后才挂载两个输入框；没有可编辑输入框时，
        // 只点击带完整日期证据的日历单元格，不按日号或坐标猜测。
        let rangeInputs = []
        let startCell = null
        while (Date.now() < deadline) {
          rangeInputs = dateInputCandidates().filter(node => !node.readOnly && !node.disabled)
          startCell = calendarDateCell(expectedStart)
          if (rangeInputs.length >= 2 || startCell) break
          await delay(Math.min(120, Math.max(0, deadline - Date.now())))
        }
        if (rangeInputs.length >= 2) {
          setDateInputValue(rangeInputs[0], expectedStart)
          setDateInputValue(rangeInputs[1], expectedEnd)
        } else if (startCell && !interactionActionBlocked(interactionNodeLabel(startCell))) {
          startCell.click?.()
          // 点击开始日后组件可能重建日历，结束日必须重新定位。
          let endCell = null
          while (Date.now() < deadline && !endCell) {
            endCell = calendarDateCell(expectedEnd)
            if (!endCell) await delay(Math.min(120, Math.max(0, deadline - Date.now())))
          }
          if (endCell && !interactionActionBlocked(interactionNodeLabel(endCell))) endCell.click?.()
        }
        const panel = dateRangeConfirmationScope(rangeInputs[0] || startCell)
        if (panel) clickPeriodApplyControl(interaction, panel, true)
        while (Date.now() < deadline) {
          const currentTrigger = trigger.isConnected === false ? dateDisplayCandidates()[0] : trigger
          const dates = extractCalendarDates(currentTrigger?.innerText || currentTrigger?.textContent)
          if (dates[0] === expectedStart && dates[1] === expectedEnd) {
            interaction.period_verified = true
            interaction.actions.push({kind: 'period_range', start: expectedStart, end: expectedEnd})
            clickPeriodApplyControl(interaction)
            return true
          }
          await delay(Math.min(120, Math.max(0, deadline - Date.now())))
        }
        interaction.actions.push({kind: 'period_unconfirmed', start: expectedStart, end: expectedEnd})
        return false
      }
      let target = null
      while (Date.now() < deadline && !target) {
        target = calendarDateCell(expectedEnd)
        if (!target) await delay(Math.min(120, Math.max(0, deadline - Date.now())))
      }
      if (target && !interactionActionBlocked(interactionNodeLabel(target))) {
        target.click?.()
        interaction.actions.push({kind: 'period_single', date: expectedEnd})
        const confirmed = await waitForPeriodConfirmation(
          expectedStart, expectedEnd, [], deadline,
        )
        interaction.period_verified = confirmed
        if (!confirmed) {
          interaction.actions.push({kind: 'period_unconfirmed', start: expectedStart, end: expectedEnd})
        }
        return confirmed
      }
      interaction.actions.push({kind: 'period_unconfirmed', start: expectedStart, end: expectedEnd})
      return false
    }
    interaction.actions.push({kind: 'period_control_missing', start: expectedStart, end: expectedEnd})
    // 无周期控件的实时总览并没有可执行失败的页面操作。保留
    // period_verified=false，由 Core Engine 再按“本次采集时点”策略约束，
    // 不能在扩展层把有效的无筛选报表提前判成交互失败。
    return true
  }
  clickPeriodApplyControl(interaction)
  const confirmed = await waitForPeriodConfirmation(expectedStart, expectedEnd, inputs, deadline)
  const values = inputs.map(node => normalizeCalendarDate(node.value || node.getAttribute?.('value')))
  interaction.period_verified = inputs.length >= 2
    ? values[0] === expectedStart && values[1] === expectedEnd
    : values[0] === expectedEnd || confirmed
  if (!interaction.period_verified) interaction.actions.push({kind: 'period_unconfirmed', start: expectedStart, end: expectedEnd})
  return interaction.period_verified
}

function dateRangeConfirmationScope(node) {
  // 输入框自身也可能叫 date-picker/date-editor。沿祖先寻找实际包含
  // 确认按钮的最小范围，不能在单个输入框包装层内结束查找。
  for (let scope = node?.parentElement; scope && !['BODY', 'HTML'].includes(scope.tagName); scope = scope.parentElement) {
    const buttons = Array.from(scope.querySelectorAll?.('button,[role="button"]') || [])
    if (buttons.some(button => elementUsable(button)
      && /^(?:应用|确定|确认|apply|ok)$/i.test(interactionNodeLabel(button)))) return scope
  }
  return null
}

function clickPeriodApplyControl(interaction, scope = document, confirmationOnly = false) {
  const labels = confirmationOnly
    ? /^(?:应用|确定|确认|apply|ok)$/i
    : /^(?:查询|应用|搜索|确定|确认|apply|search|ok)$/i
  const apply = Array.from(scope.querySelectorAll('button,[role="button"]'))
    .filter(elementUsable)
    .find(node => labels.test(interactionNodeLabel(node)))
  if (apply && !interactionActionBlocked(interactionNodeLabel(apply))) {
    apply.click?.()
    interaction.actions.push({kind: 'period_apply', label: interactionNodeLabel(apply).slice(0, 80)})
  }
}

function requestedTabIndex(objective) {
  const text = String(objective || '').toLowerCase()
  const match = text.match(/(?:第\s*)?(\d+)\s*(?:个\s*)?tab/)
  if (match) return Math.max(0, Number(match[1]) - 1)
  const chinese = text.match(/第\s*([一二两三四五六七八九十]+)\s*个?\s*tab/)
  if (!chinese) return null
  const digits = {一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9}
  const value = chinese[1].includes('十')
    ? (digits[chinese[1].split('十')[0]] || 1) * 10 + (digits[chinese[1].split('十')[1]] || 0)
    : digits[chinese[1]]
  return value ? value - 1 : null
}

function dateInputCandidates() {
  const candidates = Array.from(document.querySelectorAll('input')).filter(node => {
    if (!elementUsable(node)) return false
    const evidence = [
      node.value,
      node.getAttribute?.('value'),
      node.getAttribute?.('placeholder'),
      node.getAttribute?.('aria-label'),
      node.getAttribute?.('type'),
      node.parentElement?.innerText,
    ].join(' ')
    return /20\d{2}[-/.年]\d{1,2}/.test(evidence)
      || /(?:日期|开始|结束|date|period)/i.test(evidence)
  })
  const valued = candidates.filter(node => normalizeCalendarDate(node.value || node.getAttribute?.('value')))
  return valued.length ? valued : candidates
}

function dateDisplayCandidates() {
  // 只有可交互控件才可能是日期选择器入口。正文、图表标题和历史说明中
  // 也经常出现日期；把任意 body 子节点当入口会点击普通文本，并把本来
  // 没有周期控件的实时总览误报为“周期切换未确认”。
  return Array.from(document.querySelectorAll(
    'button,[role="button"],[aria-haspopup],[class*="date-picker" i],[class*="datepicker" i],[class*="date-range" i],[class*="daterange" i]',
  ))
    .filter(elementUsable)
    .filter(node => {
      const text = String(node.innerText || node.textContent || '').trim()
      return text.length <= 80 && extractCalendarDates(text).length > 0 && node.childElementCount <= 4
    })
    .sort((left, right) => {
      const leftArea = rectangleArea(left)
      const rightArea = rectangleArea(right)
      return leftArea - rightArea
    })
}

function calendarDateCell(targetDate) {
  return Array.from(document.querySelectorAll(
    '[data-date],[aria-label],[title],[role="gridcell"],button,td',
  )).filter(node => elementUsable(node) && !node.disabled
    && node.getAttribute?.('aria-disabled') !== 'true').find(node => {
    const evidence = [
      node.getAttribute?.('data-date'),
      node.getAttribute?.('aria-label'),
      node.getAttribute?.('title'),
      node.innerText,
      node.textContent,
    ].join(' ')
    return extractCalendarDates(evidence).includes(targetDate)
      || normalizeCalendarDate(evidence) === targetDate
  })
}

function setDateInputValue(node, value) {
  const current = String(node.value || node.getAttribute?.('value') || '')
  const formatted = current.includes('.') ? value.replace(/-/g, '.')
    : current.includes('/') ? value.replace(/-/g, '/') : value
  const prototype = globalThis.HTMLInputElement?.prototype
  const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, 'value')
  if (descriptor?.set) descriptor.set.call(node, formatted)
  else node.value = formatted
  for (const eventName of ['input', 'change', 'blur']) {
    node.dispatchEvent?.(new Event(eventName, {bubbles: true}))
  }
}

async function waitForPeriodConfirmation(start, end, inputs, deadline) {
  while (Date.now() < deadline) {
    const values = inputs.map(node => normalizeCalendarDate(node.value || node.getAttribute?.('value')))
    const confirmed = inputs.length >= 2
      ? values[0] === start && values[1] === end
      : inputs.length === 1 && values[0] === end
    if (confirmed) return true
    const dates = extractCalendarDates(visibleText())
    if (dates.length > 0 && dates.every(date => date >= start && date <= end)) return true
    await delay(Math.min(200, Math.max(0, deadline - Date.now())))
  }
  return false
}

function normalizeCalendarDate(value) {
  const match = String(value || '')
    .replace(/[年/.]/g, '-')
    .replace(/月/g, '-')
    .replace(/日/g, '')
    .match(/(20\d{2})-(\d{1,2})-(\d{1,2})/)
  if (!match) return ''
  return `${match[1]}-${String(match[2]).padStart(2, '0')}-${String(match[3]).padStart(2, '0')}`
}

function extractCalendarDates(value) {
  const dates = []
  const pattern = /20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?/g
  for (const match of String(value || '').match(pattern) || []) {
    const normalized = normalizeCalendarDate(match)
    if (normalized && !dates.includes(normalized)) dates.push(normalized)
  }
  return dates
}

function elementUsable(node) {
  if (!node) return false
  const style = globalThis.getComputedStyle?.(node)
  if (style && (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0)) return false
  const rect = node.getBoundingClientRect?.()
  return !rect || (rect.width > 4 && rect.height > 4)
}

function rectangleArea(node) {
  const rect = node.getBoundingClientRect?.()
  return rect ? Math.max(0, rect.width * rect.height) : Number.MAX_SAFE_INTEGER
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
    // 业务接口已经明确返回稳定错误页时立即交给 Core 失败关闭并触发
    // 下一采集通道，不必把完整页面等待预算耗在不会自行恢复的壳层上。
    if (
      stablePasses >= fastStablePasses
      && snapshot.terminal_error_marker_count > 0
    ) {
      completionMode = 'terminal_error'
      break
    }
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
    terminal_error_marker_count: snapshot.terminal_error_marker_count,
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
  const terminalErrorMarkerCount = text.split(/\n+/).filter(line => {
    const normalized = String(line || '').trim()
    return /^(?:网络错误|网络异常|请求失败|请求错误|系统异常|服务异常|加载失败|加载异常|failed to load|network error)$/i.test(normalized)
      || /^(?:获取|读取|查询|刷新)[^\n]{0,40}失败$/i.test(normalized)
  }).length
  const numericTokenCount = (text.match(/(?:^|\s|[^\w])[-+]?\d[\d,.]*(?:%|ms|s|k|m|b|万|亿)?(?=$|\s|[^\w])/ig) || []).length
  const structuredNumericRowCount = Array.from(document.querySelectorAll?.('table tr') || [])
    .slice(0, 400)
    .filter(row => /\d/.test(String(row.innerText || row.textContent || '')))
    .length
  const contentReady = terminalErrorMarkerCount === 0 && (structuredNumericRowCount > 0
    || requestedMetricValueCount > 0
    || (numericTokenCount >= 2 && text.length >= 100))
  const dataComplete = requested.length > 0
    ? requestedMetricValueCount >= requested.length
    : contentReady
  return {
    text_fingerprint: normalizedText.slice(0, 12000),
    loading_marker_count: loadingMarkerCount,
    terminal_error_marker_count: terminalErrorMarkerCount,
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
  const blocks = []
  for (const segment of segments) {
    const normalized = segment.split('\n').map(line => line.trim()).filter(Boolean).join('\n')
    if (!normalized) continue
    // 只能合并完整的重复快照；按行去重会删除不同指标卡重复的日期、
    // 单位和值，导致数值错配或错误继承页面筛选周期。
    const block = `\n${normalized}\n`
    if (blocks.some(existing => existing.includes(block))) continue
    for (let index = blocks.length - 1; index >= 0; index -= 1) {
      if (block.includes(blocks[index])) blocks.splice(index, 1)
    }
    blocks.push(block)
  }
  return blocks.map(block => block.trim()).join('\n\n')
}

function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)) }
function extractionError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
