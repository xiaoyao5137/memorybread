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
        getManifest: () => JSON.parse(readFileSync(join(__dirname, 'manifest.json'), 'utf8')),
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

test('取消消息在 busy 期间也关闭对应后台页且不启动新任务', async () => {
  const {context} = loadServiceWorker()
  const closed = []
  context.closeActiveJobTab = async id => closed.push(id)
  runInNewContext('busy = true', context)
  await context.handleNativeResponse({cancelled_job_ids:['cancel-me']})
  assert.deepEqual(closed,['cancel-me'])
  assert.equal(runInNewContext('cancelledJobIds.has("cancel-me")',context),true)
})

test('取消挂起的浏览器调用立即释放队列，迟到结果不会再次发布', async () => {
  const {context, posted} = loadServiceWorker()
  let finishOld
  context.executeJob = () => new Promise(resolve => { finishOld = resolve })
  const pending = context.handleNativeResponse({job:{browser_job_id:'cancel-stuck',deadline_ms:Date.now()+60000}})
  await context.handleNativeResponse({cancelled_job_ids:['cancel-stuck']})
  let timer
  try {
    await Promise.race([pending, new Promise((_,reject) => {timer=setTimeout(() => reject(new Error('cancel remained busy')),500)})])
  } finally { clearTimeout(timer) }
  assert.equal(runInNewContext('busy',context),false)
  assert.equal(runInNewContext('jobCancellationWaiters.size',context),0)
  assert.equal(posted.find(m=>m.type==='result').result.error_code,'SOURCE_REFRESH_CANCELLED')
  context.executeJob = async job => ({browser_job_id:job.browser_job_id,status:'complete'})
  await context.handleNativeResponse({job:{browser_job_id:'after-cancel',deadline_ms:Date.now()+10000}})
  finishOld({browser_job_id:'cancel-stuck',status:'complete'})
  await Promise.resolve()
  context.sendProgress({browser_job_id:'cancel-stuck'},{stage:'late'})
  assert.equal(posted.filter(m=>m.type==='result' && m.result.browser_job_id==='cancel-stuck').length,1)
  assert.equal(posted.filter(m=>m.type==='progress').length,0)
  assert.ok(posted.some(m=>m.type==='result' && m.result.browser_job_id==='after-cancel' && m.result.status==='complete'))
})

test('断线前的迟到结果不能释放重连后新任务的执行槽', async () => {
  const {context, posted, disconnect} = loadServiceWorker()
  let finishOld, finishNew
  context.executeJob = () => new Promise(resolve => { finishOld=resolve })
  const old = context.handleNativeResponse({job:{browser_job_id:'old-connection',deadline_ms:Date.now()+10000}})
  disconnect()
  context.connectNative()
  context.executeJob = () => new Promise(resolve => { finishNew=resolve })
  const next = context.handleNativeResponse({job:{browser_job_id:'new-connection',deadline_ms:Date.now()+10000}})
  finishOld({browser_job_id:'old-connection',status:'complete'})
  await old
  assert.equal(runInNewContext('busy',context),true)
  assert.equal(posted.filter(m=>m.type==='result' && m.result.browser_job_id==='old-connection').length,0)
  finishNew({browser_job_id:'new-connection',status:'complete'})
  await next
  assert.equal(runInNewContext('busy',context),false)
  assert.ok(posted.some(m=>m.type==='result' && m.result.browser_job_id==='new-connection'))
})
