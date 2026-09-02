const {readFileSync} = require('node:fs')
const {runInNewContext} = require('node:vm')
const {test} = require('node:test')
const assert = require('node:assert/strict')
const {join} = require('node:path')

function loadRuntime(initialText, querySelectorAll = () => []) {
  let bodyText = initialText
  const context = {
    chrome: {runtime: {onMessage: {addListener() {}}}},
    document: {
      body: {
        get innerText() { return bodyText },
      },
      querySelectorAll,
    },
    globalThis: {},
    setTimeout,
    clearTimeout,
    Date,
    Promise,
    Set,
    String,
    Number,
    Math,
    Event: class Event {
      constructor(type, options = {}) { this.type = type; this.bubbles = Boolean(options.bubbles) }
    },
  }
  const source = readFileSync(join(__dirname, 'content-runtime.js'), 'utf8')
  runInNewContext(source, context)
  return {
    context,
    setText(value) { bodyText = value },
  }
}

test('滚动快照合并保留每张指标卡的重复日期、数值和单位', () => {
  const {context} = loadRuntime('')
  const first = '输入Token\n2026-08-26至2026-08-26\n100\n亿'
  const second = '输出Token\n2026-08-26至2026-08-26\n100\n亿'
  const full = `${first}\n${second}`
  assert.equal(context.mergeSegments([first, full, full, second]), full)
  assert.equal(context.mergeSegments([first, second]), `${first}\n\n${second}`)
})

test('通用周期交互按日期输入结构填写范围且不依赖业务列名', async () => {
  function input(value, placeholder) {
    return {
      value,
      parentElement: {innerText: ''},
      getAttribute(name) {
        return name === 'placeholder' ? placeholder : name === 'type' ? 'text' : ''
      },
    }
  }
  const start = input('2026-08-01', '开始日期')
  const end = input('2026-08-07', '结束日期')
  const runtime = loadRuntime('通用报表', selector => selector === 'input' ? [start, end] : [])

  const interaction = await runtime.context.applyPageInteraction({
    objective: '读取当前报表',
    expected_period_start: '2026-08-17',
    expected_period_end: '2026-08-23',
  }, Date.now() + 100)

  assert.equal(start.value, '2026-08-17')
  assert.equal(end.value, '2026-08-23')
  assert.equal(interaction.period_verified, true)
  assert.equal(interaction.actions[0].kind, 'period_range')
})

test('只有日期触发器的单日看板会打开日历并选择周期结束日', async () => {
  let selectedDate = '2026-08-24'
  let calendarOpen = false
  let runtime
  const trigger = {
    innerText: selectedDate,
    textContent: selectedDate,
    childElementCount: 1,
    className: 'generic-date-trigger',
    getAttribute() { return null },
    getBoundingClientRect() { return {width: 120, height: 32} },
    click() { calendarOpen = true },
  }
  const target = {
    innerText: '23',
    textContent: '23',
    className: '',
    getAttribute(name) { return name === 'data-date' ? '2026-08-23' : null },
    getBoundingClientRect() { return {width: 32, height: 32} },
    click() {
      selectedDate = '2026-08-23'
      trigger.innerText = selectedDate
      trigger.textContent = selectedDate
      calendarOpen = false
      runtime.setText(`通用报表\n选择日期 ${selectedDate}\n利用率 76%`)
    },
  }
  runtime = loadRuntime('通用报表\n选择日期 2026-08-24\n利用率 75%', selector => {
    if (selector === 'input') return []
    if (selector.startsWith('button')) return [trigger]
    if (selector.startsWith('[data-date]')) return calendarOpen ? [target] : []
    return []
  })

  const interaction = await runtime.context.applyPageInteraction({
    objective: '读取上周报表',
    expected_period_start: '2026-08-17',
    expected_period_end: '2026-08-23',
  }, Date.now() + 300)

  assert.equal(selectedDate, '2026-08-23')
  assert.equal(interaction.status, 'completed')
  assert.equal(interaction.period_verified, true)
  assert.deepEqual(
    JSON.parse(JSON.stringify(interaction.actions.map(item => item.kind))),
    ['period_open', 'period_single'],
  )
})

