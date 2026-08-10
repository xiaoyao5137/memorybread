import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PipelineBacklogAlert } from '../components/MonitorPanel'

const defaultProps = {
  capturePending: 2,
  captureOldestAtMs: null,
  bakePending: 1,
  bakeOldestAtMs: null,
  staleBakeRuns: 0,
  bakeStalled: false,
  recentBakeFailures: 0,
  recentBakeDeferred: 0,
  recentBakeRuns: 0,
  recentNoProgress: 0,
  schedulerMismatch: false,
  captureEnabled: false,
  inferenceQueue: undefined,
}

describe('MonitorPanel 采集暂停提示', () => {
  it('暂停时提供明确的恢复入口并触发授权操作', () => {
    const onEnableCapture = vi.fn()
    render(<PipelineBacklogAlert {...defaultProps} onEnableCapture={onEnableCapture} />)

    expect(screen.getByText('自动采集与提炼已暂停')).toBeInTheDocument()
    expect(screen.getByText(/时间线队列 2 条、烘焙队列 1 条/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开启自动采集与提炼' }))
    expect(onEnableCapture).toHaveBeenCalledTimes(1)
  })

  it('请求期间禁用按钮并显示进度', () => {
    render(
      <PipelineBacklogAlert
        {...defaultProps}
        enablingCapture
        onEnableCapture={() => {}}
      />,
    )

    expect(screen.getByRole('button', { name: '正在开启…' })).toBeDisabled()
  })

  it('开启失败时保留暂停状态并显示可重试错误', () => {
    render(
      <PipelineBacklogAlert
        {...defaultProps}
        enableCaptureError="开启失败，请确认本机服务正常后重试"
        onEnableCapture={() => {}}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('开启失败，请确认本机服务正常后重试')
    expect(screen.getByRole('button', { name: '开启自动采集与提炼' })).toBeEnabled()
  })

  it('队列可执行但连续空跑时显示调度口径异常', () => {
    render(
      <PipelineBacklogAlert
        {...defaultProps}
        captureEnabled
        bakePending={319}
        recentNoProgress={4}
        schedulerMismatch
      />,
    )

    expect(screen.getByText(/队列仍有可执行候选，但最近 5 批有 4 批未取得进展/)).toBeInTheDocument()
  })
})
