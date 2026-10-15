import { describe, expect, it } from 'vitest'
import { stageFixture } from '../../test/brainstorm-stage-fixture'
import { brainstormHistoryStatus, brainstormStatusSyncRetryDelay, normalizeBrainstormStageSummary, type BrainstormStageSummary } from '../utils/brainstormStageStatus'

const summary = (patch: Partial<BrainstormStageSummary> = {}): BrainstormStageSummary => ({
  version: 2, phase: 'ready', active_stage: 'writing', stage_status: 'ready',
  completed_chapters: 1, total_chapters: 2, active_chapter_title: '执行安排', ...patch,
})

describe('brainstorm history stage status', () => {
  it.each([408, 429, 502, 503, 504])('backs off temporary status read failure %s', status => {
    expect(brainstormStatusSyncRetryDelay({ status }, 1)).toBe(1500)
    expect(brainstormStatusSyncRetryDelay({ status }, 2)).toBe(3000)
    expect(brainstormStatusSyncRetryDelay({ status }, 20)).toBe(30000)
  })

  it('retries interrupted reads and timeouts while stopping on authentication, invalid responses and unknown failures', () => {
    expect(brainstormStatusSyncRetryDelay({ code: 'BRAINSTORM_CONNECTION_INTERRUPTED' }, 1)).toBe(1500)
    expect(brainstormStatusSyncRetryDelay({ name: 'TimeoutError' }, 1)).toBe(1500)
    for (const error of [{ status: 401, retryable: true }, { status: 403 }, { status: 404 }, { status: 409 }, { status: 500 }, { code: 'BRAINSTORM_RESPONSE_INVALID' }, new Error('unknown')]) {
      expect(brainstormStatusSyncRetryDelay(error, 1)).toBeNull()
    }
  })
  it('prefers current session summary over an older completed brief', () => {
    const brief = stageFixture('completed', 'writing')
    brief.phase = 'completed'
    expect(normalizeBrainstormStageSummary(summary(), brief)).toEqual(summary())
  })

  it('falls back to stored v2 brief when an older Core has no summary', () => {
    const brief = stageFixture('ready', 'writing')
    brief.stage_workflow!.chapters[0].status = 'completed'
    expect(normalizeBrainstormStageSummary(undefined, brief)).toMatchObject({
      active_stage: 'writing', phase: 'ready', stage_status: 'ready', completed_chapters: 1, total_chapters: 2,
    })
    expect(normalizeBrainstormStageSummary(undefined, null)).toBeNull()
    expect(brainstormHistoryStatus(null, 'completed')).toBeNull()
  })

  it.each([
    ['ready', 'ready', 'completed', '创作行文待确认', false],
    ['chapter_writing', 'answering', 'completed', '章节创作中', true],
    ['chapter_writing', 'answering', 'failed', '章节创作待继续', false],
    ['chapter_writing', 'answering', 'cancelled', '章节创作待继续', false],
    ['preparing', 'thinking', 'completed', '创作行文思考中', true],
    ['preparing', 'failed', 'completed', '创作行文思考暂停', false],
  ])('shows stage %s/%s separately from durable lifecycle %s', (phase, stage_status, lifecycle, label, poll) => {
    expect(brainstormHistoryStatus(summary({ phase, stage_status }), lifecycle)).toMatchObject({
      label, poll, detail: '1 / 2 章已完成 · 当前章节：执行安排',
    })
  })

  it('marks the entire creation complete only when its workflow is complete', () => {
    expect(brainstormHistoryStatus(summary({ phase: 'completed', stage_status: 'completed', completed_chapters: 2, active_chapter_title: null }), 'failed'))
      .toMatchObject({ label: '全部章节已完成', detail: '2 / 2 章已完成', poll: false })
  })

  it('continues polling a recovering stage even when the older document lifecycle is failed', () => {
    const brief = stageFixture('thinking', 'writing')
    brief.stage_workflow!.stages[2].recovery_progress = { attempt: 2, max_attempts: 2, retry_at_ms: Date.now() }
    const current = normalizeBrainstormStageSummary(undefined, brief)
    expect(brainstormHistoryStatus(current, 'failed')).toMatchObject({
      label: '创作行文思考中', tone: 'running', poll: true,
      description: '模型正在后台准备本阶段完整思路，点击查看进度。',
    })
  })
})
