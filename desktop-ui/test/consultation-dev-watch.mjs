import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, rm, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { createServer, loadConfigFromFile } from 'vite'

for (const mode of ['development', 'test']) {
  test(`${mode}: isolate test edits from live consultation without disabling Vitest watch`, async () => {
    const root = await realpath(await mkdtemp(join(tmpdir(), 'mb-consultation-watch-')))
    await mkdir(join(root, 'src', '__tests__'), { recursive: true })
    const runtime = join(root, 'src', 'runtime.js')
    const testFiles = [join(root, 'src', '__tests__', 'history.test.tsx'), join(root, 'src', 'history.spec.tsx')]
    await writeFile(runtime, 'export const value = 1\n')
    for (const file of testFiles) await writeFile(file, 'export const fixture = 1\n')
    const loaded = await loadConfigFromFile({ command: 'serve', mode }, resolve('vite.config.ts'))
    assert.ok(loaded)
    const server = await createServer({ ...loaded.config, configFile: false, root, mode,
      cacheDir: join(root, '.vite'), logLevel: 'silent',
      server: { ...loaded.config.server, host: '127.0.0.1', port: 0, strictPort: false },
      optimizeDeps: { noDiscovery: true, include: [] },
    })
    try {
      const changes = []
      const messages = []
      server.watcher.on('change', path => changes.push(path))
      const send = server.ws.send.bind(server.ws)
      server.ws.send = (...args) => { messages.push(args[0]); return send(...args) }
      await server.listen()
      await server.transformRequest('/src/runtime.js')
      await delay(150)
      for (const file of testFiles) await writeFile(file, 'export const fixture = 2\n')
      await delay(500)
      if (mode === 'development') {
        assert.equal(changes.length, 0, 'test edits must not reach the live application watcher')
        assert.equal(messages.filter(m => m?.type === 'full-reload').length, 0)
      } else {
        for (const file of testFiles) assert.ok(changes.includes(file), 'Vitest must still observe test edits')
      }
      await writeFile(runtime, 'export const value = 2\n')
      for (let i = 0; i < 30 && !changes.includes(runtime); i++) await delay(50)
      assert.ok(changes.includes(runtime), 'runtime changes must still be watched')
    } finally {
      await server.close()
      await rm(root, { recursive: true, force: true })
    }
  })
}
