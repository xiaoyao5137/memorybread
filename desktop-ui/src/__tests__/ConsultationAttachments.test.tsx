import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { invoke } from '@tauri-apps/api/core'
import { ConsultationAttachments, ConsultationImagePreview, consultationImages } from '../components/ConsultationAttachments'
import { buildConsultationAttachmentMetadata, buildAttachmentPrompt, filesToAttachments, persistConsultationAttachments } from '../utils/attachments'
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const images = ['first.png', 'second.png'].map((name, i) => ({ id: String(i), name, type: 'image/png', size: 3, path: `/local/${name}` }))

describe('咨询多图附件', () => {
  it('独立加载两张图片，点击第二张预览，移除第一张', async () => {
    vi.mocked(invoke).mockImplementation(async (_cmd, args: any) => `data:image/png;base64,${args.path}` as any)
    const preview = vi.fn(), remove = vi.fn()
    render(<ConsultationAttachments items={images} onPreview={preview} onRemove={remove} />)
    const second = screen.getByRole('button', { name: '查看图片 图片2' })
    await waitFor(() => expect(second).toBeEnabled())
    fireEvent.click(second)
    expect(preview).toHaveBeenCalledWith('data:image/png;base64,/local/second.png')
    fireEvent.click(screen.getByRole('button', { name: '移除 图片1' }))
    expect(remove).toHaveBeenCalledWith('0')
  })
  it('图片缺失不影响其他附件，保留失效提示', async () => {
    vi.mocked(invoke).mockRejectedValue('SCREENSHOT_NOT_FOUND')
    render(<ConsultationAttachments items={images} onPreview={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('图片1 · 原图暂不可用')).toBeInTheDocument())
    expect(screen.getAllByRole('button').every(button => button.hasAttribute('disabled'))).toBe(true)
  })
  it('原图预览可用 Escape 关闭', () => {
    const close = vi.fn()
    render(<ConsultationImagePreview src="data:image/png;base64,abc" onClose={close} />)
    expect(screen.getByRole('dialog', { name: '图片原图预览' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(close).toHaveBeenCalledOnce()
  })
  it('超过六个附件明确报错而非静默截断', async () => {
    await expect(filesToAttachments([new File(['x'], 'a.png')], 6)).rejects.toThrow('最多添加 6 个附件')
  })
  it('大图保存后请求只包含引用，重复发送复用原文件', async () => {
    vi.mocked(invoke).mockResolvedValue({ path: '/local/original.png', ocr_text: '图内文字' })
    const file = { ...images[0], path: undefined, dataUrl: 'data:image/png;base64,' + 'A'.repeat(3 * 1024 * 1024) }
    const saved = await persistConsultationAttachments([file])
    const metadata = JSON.stringify(buildConsultationAttachmentMetadata(saved))
    expect(metadata.length).toBeLessThan(300)
    expect(metadata).not.toContain('base64')
    expect(buildAttachmentPrompt(saved)).toContain('图内文字')
    expect(buildAttachmentPrompt(saved)).not.toContain('base64')
    await persistConsultationAttachments(saved)
    expect(invoke).toHaveBeenCalledOnce()
  })
  it('截屏和上传图片一起恢复并去重', () => {
    const ctx: any = { screenshot_path: '/local/screen.jpg', attachments: images }
    expect(consultationImages([ctx, ctx])).toHaveLength(3)
  })
})