test('没有日期控件的即时总览会明确标记而不猜测业务周期', async () => {
  const runtime = loadRuntime('当前项目总览\n项目数 20')

  const interaction = await runtime.context.applyPageInteraction({
    objective: '读取当前报表',
    expected_period_start: '2026-08-17',
    expected_period_end: '2026-08-23',
  }, Date.now() + 100)

  assert.equal(interaction.period_verified, false)
  assert.equal(interaction.actions[0].kind, 'period_control_missing')
  assert.equal(interaction.status, 'completed')
  assert.equal(interaction.view_status, 'verified')
})

test('已显示目标日期范围的非 input 筛选器会直接查询而不破坏范围', async () => {
  let queryClicks = 0
  const query = {
    tagName: 'BUTTON', innerText: '查询', textContent: '查询', className: '',
    getAttribute() { return null },
    getBoundingClientRect() { return {width: 80, height: 30} },
    click() { queryClicks += 1 },
  }
  const runtime = loadRuntime(
    '通用运营看板\n日期 2026-08-17 至 2026-08-23\n查询',
    selector => selector === 'input' ? [] : selector.startsWith('button') ? [query] : [],
  )

  const interaction = await runtime.context.applyPageInteraction({
    objective: '读取目标周报表',
    expected_period_start: '2026-08-17',
    expected_period_end: '2026-08-23',
  }, Date.now() + 150)

  assert.equal(interaction.period_verified, true)
  assert.equal(queryClicks, 1)
  assert.deepEqual(
    JSON.parse(JSON.stringify(interaction.actions.map(item => item.kind))),
    ['period_current', 'period_apply'],
  )
})

test('正文中的普通日期文本不会被当成日期选择器入口', async () => {
  const runtime = loadRuntime('实时总览\n模型发布日期 2026-07-01', selector => {
    assert.equal(selector.includes('body *'), false)
    return []
  })

  const interaction = await runtime.context.applyPageInteraction({
    objective: '读取当前报表',
    expected_period_start: '2026-08-17',
    expected_period_end: '2026-08-23',
  }, Date.now() + 100)

  assert.equal(interaction.period_verified, false)
  assert.equal(interaction.actions[0].kind, 'period_control_missing')
})

test('自定义日期范围容器展开后填写双输入框并确认起止日期', async () => {
  let open = false
  let queryClicks = 0
  const trigger = semanticNode('2026-08-18 至 2026-08-24', 'custom-date-picker-content')
  trigger.childElementCount = 2
  trigger.click = () => { open = true }
  const input = (value, placeholder) => ({
    value, getAttribute(name) { return name === 'placeholder' ? placeholder : null },
    closest() { return panel },
  })
  const start = input('2026-08-18', '开始日期')
  const end = input('2026-08-24', '结束日期')
  const confirm = semanticNode('确定', '')
  confirm.click = () => { trigger.innerText = `${start.value} 至 ${end.value}`; open = false }
  const panel = {querySelectorAll() { return [confirm] }}
  // 真实组件的 closest(date-picker) 只会命中输入框包装层，确认按钮在外层。
  const wrapper = {parentElement: panel, querySelectorAll() { return [] }}
  start.parentElement = wrapper
  end.parentElement = wrapper
  start.closest = () => wrapper
  end.closest = () => wrapper
  const query = semanticNode('查询', '')
  query.click = () => { queryClicks += 1 }
  const runtime = loadRuntime('报表正文含其他日期 2026-01-01', selector => {
    if (selector === 'input') return open ? [start, end] : []
    if (selector.includes('[class*="date-picker"')) return [trigger]
    if (selector.startsWith('button')) return [query]
    return []
  })
  const result = await runtime.context.applyPageInteraction({expected_period_start: '2026-08-17', expected_period_end: '2026-08-23'}, Date.now() + 200)

  assert.equal(result.status, 'completed')
  assert.equal(result.period_verified, true)
  assert.equal(trigger.innerText, '2026-08-17 至 2026-08-23')
  assert.equal(queryClicks, 1)
})

