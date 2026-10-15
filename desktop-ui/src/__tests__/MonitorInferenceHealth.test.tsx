import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ExtractionQueueCard, ServiceHealthBanner } from '../components/MonitorPanel'
import type { InferenceRuntimeHealth, ServiceHealth } from '../types'

const healthy: ServiceHealth = {
  status: 'ok', mode: 'full', full_dispatch_ready: true, background_processor_running: true,
  critical_checks_passed: true, embedding_ok: true, issues: [], updated_at_ms: Date.now(),
}
const inference: InferenceRuntimeHealth = {
  available: false, checked_at_ms: Date.now(), recovery_status: 'restarting', recovery_stale: false,
  attempts: 1, max_attempts: 3, next_retry_at_ms: Date.now() + 30_000,
  error_code: 'LOCAL_INFERENCE_UNAVAILABLE',
}

describe('监控实时推理故障', () => {
  it('启动快照正常也必须显示实时故障告警和自动恢复进度', () => {
    render(<ServiceHealthBanner health={{ ...healthy, inference }} />)
    expect(screen.getByRole('alert')).toHaveTextContent('关键服务不可用')
    expect(screen.getByText('正在自动重启本地推理服务')).toBeInTheDocument()
    expect(screen.getByText('本轮已尝试 1/3 次')).toBeInTheDocument()
    expect(screen.getByText('本地推理 异常')).toBeInTheDocument()
    expect(screen.getByText('后台整理 受阻')).toBeInTheDocument()
    expect(screen.getByText(/下次重试约/)).toBeInTheDocument()
  })
  it.each([
    ['cooldown', '自动重启暂未成功，正在冷却等待重试'],
    ['component_missing', '本地推理组件缺失，请在设置中继续初始化／修复'],
    ['port_conflict', '本地推理服务端口被占用，请检查本机服务'],
    ['retry_wait', '本地推理服务启动失败，等待自动重试'],
  ])('展示恢复状态 %s', (status, label) => {
    render(<ServiceHealthBanner health={{ ...healthy, inference: { ...inference, recovery_status: status } }} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })
  it('恢复后移除告警，但后台整理未启动时仍保留告警', () => {
    const { rerender } = render(<ServiceHealthBanner health={{ ...healthy, inference: { ...inference, available: true } }} />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    rerender(<ServiceHealthBanner health={{ ...healthy, status: 'down', background_processor_running: false,
      inference: { ...inference, available: true } }} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('后台整理 异常')).toBeInTheDocument()
  })
  it('存在正在处理的任务也不能把不可用的引擎显示为运行中', () => {
    render(<ExtractionQueueCard pending={5500} status="running" extractingCount={9}
      lastExtractionAtMs={Date.now()} oldestPendingAtMs={Date.now() - 100_000} stalled={false}
      inference={inference} />)
    expect(screen.getByText('提炼受阻')).toBeInTheDocument()
    expect(screen.queryByText('运行中')).not.toBeInTheDocument()
    expect(screen.queryByText(/正在提炼 9 条/)).not.toBeInTheDocument()
  })
  it('保留用户主动暂停的状态', () => {
    render(<ExtractionQueueCard pending={5500} status="paused" extractingCount={0}
      lastExtractionAtMs={null} oldestPendingAtMs={null} stalled={false} inference={inference} />)
    expect(screen.getByText('已暂停')).toBeInTheDocument()
  })
})
