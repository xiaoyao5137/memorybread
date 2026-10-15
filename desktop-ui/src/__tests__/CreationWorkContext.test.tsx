import React, { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationWorkContextEditor from '../components/CreationWorkContextEditor'
import { creationBriefWithWorkContext, emptyWorkContext, readWorkContext, workContextForRequest, workPreviewText,
  type CreationWorkContext, type WorkPreviewResult } from '../utils/creationWorkContext'

afterEach(() => vi.restoreAllMocks())

describe('工作文档设置与只读预览', () => {
  it('候选仍展示已有审阅问题和改法，不隐藏核验失败原因', () => {
    const text = workPreviewText({ status: 'revise', content: '当前候选正文。', review: {
      checks: [{ id: 'CREATION_WORK_DECISION_UNCLEAR', passed: false, reason: '需要明确评审结论' }],
      issues: [{ code: 'CREATION_WORK_DECISION_UNCLEAR', quote: '另行考虑', correction: '写明是否批准试点' }],
    } })
    expect(text).toContain('当前候选正文。')
    expect(text).toContain('需要明确评审结论')
    expect(text).toContain('写明是否批准试点')
  })
  it('旧草稿不启用审阅，已有简报的确认项原样保留，样稿草稿空槽不发送', () => {
    expect(readWorkContext(undefined)).toBeNull()
    const brief = { decisions: [{ source: 'user', summary: '保留原有流程' }], brief_edits: { topic: '人工编辑' } }
    expect(creationBriefWithWorkContext(brief, undefined)).toBe(brief)
    const context = { ...emptyWorkContext(), decision_goal: '批准试点', style_samples: ['认可的表达', ''] }
    expect(creationBriefWithWorkContext(brief, context)).toEqual({ ...brief, work_context: { ...context, style_samples: ['认可的表达'] } })
    expect(workContextForRequest(context)?.review_enabled).toBe(false)
    expect(readWorkContext({ schema_version: 'unsupported' })).toBeNull()
    expect(readWorkContext({ style_samples: ['a'.repeat(1501)] })).toBeNull()
  })

  it('仅选择场景才开启对应审阅，取消仍保留填写内容', () => {
    let latest: CreationWorkContext | undefined
    function Harness() {
      const [value, setValue] = useState<CreationWorkContext>()
      return <CreationWorkContextEditor value={value} onChange={next => { latest = next; setValue(next) }} onPreview={vi.fn()} />
    }
    render(<Harness />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    fireEvent.change(screen.getByLabelText('使用场景'), { target: { value: 'proposal_review' } })
    expect(screen.getByRole('checkbox')).toBeChecked()
    expect(latest?.purpose).toBe('方案评审')
    fireEvent.change(screen.getByLabelText('希望读者决定什么'), { target: { value: '批准试点' } })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(latest).toMatchObject({ profile_id: 'proposal_review', decision_goal: '批准试点', review_enabled: false })
  })

  it('试写只显示候选，必须主动认可才保存样稿，满额不删除旧样稿', async () => {
    const result: WorkPreviewResult = { status: 'candidate', content: '建议先验证入口流程，避免同时改动下游系统。' }
    const preview = vi.fn().mockResolvedValue(result)
    let latest = { ...emptyWorkContext(), style_samples: ['旧样稿一', '旧样稿二'] }
    function Harness() {
      const [value, setValue] = useState(latest)
      return <CreationWorkContextEditor value={value} onChange={next => { latest = next; setValue(next) }} canPreview onPreview={preview} />
    }
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: '试写关键段落' }))
    expect(await screen.findByText(result.content!)).toBeInTheDocument()
    expect(latest.style_samples).toEqual(['旧样稿一', '旧样稿二'])
    fireEvent.click(screen.getByRole('button', { name: '认可并保存为表达样稿' }))
    expect(latest.style_samples).toEqual(['旧样稿一', '旧样稿二', result.content])
    fireEvent.click(screen.getByRole('button', { name: '认可并保存为表达样稿' }))
    expect(screen.getByRole('status')).toHaveTextContent('已保存三条表达样稿')
    expect(latest.style_samples).toHaveLength(3)
  })

  it('未核验候选不可保存；取消预览不会接受迟到结果', async () => {
    let finish!: (value: WorkPreviewResult) => void
    const onChange = vi.fn()
    const preview = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    render(<CreationWorkContextEditor canPreview onChange={onChange} onPreview={preview} />)
    fireEvent.click(screen.getByRole('button', { name: '试写关键段落' }))
    fireEvent.click(screen.getByRole('button', { name: '取消预览' }))
    finish({ status: 'candidate', content: '迟到结果' })
    await waitFor(() => expect(screen.queryByText('迟到结果')).not.toBeInTheDocument())
    expect(onChange).not.toHaveBeenCalled()
  })
})