test('无输入框的日期范围必须分别选择开始日和结束日且不能用正文日期冒充确认', async () => {
  for (const updateDisplay of [true, false]) {
    let open = false
    const selected = []
    const trigger = semanticNode('2026-08-18 至 2026-08-24', 'custom-date-range')
    trigger.childElementCount = 2
    trigger.click = () => { open = true }
    const cell = date => ({
      innerText: date.slice(-2),
      getAttribute(name) { return name === 'data-date' ? date : null },
      click() {
        selected.push(date)
        if (selected.length === 2 && updateDisplay) trigger.innerText = selected.join(' 至 ')
      },
    })
    const start = cell('2026-08-17')
    const end = cell('2026-08-23')
    const runtime = loadRuntime('历史正文 2026-08-17 至 2026-08-23', selector => {
      if (selector.includes('[class*="date-picker"')) return [trigger]
      if (selector.startsWith('[data-date]')) return open ? [start, end] : []
      return []
    })
    // 直接验证范围动作；正文中恰好有目标日期不能证明筛选器已变更。
    const interaction = {actions: [], expected_period: {}, period_verified: false}
    const result = await runtime.context.applyDateRangeStep({value: {start: '2026-08-17', end: '2026-08-23'}}, interaction, Date.now() + 150)
    assert.deepEqual(selected, ['2026-08-17', '2026-08-23'])
    assert.equal(result, updateDisplay)
    assert.equal(interaction.period_verified, updateDisplay)
  }
})

test('结构化计划可通过导航语义切换无 role=tab 的第二项并验证目标文本', async () => {
  const group = {innerText: '总览 用量统计', parentElement: null}
  let runtime
  const item = (label, onClick) => ({
    tagName: 'DIV',
    className: 'dashboard-navigation-item',
    innerText: label,
    textContent: label,
    parentElement: group,
    getAttribute() { return null },
    getBoundingClientRect() { return {width: 120, height: 30, top: 0, left: label === '总览' ? 0 : 130} },
    closest(selector) { return selector === 'label' ? null : group },
    click: onClick,
  })
  const first = item('总览', () => {})
  const second = item('用量统计', () => runtime.setText('用量统计\n输入Tokens 128亿'))
  runtime = loadRuntime('总览', selector => selector.includes('[role="tab"]') ? [first, second] : [])

  const interaction = await runtime.context.applyPageInteraction({
    interaction_plan: {
      schema_version: 'memorybread.page-interaction-plan.v1',
      safety_mode: 'read_only',
      steps: [{
        id: 'target-view', action: 'activate',
        target: {role_hints: ['tab', 'navigation_item'], ordinal: 2},
        postconditions: [{kind: 'text_present', any: ['输入Token']}],
      }],
    },
  }, Date.now() + 200)

  assert.equal(interaction.status, 'completed')
  assert.equal(interaction.view_status, 'verified')
  assert.equal(interaction.steps[0].candidate_count, 2)
})

test('多个同分语义候选会失败关闭而不是逐个试点', async () => {
  let clickCount = 0
  const candidate = label => ({
    tagName: 'BUTTON', innerText: label, textContent: label, className: '', parentElement: {},
    getAttribute(name) { return name === 'role' ? 'button' : null },
    getBoundingClientRect() { return {width: 100, height: 30, top: 0, left: 0} },
    click() { clickCount += 1 },
  })
  const runtime = loadRuntime('查询入口', selector => selector.includes('[role="tab"]')
    ? [candidate('查询数据'), candidate('查询报表')] : [])

  const interaction = await runtime.context.applyPageInteraction({
    interaction_plan: {
      schema_version: 'memorybread.page-interaction-plan.v1', safety_mode: 'read_only',
      steps: [{
        id: 'query', action: 'activate',
        target: {role_hints: ['button'], labels: ['查询']},
      }],
    },
  }, Date.now() + 100)

  assert.equal(interaction.status, 'failed')
  assert.equal(interaction.steps[0].error_code, 'INTERACTION_TARGET_AMBIGUOUS')
  assert.equal(clickCount, 0)
})

test('业务控件异步挂载后再执行动作而不是在页面壳阶段报未找到', async () => {
  let ready = false
  let clicks = 0
  const group = semanticNode('总览 用量', 'tabs', null, 'tablist')
  const first = semanticNode('总览', 'tab', group, 'tab')
  const second = semanticNode('用量', 'tab', group, 'tab')
  second.click = () => { clicks += 1 }
  const runtime = loadRuntime('页面壳', selector => ready && selector.includes('[role="tab"]') ? [first, second] : [])
  const timer = setTimeout(() => { ready = true }, 20)
  try {
    const result = await runtime.context.executeInteractionStep({id: 'target-view', action: 'activate', target: {role_hints: ['tab'], ordinal: 2}}, {actions: []}, Date.now() + 600)
    assert.equal(result.status, 'completed')
    assert.equal(clicks, 1)
  } finally { clearTimeout(timer) }
})

