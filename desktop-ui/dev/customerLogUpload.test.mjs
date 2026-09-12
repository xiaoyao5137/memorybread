// @vitest-environment node
import { createServer } from 'node:http'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { customerLogUploadMiddleware, validateUpload, CUSTOMER_LOG_UPLOAD_PATH } from './customerLogUpload.mjs'

const payload = () => ({
  uploadUrl: 'https://test-bucket.oss-cn-beijing.aliyuncs.com/customer-logs/production/2026/09/06/test.zip?OSSAccessKeyId=test&Expires=9999999999&Signature=secret',
  requiredHeaders: { 'content-type': 'application/zip' },
  contentBase64: Buffer.from('test archive').toString('base64'),
})
const servers = []
afterEach(async () => {
  await Promise.all(servers.splice(0).map(server => new Promise((resolve) => {
    server.close(() => resolve()); server.closeAllConnections()
  })))
})
async function start(send = vi.fn().mockResolvedValue(new Response(null))) {
  const handler = customerLogUploadMiddleware(send)
  const server = createServer((req, res) => void handler(req, res, () => { res.writeHead(404); res.end() }))
  servers.push(server)
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  const base = `http://127.0.0.1:${address.port}`
  const post = (body = payload(), headers = {}) => fetch(base + CUSTOMER_LOG_UPLOAD_PATH, {
    method: 'POST', headers: { Origin: base, 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
  return { post, send }
}
describe('local diagnostic upload boundary', () => {
  it('sends the exact bytes through a bounded non-redirecting PUT', async () => {
    const { post, send } = await start()
    expect((await post()).status).toBe(200)
    expect(send).toHaveBeenCalledWith(new URL(payload().uploadUrl), expect.objectContaining({
      method: 'PUT', redirect: 'manual', body: Buffer.from('test archive'), signal: expect.any(AbortSignal),
      headers: { 'content-type': 'application/zip' },
    }))
  })
  it.each(['https://attacker.example', 'null', ''])('rejects foreign/missing origin %s', async origin => {
    const { post, send } = await start()
    expect((await post(payload(), { Origin: origin })).status).toBe(403)
    expect(send).not.toHaveBeenCalled()
  })
  it.each([
    'http://test-bucket.oss-cn-beijing.aliyuncs.com/customer-logs/p/test.zip',
    'https://127.0.0.1/private',
    'https://test-bucket.oss-cn-beijing.aliyuncs.com.evil.example/customer-logs/p/test.zip',
    'https://test-bucket.oss-cn-beijing.aliyuncs.com/private.zip',
    'https://test-bucket.oss-cn-beijing.aliyuncs.com/customer-logs/%ZZ.zip',
    'https://user:pass@test-bucket.oss-cn-beijing.aliyuncs.com/customer-logs/p/test.zip',
  ])('rejects untrusted target %s', uploadUrl => {
    expect(() => validateUpload({ ...payload(), uploadUrl })).toThrow()
  })
  it('rejects injected headers and invalid or oversized archives', () => {
    for (const requiredHeaders of [{ authorization: 'Bearer secret' }, { 'content-type': 'application/zip\r\nHost: other' }]) {
      expect(() => validateUpload({ ...payload(), requiredHeaders })).toThrow()
    }
    for (const contentBase64 of ['', '!!!!', Buffer.alloc(10 * 1024 * 1024 + 1).toString('base64')]) {
      expect(() => validateUpload({ ...payload(), contentBase64 })).toThrow()
    }
    expect(validateUpload({ ...payload(), contentBase64: Buffer.alloc(10 * 1024 * 1024).toString('base64') }).archive.length).toBe(10 * 1024 * 1024)
  })
  it.each([302, 403, 500])('does not claim success for upstream %s or expose signed URL', async status => {
    const { post } = await start(vi.fn().mockResolvedValue(new Response('sensitive XML', { status })))
    const response = await post()
    expect(response.status).toBe(502)
    const text = await response.text()
    expect(text).toContain(`HTTP ${status}`)
    expect(text).not.toMatch(/secret|sensitive|Signature/)
  })
  it('sanitizes network errors that include credentials', async () => {
    const { post } = await start(vi.fn().mockRejectedValue(new Error(payload().uploadUrl)))
    const response = await post()
    expect(response.status).toBe(502)
    expect(await response.text()).not.toContain('Signature')
  })
})
