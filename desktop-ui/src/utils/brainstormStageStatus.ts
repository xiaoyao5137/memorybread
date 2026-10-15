import type { CreationBrainstormStageId, CreationBrainstormState } from '../store/useAppStore'

export const brainstormStageTitle = (id: string) => ({
  direction: '创作方向', detail: '创作细节', writing: '创作行文',
})[id] || '脑暴'

/** Retry only reads that can recover; user actions and generation stay in Core. */
export function brainstormStatusSyncRetryDelay(error: unknown, failures: number): number | null {
  const failure = error as { code?: string; status?: number; name?: string } | null
  const status = failure?.status
  if (typeof status === 'number' && status >= 400 && status < 500 && status !== 408 && status !== 429) return null
  const temporary = [408, 429, 502, 503, 504].includes(status || 0)
    || failure?.code === 'BRAINSTORM_CONNECTION_INTERRUPTED'
    || failure?.name === 'TimeoutError'
  return temporary ? Math.min(30000, 1500 * 2 ** Math.min(5, Math.max(0, failures - 1))) : null
}

/** Marks the exact server snapshot already accepted by this mounted page. */
export function brainstormStageSnapshotKey(state: CreationBrainstormState | null): string | null {
  const workflow = state?.stage_workflow
  const stage = workflow?.stages.find(item => item.id === workflow.active_stage)
  return workflow && stage ? JSON.stringify([state.session_id, state.revision, workflow.active_stage, stage.generation_id]) : null
}

export interface BrainstormStageSummary {
  version: 2
  phase: string
  active_stage: CreationBrainstormStageId
  stage_status: string
  completed_chapters: number
  total_chapters: number
  active_chapter_title?: string | null
}

/** The server summary is current session state; the brief is an older-API fallback. */
export function normalizeBrainstormStageSummary(value: unknown, brief: CreationBrainstormState | null): BrainstormStageSummary | null {
  const summary = value && typeof value === 'object' ? value as Partial<BrainstormStageSummary> : null
  if (summary?.version === 2 && ['direction', 'detail', 'writing'].includes(String(summary.active_stage))
    && typeof summary.phase === 'string' && typeof summary.stage_status === 'string') {
    return {
      version: 2, phase: summary.phase, active_stage: summary.active_stage!, stage_status: summary.stage_status,
      completed_chapters: Math.max(0, Number(summary.completed_chapters) || 0),
      total_chapters: Math.max(0, Number(summary.total_chapters) || 0),
      active_chapter_title: typeof summary.active_chapter_title === 'string' ? summary.active_chapter_title : null,
    }
  }
  const workflow = brief?.stage_workflow
  const stage = workflow?.stages.find(item => item.id === workflow.active_stage)
  if (!workflow || !stage) return null
  return { version: 2, phase: brief.phase, active_stage: workflow.active_stage, stage_status: stage.status,
    completed_chapters: workflow.chapters.filter(item => item.status === 'completed').length,
    total_chapters: workflow.chapters.length,
    active_chapter_title: workflow.chapters.find(item => item.id === workflow.active_chapter_id)?.title || null }
}

/** Presentation only: never replace an operation's durable lifecycle status. */
export function brainstormHistoryStatus(summary: BrainstormStageSummary | null, lifecycleStatus: string) {
  if (!summary) return null
  const detail = summary.total_chapters > 0
    ? `${summary.completed_chapters} / ${summary.total_chapters} 章已完成${summary.active_chapter_title && summary.phase !== 'completed' ? ` · 当前章节：${summary.active_chapter_title}` : ''}` : ''
  const stage = brainstormStageTitle(summary.active_stage)
  if (summary.phase === 'abandoned') return { label: '会话已终止', tone: 'cancelled', detail,
    description: '会话已终止，已确认的思路和已有正文仍可查看。', poll: false }
  if (summary.phase === 'completed') return { label: '全部章节已完成', tone: 'completed', detail,
    description: '全部章节已完成，可以打开文档继续修改。', poll: false }
  if (summary.stage_status === 'failed') return { label: `${stage}思考暂停`, tone: 'failed', detail,
    description: '阶段思考尚未完成，已保存完成的批次，点击查看恢复方式。', poll: false }
  if (summary.stage_status === 'thinking') return { label: `${stage}思考中`, tone: 'running', detail,
    description: '模型正在后台准备本阶段完整思路，点击查看进度。', poll: true }
  if (summary.phase === 'chapter_writing') {
    const paused = lifecycleStatus === 'failed' || lifecycleStatus === 'cancelled'
    return { label: paused ? '章节创作待继续' : '章节创作中', tone: paused ? 'failed' : 'running', detail,
      description: paused ? '已有章节内容已保留，点击继续当前章节。' : '当前章节正在创作与交付检查，通过后再进入下一章。',
      poll: !paused }
  }
  return { label: `${stage}待确认`, tone: 'waiting', detail,
    description: summary.active_stage === 'writing' ? '当前章节设计等待确认，已完成的章节内容已保留。' : '本阶段完整思路已准备好，点击继续确认。', poll: false }
}