test('控件始终未挂载时在预算内失败且不点击其他控件', async () => {
  const runtime = loadRuntime('页面壳')
  const result = await runtime.context.executeInteractionStep({id: 'target-view', action: 'activate', target: {role_hints: ['tab'], ordinal: 2}}, {actions: []}, Date.now() + 40)
  assert.equal(result.status, 'failed')
  assert.equal(result.error_code, 'INTERACTION_TARGET_NOT_FOUND')
})

function semanticNode(label, className, parentElement = null, role = '') {
  const node = {
    tagName: 'DIV', innerText: label, textContent: label, className, parentElement,
    getAttribute(name) { return name === 'role' ? role : null },
    getBoundingClientRect() { return {width: 120, height: 30, top: 0, left: 0} },
    contains(other) {
      for (let current = other; current; current = current.parentElement) {
        if (current === this) return true
      }
      return false
    },
    closest(selector) {
      for (let current = this; current; current = current.parentElement) {
        if (selector === 'label') return null
        if (selector === '[role="treeitem"]') {
          if (current.getAttribute?.('role') === 'treeitem') return current
        } else if (/tabs|menu|nav|sidebar/i.test(current.className || '')
          || ['tablist', 'navigation'].includes(current.getAttribute?.('role'))) return current
      }
      return null
    },
  }
  return node
}

test('多层页签包装与目录并存时序号按真实控件组计算', () => {
  const root = semanticNode('筛选 总览 用量 明细', '')
  const tree = semanticNode('筛选 总览 用量 明细', 'menu-tree', root, 'tree')
  const nodes = [tree]
  const filters = semanticNode('筛选 日期 查询', 'tab-container', root)
  const filterHeader = semanticNode('筛选', 'draggable-tabs__box', filters)
  nodes.push(filters, filterHeader,
    semanticNode('筛选', 'draggable-tabs__item is-active', filterHeader),
    semanticNode('日期 查询', 'tab-container__body', filters))
  for (const label of ['筛选', '总览', '用量', '明细']) {
    const entry = semanticNode(label, 'tree-node', tree, 'treeitem')
    nodes.push(entry, semanticNode(label, 'label tab', entry))
  }
  const hiddenSheet = semanticNode('用量', 'tabs__nav', root, 'tablist')
  nodes.push(hiddenSheet, semanticNode('用量', 'tabs__item is-active', hiddenSheet, 'tab'))
  const group = semanticNode('总览 用量 明细', 'draggable-tabs__box', root)
  nodes.push(group)
  let expected
  for (const [index, label] of ['总览', '用量', '明细'].entries()) {
    const selected = index === 1 ? ' is-active' : ''
    const wrapper = semanticNode(label, `draggable-tabs__item${selected}`, group)
    const item = semanticNode(label, `area-tab${selected}`, wrapper)
    const text = semanticNode(label, 'tab-name', item)
    for (const n of [wrapper, item, text]) n.getBoundingClientRect = () => ({width: 100, height: 30, top: 0, left: index * 110})
    nodes.push(wrapper, item, text)
    if (index === 1) expected = wrapper
  }
  const runtime = loadRuntime('报表', selector => selector.includes('[role="tab"]') ? nodes : [])
  const resolved = runtime.context.resolveSemanticTarget({role_hints: ['tab', 'navigation_item'], ordinal: 2})

  assert.equal(resolved.error, undefined)
  assert.equal(resolved.node, expected)
})

test('独立页签组同分时仍拒绝序号选择', () => {
  const nodes = []
  for (let index = 0; index < 2; index += 1) {
    const group = semanticNode('总览 明细', 'tabs', null, 'tablist')
    nodes.push(group, semanticNode('总览', 'tab active', group, 'tab'), semanticNode('明细', 'tab', group, 'tab'))
  }
  const runtime = loadRuntime('报表', selector => selector.includes('[role="tab"]') ? nodes : [])
  assert.equal(runtime.context.resolveSemanticTarget({role_hints: ['tab'], ordinal: 2}).error, 'INTERACTION_TARGET_AMBIGUOUS')
})

