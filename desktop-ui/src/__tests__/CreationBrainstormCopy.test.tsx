import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { BrainstormChoice, BrainstormContext, BrainstormPrompt, splitBrainstormCopy } from '../components/CreationBrainstormCopy'

describe('脑暴文案与依据分层展示', () => {
  it.each([
    '本轮未检索到相关历史记忆。',
    '本轮历史记忆检索失败。',
    '本轮部分记忆检索失败。',
    '本轮按你的资料范围要求未检索历史记忆。',
    '待验证推演：缺少验证材料，需要进一步确认。',
    '基于当前输入的建议。',
  ])('将旧版附加通知「%s」归入依据，保留原说明', notice => {
    expect(splitBrainstormCopy(`先明确需要达成的目标。\n${notice}`)).toEqual({
      copy: '先明确需要达成的目标。', details: notice,
    })
  })

  it('默认只显示决策说明，来源独立展开且不会触发选项作答', () => {
    const onSelect = vi.fn()
    const details = '《业务访谈》：商家只审核商业逻辑与合规底线。'
    const option = { id: 'review', label: '提高商家审核效率', description: '优先缩短审核时间并守住合规底线。', details, recommended: true }
    const { container } = render(<div className="creation-brainstorm-options"><BrainstormChoice option={option}
      type="button" role="checkbox" aria-checked={false} onClick={onSelect} /></div>)
    const choice = screen.getByRole('checkbox', { name: /提高商家审核效率/ })
    expect(choice).toHaveTextContent(option.description)
    expect(choice).not.toHaveTextContent(details)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('查看选项依据'))
    expect(container.querySelector('details')).toHaveAttribute('open')
    expect(screen.getByText(details)).toBeVisible()
    expect(onSelect).not.toHaveBeenCalled()
    fireEvent.click(choice)
    expect(onSelect).toHaveBeenCalledOnce()
  })

  it('旧记录的说明与问题背景中附加的记忆依据默认折叠，保留完整原文', () => {
    const source = '记忆依据：《经营访谈》 · knowledge:4406\n' + JSON.stringify({ dedup_key: 'a'.repeat(1200) })
    const { container } = render(<>
      <BrainstormContext text={'先确认优先目标。本轮已检索历史记忆；沿用历史结论：审核由商家负责。'} />
      <BrainstormChoice option={{ id: 'review', label: '商家审核效率', description: `先打通商家审核。\n${source}` }}
        type="button" role="radio" aria-checked={false} />
    </>)
    expect(screen.getByText('先确认优先目标。')).toBeVisible()
    expect(screen.getByRole('radio')).toHaveTextContent('先打通商家审核。')
    expect(screen.getByRole('radio')).not.toHaveTextContent('dedup_key')
    expect([...container.querySelectorAll('details')].every(element => !element.open)).toBe(true)
    fireEvent.click(screen.getByText('查看背景与依据'))
    expect(screen.getByText('本轮已检索历史记忆；沿用历史结论：审核由商家负责。')).toBeVisible()
    fireEvent.click(screen.getByText('查看选项依据'))
    expect(container.querySelector('.creation-brainstorm-choice-details > p')?.textContent).toBe(source)
  })

  it('长标题与说明可展开完整内容，保留原始选项对象和用户选择行为', () => {
    const option = {
      id: 'long-choice',
      label: '围绕现有商家角色转变与信任重建制定分阶段的审核、授权与责任方案',
      description: '需要由商家确认品牌授权、审核合规风险以及最终发布责任。'.repeat(8),
    }
    const savedOption = JSON.stringify(option)
    const onSelect = vi.fn()
    const { container } = render(<BrainstormChoice option={option} type="button" role="checkbox" aria-checked onClick={onSelect} />)
    const choice = screen.getByRole('checkbox')
    expect(choice.querySelector('strong > span')?.textContent?.length).toBeLessThanOrEqual(24)
    expect(choice.querySelector('span > small')?.textContent?.length).toBeLessThanOrEqual(80)
    expect(choice).toBeChecked()
    fireEvent.click(screen.getByText('展开完整选项'))
    expect(container.querySelector('details')).toHaveAttribute('open')
    expect(screen.getByText(option.label)).toBeVisible()
    expect(screen.getByText(option.description)).toBeVisible()
    expect(onSelect).not.toHaveBeenCalled()
    expect(JSON.stringify(option)).toBe(savedOption)
  })

  it('缩短预览中占据整段的来源标题，让用户先看到实际取舍并可展开原文', () => {
    const source = '《该方案旨在突破商家限制并吸纳高价值商家同时完成授权制作发布的完整方案》'
    const description = `依据记忆${source}，此方向优先降低商家确认成本。`
    const option = { id: 'cost', label: '降低商家确认成本', description }
    render(<><BrainstormContext text={description} /><BrainstormChoice option={option} type="button" role="checkbox" /></>)
    expect(screen.getByRole('checkbox')).toHaveTextContent('依据记忆《历史资料》，此方向优先降低商家确认成本。')
    expect(screen.getByRole('checkbox')).not.toHaveTextContent(source)
    // Even a replacement that now fits the preview limit must retain an
    // independent full-text disclosure for both question and option copy.
    fireEvent.click(screen.getByText('展开选项说明'))
    fireEvent.click(screen.getByText('展开问题说明'))
    expect(screen.getAllByText(description)).toHaveLength(2)
    expect(option.description).toBe(description)
  })

  it('超长旧题干可完整展开并在切换问题时恢复简洁展示', () => {
    const text = '在托管式直销模式下，'.repeat(9) + '您最希望最终呈现的核心成果是什么？'
    const { rerender } = render(<BrainstormPrompt text={text} />)
    expect(screen.queryByText(text)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开完整问题' }))
    expect(screen.getByText(text)).toBeVisible()
    expect(screen.getByRole('button', { name: '收起完整问题' })).toHaveAttribute('aria-expanded', 'true')
    rerender(<BrainstormPrompt text={text + '新的问题。'} />)
    expect(screen.getByRole('button', { name: '展开完整问题' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('短文案不增加无用的展开操作，纯背景字段仍然可查阅', () => {
    const { container } = render(<>
      <BrainstormPrompt text="优先改进哪个环节？" />
      <BrainstormContext text="" details="本轮没有找到可引用的相关历史。" />
      <BrainstormChoice option={{ id: 'short', label: '商家审核', description: '先优化审核体验。' }} type="button" />
    </>)
    expect(screen.queryByRole('button', { name: '展开完整问题' })).not.toBeInTheDocument()
    expect(container.querySelectorAll('details')).toHaveLength(1)
    expect(screen.getByText('查看背景与依据')).toBeVisible()
  })
})
