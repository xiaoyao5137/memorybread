
export const CUSTOMER_LOG_UPLOAD_PATH = '/__memorybread/diagnostics/upload'
const MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
const MAX_REQUEST_BYTES = Math.ceil(MAX_ARCHIVE_BYTES / 3) * 4 + 16 * 1024

class UploadError extends Error {
  constructor(status, message) { super(message); this.status = status }
}

// Browser development has no Tauri IPC. Keep its signed PUT on the local
// server, with the same bounded archive boundary as the desktop command.
export function validateUpload(payload) {
  const invalid = () => new UploadError(400, '诊断日志上传参数无效，请重新上报')
  if (!payload || typeof payload !== 'object') throw invalid()
  const { uploadUrl, requiredHeaders, contentBase64 } = payload
  if (typeof uploadUrl !== 'string' || uploadUrl.length > 8192) throw invalid()
  let url
  let path
  try { url = new URL(uploadUrl); path = decodeURIComponent(url.pathname) } catch { throw invalid() }
  if (url.protocol !== 'https:' || url.port || url.username || url.password || url.hash
    || !/^[a-z0-9][a-z0-9-]*\.oss-[a-z0-9-]+\.aliyuncs\.com$/.test(url.hostname)
    || !/^\/customer-logs\/[a-z0-9/_-]+\.zip$/i.test(path)
    || !['OSSAccessKeyId', 'Expires', 'Signature'].every(key => url.searchParams.get(key))) throw invalid()
  if (!requiredHeaders || typeof requiredHeaders !== 'object' || Array.isArray(requiredHeaders)) throw invalid()
  const headers = {}
  for (const [key, value] of Object.entries(requiredHeaders)) {
    const name = key.toLowerCase()
    if (!/^(content-type|content-md5|x-oss-[a-z0-9-]+)$/.test(name)
      || typeof value !== 'string' || /[\r\n]/.test(value)) throw invalid()
    headers[name] = value
  }
  if (headers['content-type'] !== 'application/zip') throw invalid()
  if (typeof contentBase64 !== 'string' || contentBase64.length > MAX_REQUEST_BYTES
    || contentBase64.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(contentBase64)) throw invalid()
  const archive = Buffer.from(contentBase64, 'base64')
  if (!archive.length || archive.length > MAX_ARCHIVE_BYTES || archive.toString('base64') !== contentBase64) throw invalid()
  return { url, headers, archive }
}

export function customerLogUploadMiddleware(send = fetch) {
  return async (req, res, next) => {
    if (req.url !== CUSTOMER_LOG_UPLOAD_PATH) return next()
    const reply = (status, message) => {
      res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
      res.end(JSON.stringify(message ? { error: { message } } : { uploaded: true }))
    }
    try {
      // No cross-origin callers, DNS rebinding hosts or form submissions.
      const authority = req.headers.host || ''
      if (!/^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/.test(authority)
        || req.headers.origin !== `http://${authority}`) throw new UploadError(403, '仅允许本机开发页面上传诊断日志')
      if (req.method !== 'POST') throw new UploadError(405, '不支持的日志上传方法')
      if (req.headers['content-type'] !== 'application/json') throw new UploadError(415, '日志上传格式无效')
      const chunks = []
      let size = 0
      for await (const chunk of req) {
        size += chunk.length
        if (size > MAX_REQUEST_BYTES) throw new UploadError(413, '诊断日志超过 10MB')
        chunks.push(Buffer.from(chunk))
      }
      let payload
      try { payload = JSON.parse(Buffer.concat(chunks).toString('utf8')) }
      catch { throw new UploadError(400, '诊断日志上传参数无效') }
      const { url, headers, archive } = validateUpload(payload)
      const response = await send(url, {
        method: 'PUT', headers, body: archive, redirect: 'manual',
        signal: AbortSignal.timeout(55_000),
      })
      await response.body?.cancel()
      if (!response.ok) throw new UploadError(502, `诊断日志上传失败（HTTP ${response.status}），请重新上报`)
      reply(200)
    } catch (error) {
      // Never return/log signed URLs, upstream XML or request contents.
      reply(error instanceof UploadError ? error.status : 502,
        error instanceof UploadError ? error.message : '诊断日志传输失败，请检查网络后重试')
    }
  }
}

export function customerLogUploadPlugin() {
  return {
    name: 'memorybread-diagnostic-upload',
    apply: 'serve',
    configureServer(server) { server.middlewares.use(customerLogUploadMiddleware()) },
  }
}