test('两个独立单项页签组不能跨区域拼出第二项', () => {
  const root = semanticNode('总览 明细', '')
  const nodes = []
  for (const label of ['总览', '明细']) {
    const group = semanticNode(label, 'custom-tabs__box', root)
    nodes.push(semanticNode(label, 'custom-tab active', group))
  }
  const runtime = loadRuntime('报表', selector => selector.includes('[role="tab"]') ? nodes : [])
  assert.equal(runtime.context.resolveSemanticTarget({role_hints: ['tab'], ordinal: 2}).error, 'INTERACTION_TARGET_NOT_FOUND')
})

test('标签优先于序号且独立同名控件不会因文字相同被合并', () => {
  const group = semanticNode('总览 明细', 'tabs', null, 'tablist')
  const first = semanticNode('总览', 'tab active', group, 'tab')
  const second = semanticNode('明细', 'tab', group, 'tab')
  const nodes = [first, second]
  const runtime = loadRuntime('报表', selector => selector.includes('[role="tab"]') ? nodes : [])
  assert.equal(runtime.context.resolveSemanticTarget({role_hints: ['tab'], labels: ['明细'], ordinal: 2}).node, second)
  nodes.push(semanticNode('明细', 'tab', group, 'tab'))
  assert.equal(runtime.context.resolveSemanticTarget({role_hints: ['tab'], labels: ['明细']}).error, 'INTERACTION_TARGET_AMBIGUOUS')
})

test('同名目录项与页签并存时按目标 role 选择页签', async () => {
  let clicked = ''
  const node = (label, role, group, className = '') => ({
    tagName: 'DIV', innerText: label, textContent: label, className, parentElement: group,
    getAttribute(name) { return name === 'role' ? role : null },
    getBoundingClientRect() { return {width: 120, height: 30, top: 0, left: 0} },
    closest(selector) { return selector === 'label' ? null : group },
    click() { clicked = role },
  })
  const sidebar = {innerText: '用量统计', parentElement: null}
  const tabs = {innerText: 'GPU资源池 用量统计', parentElement: null}
  const runtime = loadRuntime('稳定页面', selector => selector.includes('[role="tab"]') ? [
    node('用量统计', 'treeitem', sidebar),
    node('用量统计', 'tab', tabs, 'dashboard-tab-active'),
  ] : [])

  const interaction = await runtime.context.applyPageInteraction({
    interaction_plan: {
      schema_version: 'memorybread.page-interaction-plan.v1', safety_mode: 'read_only',
      steps: [{
        id: 'target-view', action: 'activate',
        target: {role_hints: ['tab', 'navigation_item'], labels: ['用量统计']},
        postconditions: [{kind: 'selected', target_ref: 'self'}],
      }],
    },
  }, Date.now() + 150)

  assert.equal(interaction.status, 'completed')
  assert.equal(clicked, 'tab')
})

test('通用下拉框按关联标签定位并验证所选值', async () => {
  const options = [
    {value: 'north', innerText: '华北', textContent: '华北', getAttribute() { return null }},
    {value: 'east', innerText: '华东', textContent: '华东', getAttribute() { return null }},
  ]
  const select = {
    id: 'region-filter', tagName: 'SELECT', value: 'north', options, className: '', parentElement: {},
    get selectedOptions() { return options.filter(option => option.value === this.value) },
    getAttribute(name) { return name === 'role' ? 'combobox' : name === 'id' ? this.id : null },
    getBoundingClientRect() { return {width: 120, height: 30, top: 0, left: 0} },
    querySelectorAll() { return options }, dispatchEvent() {},
  }
  const label = {
    htmlFor: 'region-filter', innerText: '区域',
    getAttribute(name) { return name === 'for' ? this.htmlFor : null },
  }
  const runtime = loadRuntime('区域 华北', selector => {
    if (selector === 'label') return [label]
    return selector.includes('[role="tab"]') ? [select] : []
  })

  const interaction = await runtime.context.applyPageInteraction({
    interaction_plan: {
      schema_version: 'memorybread.page-interaction-plan.v1', safety_mode: 'read_only',
      steps: [{
        id: 'region', action: 'select_option', value: '华东',
        target: {role_hints: ['combobox'], labels: ['区域']},
        postconditions: [{kind: 'value_displayed', target_ref: 'self', any: ['华东']}],
      }],
    },
  }, Date.now() + 200)

  assert.equal(select.value, 'east')
  assert.equal(interaction.status, 'completed')
})

