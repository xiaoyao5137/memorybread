import { invoke } from '@tauri-apps/api/core'

export interface UserAttachment {
  id: string
  name: string
  type: string
  size: number
  path?: string
  ocrText?: string
  dataUrl: string
}

const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
const MAX_ATTACHMENTS = 6

const readFileAsDataUrl = (file: File) => new Promise<UserAttachment>((resolve, reject) => {
  if (file.size > MAX_ATTACHMENT_BYTES) {
    reject(new Error(`${file.name} 超过 8MB，暂不支持上传`))
    return
  }

  const reader = new FileReader()
  reader.onload = () => {
    resolve({
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      name: file.name || '未命名附件',
      type: file.type || 'application/octet-stream',
      size: file.size,
      dataUrl: String(reader.result || ''),
    })
  }
  reader.onerror = () => reject(new Error(`${file.name || '附件'} 读取失败`))
  reader.readAsDataURL(file)
})

export async function filesToAttachments(files: Iterable<File>, existingCount = 0) {
  const selected = Array.from(files)
  if (selected.length + existingCount > MAX_ATTACHMENTS) throw new Error(`最多添加 ${MAX_ATTACHMENTS} 个附件，请移除部分附件后重试`)
  return Promise.all(selected.map(readFileAsDataUrl))
}

export function formatAttachmentSize(size: number) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

export function buildAttachmentPrompt(attachments: UserAttachment[]) {
  if (!attachments.length) return ''
  return [
    '用户随本次请求附加了以下文件。请结合附件信息回答；如果当前模型无法直接读取图片内容，请明确基于用户指令和可见上下文给出结果，不要声称已经看到了图片细节。',
    ...(attachments.some(item => /^图片\d+$/.test(item.name)) ? ['指令中的 @图片编号 对应下方同名图片，请严格按名称对应其内容。'] : []),
    ...attachments.map((item, index) => {
      const imageHint = !item.type.startsWith('image/') ? '' : item.path
        ? `，图片文字识别：${item.ocrText?.slice(0, 6000) || '未识别到文字，不能据此判断图片细节'}`
        : `，图片 data URL：${item.dataUrl.slice(0, 180)}...`
      return `${index + 1}. ${item.name}（${item.type || '未知类型'}，${formatAttachmentSize(item.size)}${imageHint}）`
    }),
  ].join('\n')
}

export function buildAttachmentMetadata(attachments: UserAttachment[]) {
  return attachments.map(({ id, name, type, size, dataUrl }) => ({ id, name, type, size, data_url: dataUrl }))
}

export function buildConsultationAttachmentMetadata(attachments: UserAttachment[]) {
  return attachments.map(({ id, name, type, size, path }) => ({ id, name, type, size, path }))
}

export function buildConsultationOcrText(attachments: UserAttachment[], screenText = '') {
  return [
    ...(screenText.trim() ? [`【本次屏幕】\n${screenText.trim()}`] : []),
    ...attachments.filter(item => item.ocrText?.trim()).map(item =>
      `【${item.name}】\n${item.ocrText!.trim()}\n【${item.name}结束】`),
  ].join('\n\n')
}

// Persist originals before sending; request/history contain references, never Base64 blobs.
export async function persistConsultationAttachments(attachments: UserAttachment[]): Promise<UserAttachment[]> {
  const result: UserAttachment[] = []
  for (const item of attachments) {
    if (item.path) { result.push(item); continue }
    const saved = await invoke<{ path: string; ocr_text: string }>('save_consultation_attachment', { dataUrl: item.dataUrl })
    result.push({ ...item, path: saved.path, ocrText: saved.ocr_text })
  }
  return result
}

// Assign once on insertion: removing a picture must never retarget an existing @ mention.
export function nameConsultationImages<T extends { name: string; type: string }>(items: T[], start = 0): T[] {
  let next = Math.max(start, ...items.map(item => item.type.startsWith('image/') ? Number(/^图片(\d+)$/.exec(item.name)?.[1] || 0) : 0))
  return items.map(item => item.type.startsWith('image/') && !/^图片\d+$/.test(item.name)
    ? { ...item, name: `图片${++next}` } : item)
}
