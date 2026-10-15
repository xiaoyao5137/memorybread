import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import CreationBrainstormInputEvidence from '../components/CreationBrainstormInputEvidence'
import type { CreationBrainstormInputPreflight, CreationBrainstormQuestion } from '../store/useAppStore'

const question: CreationBrainstormQuestion = {
  id: 'period', dimension: '周报周期', type: 'single_choice', prompt: '本次周报是哪一周？',
  why_now: '先确认统计范围。', required: true, allow_custom: true, answer_template: '填写起止日期。',
  input_clarification: { kind: 'report_period', need_ids: ['meeting', 'gpu', 'failed', 'skipped'] },
  options: [],
}

const preflight: CreationBrainstormInputPreflight = {
  schema_version: 1, status: 'partial', as_of_local_date: '2026-09-28', period: { status: 'unconfirmed' },
  needs: [
    { id: 'meeting', label: '周会纪要', source_type: 'memory', status: 'candidate', receipts: [{
      source_type: 'document', source_id: 783, title: 'GPU 周会纪要', source_url: 'https://example.com/meeting', observed_at: '2026-09-26',
      source_period: { start: '2026-09-21', end: '2026-09-25' },
      period_status: 'unknown', completeness: 'partial',
    }] },
    { id: 'gpu', label: 'GPU 周期报表', source_type: 'data', status: 'missing', receipts: [] },
    { id: 'failed', label: 'Token 周期报表', source_type: 'data', status: 'failed', receipts: [] },
    { id: 'skipped', label: 'AIGC 进展', source_type: 'memory', status: 'skipped', receipts: [] },
  ],
}

describe('创作细节本地资料核查', () => {
  it('shows bounded source dates and keeps candidate, missing, failed and skipped distinct', () => {
    render(<CreationBrainstormInputEvidence question={question} preflight={preflight} />)
    const region = screen.getByRole('region', { name: '本地资料核查' })
    expect(region).toHaveTextContent('部分资料仍需核对')
    expect(region).toHaveTextContent('核查日期 2026-09-28')
    expect(region).toHaveTextContent('报告周期待你确认')
    expect(region).toHaveTextContent('周会纪要候选资料待核对 · 统计期 2026-09-21 至 2026-09-25 · 采集或更新 2026-09-26')
    expect(region).toHaveTextContent('GPU 周期报表未找到匹配资料')
    expect(region).toHaveTextContent('Token 周期报表检索失败')
    expect(region).toHaveTextContent('AIGC 进展尚未检索')
    fireEvent.click(screen.getByText('候选资料待核对 · 统计期 2026-09-21 至 2026-09-25 · 采集或更新 2026-09-26'))
    expect(region).toHaveTextContent('GPU 周会纪要')
    expect(region).toHaveTextContent('与目标周期关系待确认 · 仅有部分内容')
    expect(region).toHaveTextContent('来源 document #783')
    expect(screen.getByRole('link', { name: '打开来源链接' })).toHaveAttribute('href', 'https://example.com/meeting')
    fireEvent.click(screen.getByText('检索失败', { selector: 'small' }))
    expect(region).toHaveTextContent('检索未完成，不能据此认定资料不存在。')
  })

  it('does not infer source coverage from unrelated needs or a missing preflight', () => {
    const view = render(<CreationBrainstormInputEvidence question={{ ...question,
      input_clarification: { kind: 'source_confirmation', need_ids: ['gpu'] } }} preflight={preflight} />)
    expect(screen.getByRole('region', { name: '本地资料核查' })).toHaveTextContent('GPU 周期报表')
    expect(screen.queryByText('周会纪要')).not.toBeInTheDocument()
    view.rerender(<CreationBrainstormInputEvidence question={question} />)
    expect(screen.queryByRole('region', { name: '本地资料核查' })).not.toBeInTheDocument()
  })
})