test('写操作风险词命中的控件即使命中标签也不会执行', async () => {
  let clicked = false
  const button = {
    tagName: 'BUTTON', innerText: '提交审批', textContent: '提交审批', className: '', parentElement: {},
    getAttribute(name) { return name === 'role' ? 'button' : null },
    getBoundingClientRect() { return {width: 100, height: 30, top: 0, left: 0} },
    click() { clicked = true },
  }
  const runtime = loadRuntime('审批页面', selector => selector.includes('[role="tab"]') ? [button] : [])

  const interaction = await runtime.context.applyPageInteraction({
    interaction_plan: {
      schema_version: 'memorybread.page-interaction-plan.v1', safety_mode: 'read_only',
      steps: [{id: 'unsafe', action: 'activate', target: {labels: ['提交审批']}}],
    },
  }, Date.now() + 100)

  assert.equal(interaction.steps[0].error_code, 'INTERACTION_ACTION_BLOCKED')
  assert.equal(clicked, false)
})

test('内部虚拟滚动容器逐段采集并恢复原位置', async () => {
  let scrollTop = 0
  const container = {
    clientHeight: 100, scrollHeight: 300, clientWidth: 200, scrollWidth: 200,
    get scrollTop() { return scrollTop },
    set scrollTop(value) { scrollTop = value },
    scrollLeft: 0,
    get innerText() { return `虚拟列表第${Math.floor(scrollTop / 100) + 1}段` },
    get textContent() { return this.innerText },
    getBoundingClientRect() { return {width: 200, height: 100} },
  }
  const runtime = loadRuntime('虚拟列表', selector => selector === 'body *' ? [container] : [])

  const collected = await runtime.context.collectScrollableContainers(Date.now() + 2500, 10)

  assert.equal(collected.complete, true)
  assert.ok(collected.segment_texts.some(text => text.includes('虚拟列表第3段')))
  assert.equal(scrollTop, 0)
})

test('滚动后等待懒加载数值并排除文本省略号伪滚动区', async () => {
  let scrollTop = 0
  let text = '顶部汇总 10'
  let timer
  const container = {
    clientHeight: 100, scrollHeight: 200, clientWidth: 200, scrollWidth: 200,
    get scrollTop() { return scrollTop },
    set scrollTop(value) {
      scrollTop = value
      clearTimeout(timer)
      if (value > 0) {
        text = '渲染中...'
        timer = setTimeout(() => { text = '输入Tokens 123亿 输出Tokens 45亿' }, 2700)
      }
    },
    scrollLeft: 0,
    get innerText() { return text },
    getBoundingClientRect() { return {width: 200, height: 100} },
  }
  const clipped = {clientHeight: 18, scrollHeight: 18, clientWidth: 7, scrollWidth: 10000,
    getBoundingClientRect() { return {width: 7, height: 18} }}
  const runtime = loadRuntime('报表', selector => selector === 'body *' ? [clipped, container] : [])
  runtime.context.globalThis.getComputedStyle = node => ({overflowX: 'hidden', overflowY: node === container ? 'auto' : 'hidden', opacity: '1'})
  try {
    const result = await runtime.context.collectScrollableContainers(Date.now() + 8000, 4)
    assert.equal(result.container_count, 1)
    assert.ok(result.segment_texts.some(value => value.includes('输入Tokens 123亿')))
    assert.equal(result.complete, true)
    assert.equal(scrollTop, 0)
  } finally { clearTimeout(timer) }
})

