const {readFileSync} = require('node:fs')
const {runInNewContext} = require('node:vm')
const {test} = require('node:test')
const assert = require('node:assert/strict')
const {join} = require('node:path')

function loadRuntime(initialText) {
  let bodyText = initialText
  const context = {
    chrome: {runtime: {onMessage: {addListener() {}}}},
    document: {
      body: {
        get innerText() { return bodyText },
      },
      querySelectorAll() { return [] },
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
  }
  const source = readFileSync(join(__dirname, 'content-runtime.js'), 'utf8')
  runInNewContext(source, context)
  return {
    context,
    setText(value) { bodyText = value },
  }
}

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
