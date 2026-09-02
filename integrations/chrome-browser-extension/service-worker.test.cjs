const {readFileSync} = require('node:fs')
const {runInNewContext} = require('node:vm')
const {test} = require('node:test')
const assert = require('node:assert/strict')
const {join} = require('node:path')

function loadServiceWorker() {
  const posted = []
  let disconnectListener = null
  const nativePort = {
    postMessage(message) { posted.push(message) },
    onMessage: {addListener() {}},
    onDisconnect: {addListener(listener) { disconnectListener = listener }},
  }
  const context = {
    chrome: {
      runtime: {
        getManifest: () => ({version: '0.2.2'}),
        connectNative: () => nativePort,
        onInstalled: {addListener() {}},
        onStartup: {addListener() {}},
      },
      alarms: {
        create() {},
        onAlarm: {addListener() {}},
      },
      tabs: {remove: async () => {}},
    },
    console,
    Date,
    Error,
    Math,
    Number,
    Promise,
    String,
    URL,
    Map,
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval() {},
  }
  const source = readFileSync(join(__dirname, 'service-worker.js'), 'utf8')
  runInNewContext(source, context)
  posted.length = 0
  return {
    context,
    posted,
    disconnect: () => disconnectListener(),
  }
}

test('任务执行卡死后会释放 busy 并立即恢复轮询', async () => {
  const {context, posted} = loadServiceWorker()
  context.executeJob = () => new Promise(() => {})

  await context.handleNativeResponse({
    job: {
      browser_job_id: 'stuck-job',
      deadline_ms: Date.now() - 10000,
    },
  })

  const failed = posted.find(message => message.type === 'result')
  assert.equal(failed.result.status, 'failed')
  assert.equal(failed.result.error_code, 'JOB_EXECUTION_TIMEOUT')
  assert.ok(posted.some(message => message.type === 'poll'))

  posted.length = 0
  context.executeJob = async job => ({
    browser_job_id: job.browser_job_id,
    status: 'complete',
    title: '报表',
  })
  await context.handleNativeResponse({
    job: {
      browser_job_id: 'next-job',
      deadline_ms: Date.now() + 10000,
    },
  })

  const completed = posted.find(message => message.type === 'result')
  assert.equal(completed.result.browser_job_id, 'next-job')
  assert.equal(completed.result.status, 'complete')
})

test('Native Host 断开后释放 busy，重连可继续领取任务', async () => {
  const {context, posted, disconnect} = loadServiceWorker()
  context.executeJob = () => new Promise(() => {})
  void context.handleNativeResponse({
    job: {
      browser_job_id: 'orphaned-job',
      deadline_ms: Date.now() - 10000,
    },
  })

  disconnect()
  context.connectNative()
  posted.length = 0
  context.executeJob = async job => ({
    browser_job_id: job.browser_job_id,
    status: 'complete',
  })
  await context.handleNativeResponse({
    job: {
      browser_job_id: 'reconnected-job',
      deadline_ms: Date.now() + 10000,
    },
  })

  const completed = posted.find(message => message.type === 'result')
  assert.equal(completed.result.browser_job_id, 'reconnected-job')
  assert.equal(completed.result.status, 'complete')
})