test('报表壳层稳定后仍等待异步指标出现', async () => {
  const runtime = loadRuntime('Token 数据报表\n加载中…')
  setTimeout(() => runtime.setText('Token 数据报表\n专有环境输入Tokens\n128.5亿'), 35)

  const readiness = await runtime.context.waitForReportReadiness(
    Date.now() + 300,
    ['专有环境输入Token'],
    {minimumWaitMs: 20, pollIntervalMs: 10, requiredStablePasses: 2},
  )

  assert.equal(readiness.likely_loading, false)
  assert.equal(readiness.matched_requested_metric_count, 1)
  assert.equal(readiness.requested_metric_value_count, 1)
  assert.equal(readiness.content_ready, true)
  assert.equal(readiness.data_complete, true)
  assert.equal(readiness.completion_mode, 'fast_complete')
  assert.equal(readiness.readiness_timed_out, false)
  assert.ok(readiness.readiness_wait_ms >= 35)
})

test('目标指标和值已经完整出现时无需等待八秒', async () => {
  const runtime = loadRuntime('Token 数据报表\n专有环境输入Tokens\n128.5亿')

  const readiness = await runtime.context.waitForReportReadiness(
    Date.now() + 300,
    ['专有环境输入Token'],
    {patientWaitMs: 200, pollIntervalMs: 10, fastStablePasses: 2},
  )

  assert.equal(readiness.data_complete, true)
  assert.equal(readiness.completion_mode, 'fast_complete')
  assert.equal(readiness.readiness_timed_out, false)
  assert.ok(readiness.readiness_wait_ms < 100)
})

test('仅部分指标出现时进入耐心等待后再交给结构校验', async () => {
  const runtime = loadRuntime('Token 数据报表\n专有环境输入Tokens\n128.5亿')

  const readiness = await runtime.context.waitForReportReadiness(
    Date.now() + 300,
    ['专有环境输入Token', '共享环境输出Token'],
    {
      patientWaitMs: 60,
      pollIntervalMs: 10,
      fastStablePasses: 2,
      patientStablePasses: 3,
    },
  )

  assert.equal(readiness.content_ready, true)
  assert.equal(readiness.data_complete, false)
  assert.equal(readiness.completion_mode, 'patient_partial')
  assert.equal(readiness.readiness_timed_out, false)
  assert.ok(readiness.readiness_wait_ms >= 60)
})

test('自然语言偏好零字面命中时仍按页面数字进入结构校验', async () => {
  const runtime = loadRuntime(
    'GPU 项目用量管理\n在用项目数 102\n总卡数 1803.59\n年化总成本 12178.4万元\n'
      + '项目列表包含卡数、成本、收益与 ROI 等完整字段。'.repeat(4),
  )

  const readiness = await runtime.context.waitForReportReadiness(
    Date.now() + 300,
    ['请生成GPU成本优化周报', '业务项目投入成本最高的10个项目的数据'],
    {
      patientWaitMs: 50,
      pollIntervalMs: 10,
      fastStablePasses: 2,
      patientStablePasses: 3,
    },
  )

  assert.equal(readiness.requested_metric_value_count, 0)
  assert.equal(readiness.content_ready, true)
  assert.equal(readiness.data_complete, false)
  assert.equal(readiness.completion_mode, 'patient_partial')
  assert.equal(readiness.readiness_timed_out, false)
})

test('只有稳定页面壳且目标指标未出现时等待到上限', async () => {
  const runtime = loadRuntime('Token 数据报表\n导航\n帮助中心')

  const readiness = await runtime.context.waitForReportReadiness(
    Date.now() + 80,
    ['专有环境输入Token'],
    {minimumWaitMs: 20, pollIntervalMs: 10, requiredStablePasses: 2},
  )

  assert.equal(readiness.content_ready, false)
  assert.equal(readiness.readiness_timed_out, true)
  assert.ok(readiness.readiness_wait_ms >= 70)
})

test('报表业务错误页不会被误判为可用数据', async () => {
  const runtime = loadRuntime(
    'GPU 使用情况一览\n数据时效：T-2（每天 10:30 更新）\n总体概览\n获取日期列表失败',
  )

  const readiness = await runtime.context.waitForReportReadiness(
    Date.now() + 80,
    ['GPU 利用率'],
    {minimumWaitMs: 20, pollIntervalMs: 10, requiredStablePasses: 2},
  )

  assert.equal(readiness.terminal_error_marker_count, 1)
  assert.equal(readiness.content_ready, false)
  assert.equal(readiness.data_complete, false)
  assert.equal(readiness.completion_mode, 'terminal_error')
  assert.equal(readiness.readiness_timed_out, false)
})

