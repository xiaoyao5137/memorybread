import type { CreationBrainstormStage, CreationBrainstormState, CreationBrainstormQuestion } from '../src/store/useAppStore'

export const stageQuestion = (id: string, dimension: string, parent?: string, option?: string): CreationBrainstormQuestion => ({
  id, dimension, prompt: `${dimension}应优先采用哪种思路？`, type: 'multi_choice', required: true, allow_custom: true,
  why_now: '明确创作取舍，让后续内容围绕已确认的目标展开。', answer_template: '补充你的想法。',
  parent_question_id: parent, parent_option_id: option,
  options: [
    { id: `${id}-a`, label: `${dimension}：先聚焦核心目标`, description: '先解释关键判断，再逐步展开执行安排。', recommended: true },
    { id: `${id}-b`, label: `${dimension}：覆盖完整场景`, description: '用多个场景对照，帮助读者理解不同方案的取舍。' },
  ],
})

export function stageFixture(status: CreationBrainstormStage['status'] = 'thinking', stageId: 'direction' | 'detail' | 'writing' = 'direction'): CreationBrainstormState {
  const roots = [stageQuestion(`${stageId}-goal`, '目标与读者'), stageQuestion(`${stageId}-scope`, '主题范围')]
  const questions = [...roots, ...roots.flatMap(root => root.options.map(option => stageQuestion(`${option.id}-child`, `${option.label}的展开`, root.id, option.id)))]
  const ids = ['direction', 'detail', 'writing'] as const
  const titles = ['创作方向', '创作细节', '创作行文']
  return {
    session_id: 'stages-fixture', root_request: '创作一份面向业务团队的影像质量评估方案，明确评估目标与逐步落地的方法。', revision: 5,
    phase: status === 'thinking' ? 'preparing' : status === 'ready' ? 'ready' : 'exploring',
    current_question: status === 'answering' ? questions[0] : null,
    brief_markdown: '# 创作简报', answered_count: 0, depth: 0, can_continue_brainstorm: false,
    open_flags: [], readiness_reason: '', continuation_directions: [], invalidated_question_ids: [], decisions: [], history: [],
    stage_workflow: {
      version: 2, active_stage: stageId,
      stages: ids.map((id, index) => ({
        id, title: titles[index], status: id === stageId ? status : index < ids.indexOf(stageId) ? 'completed' : 'pending',
        generation_id: `${id}-generation`, questions: id === stageId ? questions : [],
        ...(id === 'detail' && stageId === 'writing' ? { input_preflight: {
          schema_version: 1, status: 'matched' as const, period: { status: 'not_required' as const }, needs: [],
        } } : {}),
        progress: { completed_batches: status === 'thinking' ? 2 : 5, total_batches: 5, percent: status === 'thinking' ? 40 : 100,
          started_at_ms: Date.now() - 30000, updated_at_ms: Date.now(), estimated_remaining_seconds: status === 'thinking' ? 90 : 0 },
      })),
      chapters: stageId === 'writing' ? [
        { id: 'chapter-1', title: '目标与评估范围', goal: '说明为什么评估、面向哪些场景。', status: status === 'ready' ? 'ready' : 'answering', question_ids: questions.map(item => item.id) },
        { id: 'chapter-2', title: '指标设计与执行安排', goal: '定义评价方法与具体执行路径。', status: 'pending', question_ids: [] },
      ] : [],
      active_chapter_id: stageId === 'writing' ? 'chapter-1' : null,
    },
  }
}
