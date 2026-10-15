import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PipelineDagPanel from '../components/PipelineDagPanel'
import type { DagStage, DagStageKey, PipelineDagResponse } from '../types'

function stage(key: DagStageKey, label: string, completedToday: number): DagStage {
  return {
    key,
    label,
    in_progress_label: key === 'capture' || key === 'timeline' ? '提炼中' : '生成中',
    pending_label: key === 'capture' ? '排队' : key === 'timeline' ? '待提炼' : '',
    in_progress_count: 0,
    pending_count: 0,
    completed_today: completedToday,
    in_progress_items: [],
    pending_items: [],
  }
}

describe('PipelineDagPanel', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('推理服务不可用时流程页显示受阻原因', async () => {
    const response: PipelineDagResponse = {
      server_now_ms: Date.now(), extractor_status: 'blocked', capture_enabled: true,
      running_bake_run: null, running_bake_runs: [], bake_watermark_lag_ms: 0,
      stages: [stage('capture', '采集', 0)],
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(response), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    render(<PipelineDagPanel base="http://127.0.0.1:7070" isVisible />)
    expect(await screen.findByText('推理服务不可用')).toBeInTheDocument()
    expect(screen.queryByText('未知')).not.toBeInTheDocument()
  })

  it('把数据作为提炼流程的同级产物并展示今日统计', async () => {
    const response: PipelineDagResponse = {
      server_now_ms: 1_800_000_000_000,
      extractor_status: 'idle',
      capture_enabled: true,
      running_bake_run: null,
      running_bake_runs: [],
      bake_watermark_lag_ms: 0,
      stages: [
        stage('capture', '采集', 1),
        stage('timeline', '预提炼', 2),
        stage('knowledge', '知识', 3),
        stage('sop', '操作手册', 4),
        stage('document', '文档', 5),
        stage('data', '数据', 6),
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    render(<PipelineDagPanel base="http://127.0.0.1:7070" isVisible />)

    const dataLabel = await screen.findByText('数据')
    await waitFor(() => {
      expect(dataLabel.closest('g')).toHaveTextContent('今日完成 6')
    })
  })

  it('旧接口缺少数据阶段时仍显示中文名称并可打开空态抽屉', async () => {
    const response: PipelineDagResponse = {
      server_now_ms: 1_800_000_000_000,
      extractor_status: 'idle',
      capture_enabled: true,
      running_bake_run: null,
      running_bake_runs: [],
      bake_watermark_lag_ms: 0,
      stages: [
        stage('capture', '采集', 1),
        stage('timeline', '预提炼', 2),
        stage('knowledge', '知识', 3),
        stage('sop', '操作手册', 4),
        stage('document', '文档', 5),
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    render(<PipelineDagPanel base="http://127.0.0.1:7070" isVisible />)

    const dataLabel = await screen.findByText('数据')
    expect(screen.queryByText('data')).not.toBeInTheDocument()
    expect(dataLabel.closest('g')).toHaveTextContent('今日完成 0')

    fireEvent.click(dataLabel.closest('g') as SVGGElement)

    expect(screen.getByText('当前没有正在生成的条目')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '×' })).toBeInTheDocument()
  })
})
