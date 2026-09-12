import { useState } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ConsultationImageInput } from '../components/ConsultationImageInput'
import { nameConsultationImages, buildAttachmentPrompt, buildConsultationAttachmentMetadata } from '../utils/attachments'

const images = [1, 2].map(id => ({ id: String(id), name: `图片${id}`, type: 'image/png', size: 1, dataUrl: '', path: `/local/${id}`, ocrText: `文字${id}` }))
function Input({ send = vi.fn() }) {
  const [value, setValue] = useState('')
  return <ConsultationImageInput value={value} images={images} onValueChange={setValue} onKeyDown={send} />
}
describe('图片引用', () => {
  it('键盘选择第二张，保留光标后的文字，不发送咨询', () => {
    const send = vi.fn()
    render(<Input send={send} />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '比较 @ 和上一版', selectionStart: 4 } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('比较 @图片2  和上一版')
    expect(send).not.toHaveBeenCalled()
  })
  it('中文输入法确认不触发图片选择；Escape 关闭候选', () => {
    render(<Input />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '@图片' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(input).toHaveValue('@图片')
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
  it('同名文件按图片编号，删除后不改号，新图片继续递增，非图片保留文件名', () => {
    const files = [images[0], { ...images[1], name: 'Image.png' }, { ...images[0], type: 'text/plain', name: '说明.txt' }]
    expect(nameConsultationImages(files).map(item => item.name)).toEqual(['图片1', '图片2', '说明.txt'])
    expect(nameConsultationImages([images[1]])[0].name).toBe('图片2')
    expect(nameConsultationImages([{ ...images[0], name: 'Image.png' }], 2)[0].name).toBe('图片3')
    expect(buildConsultationAttachmentMetadata(images)[1]).toMatchObject({ name: '图片2', path: '/local/2' })
    expect(buildAttachmentPrompt(images)).toContain('图片2（image/png，1 B，图片文字识别：文字2）')
  })
})