test('通用分页总行数解析不依赖表格字段或业务名称', () => {
  const runtime = loadRuntime('任意报表')

  assert.equal(runtime.context.extractRelationTotalRows('共 1,024 条'), 1024)
  assert.equal(runtime.context.extractRelationTotalRows('Total 88 records'), 88)
  assert.equal(runtime.context.extractRelationTotalRows('分页 1 / 3'), null)
  assert.equal(runtime.context.relationControlKind({
    innerText: '›', textContent: '›', getAttribute() { return null },
  }), 'next')
})

test('无分页控件但总行数大于已捕获行数时不得声明全集完整', async () => {
  const cells = values => values.map(value => ({innerText: value}))
  const rows = [
    {cells: cells(['对象', '度量'])},
    {cells: cells(['甲', '10'])},
    {cells: cells(['乙', '20'])},
  ]
  const table = {rows}
  const runtime = loadRuntime(
    '任意报表\n共 5 条',
    selector => selector.includes('table') ? [table] : [],
  )

  const collected = await runtime.context.collectRelationPages(Date.now() + 100)

  assert.equal(collected.pagination.captured_rows, 2)
  assert.equal(collected.pagination.total_rows, 5)
  assert.equal(collected.pagination.dataset_complete, false)
})

test('通用关系分页逐页保留完整行并在结束后恢复原页', async () => {
  let page = 1
  const cells = values => values.map(value => ({innerText: value}))
  const table = {
    get rows() {
      const values = page === 1 ? [['甲', '10'], ['乙', '20']] : [['丙', '30'], ['丁', '40']]
      return [{cells: cells(['对象', '度量'])}, ...values.map(value => ({cells: cells(value)}))]
    },
  }
  const control = (label, attributes = {}, click = () => {}) => ({
    innerText: label,
    className: attributes.className || '',
    get disabled() { return Boolean(attributes.disabled?.()) },
    getAttribute(name) {
      if (name === 'aria-current') return attributes.current?.() ? 'page' : null
      if (name === 'aria-disabled') return attributes.disabled?.() ? 'true' : null
      return null
    },
    click,
  })
  const first = control('1', {current: () => page === 1}, () => { page = 1 })
  const second = control('2', {current: () => page === 2}, () => { page = 2 })
  const previous = control('‹', {disabled: () => page === 1}, () => { page = 1 })
  const next = control('›', {disabled: () => page === 2}, () => { page = 2 })
  const scope = {
    innerText: '1 2 共 4 条',
    querySelectorAll() { return [previous, first, second, next] },
  }
  const runtime = loadRuntime('任意报表\n共 4 条', selector => {
    if (selector.startsWith('table')) return [table]
    if (selector.startsWith('nav')) return [scope]
    return []
  })

  const collected = await runtime.context.collectRelationPages(Date.now() + 1000)

  assert.equal(collected.pagination.pages_captured, 2)
  assert.equal(collected.pagination.captured_rows, 4)
  assert.equal(collected.pagination.total_rows, 4)
  assert.equal(collected.pagination.dataset_complete, true)
  assert.deepEqual(
    JSON.parse(JSON.stringify(collected.tables.map(tableRows => tableRows[1][0]))),
    ['甲', '丙'],
  )
  assert.equal(page, 1)
})

test('分页当前页可由复合 active 类名稳定识别', () => {
  const controls = ['‹', '1', '2', '›'].map((label, index) => ({
    innerText: label,
    textContent: label,
    className: index === 2 ? 'page-btn page-btn-active' : 'page-btn',
    disabled: index === 0,
    getAttribute() { return null },
    getBoundingClientRect() { return {width: 32, height: 32} },
  }))
  const table = {
    getBoundingClientRect() { return {width: 600, height: 400, bottom: 400} },
  }
  const scope = {
    innerText: '共 40 条 1 2',
    getBoundingClientRect() { return {width: 200, height: 40, top: 410} },
    querySelectorAll() { return controls },
  }
  const runtime = loadRuntime('任意报表', selector => {
    if (selector.startsWith('table')) return [table]
    if (selector.startsWith('nav')) return [scope]
    return []
  })

  const state = runtime.context.relationPaginationState()

  assert.equal(state.page_key, '2')
  assert.equal(state.current_control, controls[2])
})
