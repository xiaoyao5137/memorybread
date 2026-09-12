import { describe, expect, it } from 'vitest'
import { buildConsultationOcrText, type UserAttachment } from '../utils/attachments'

describe('咨询材料边界', () => {
  it('保留图片名称、各自行列和屏幕来源', () => {
    const images: UserAttachment[] = [
      { id: 'a', name: '图片1', type: 'image/png', size: 1, dataUrl: '', ocrText: '方案甲 | 21.60\n方案乙 | 11.84' },
      { id: 'b', name: '图片3', type: 'image/png', size: 1, dataUrl: '', ocrText: '延迟曲线\n时间范围：完整' },
    ]
    expect(buildConsultationOcrText(images, '当前页面')).toBe(
      '【本次屏幕】\n当前页面\n\n【图片1】\n方案甲 | 21.60\n方案乙 | 11.84\n【图片1结束】\n\n【图片3】\n延迟曲线\n时间范围：完整\n【图片3结束】',
    )
  })
  it('不为未识别的图片编造文字，也不重排图片名称', () => {
    expect(buildConsultationOcrText([{ id: 'a', name: '图片2', type: 'image/png', size: 1, dataUrl: '', ocrText: ' ' }])).toBe('')
    expect(buildConsultationOcrText([])).toBe('')
  })
})
