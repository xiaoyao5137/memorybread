import { Check, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { CreationBrainstormStage, CreationBrainstormStageWorkflow } from '../store/useAppStore'
import { brainstormStageTitle } from '../utils/brainstormStageStatus'
export { brainstormStageTitle } from '../utils/brainstormStageStatus'

const statusLabels: Record<CreationBrainstormStage['status'], string> = {
  pending: '待开始', thinking: '模型思考中', answering: '待你确认', ready: '待阶段确认', completed: '已完成', failed: '思考暂停',
}

function remainingLabel(seconds: number | null, expired: boolean) {
  if (seconds === null || !Number.isFinite(seconds)) return '预计耗时：正在估算'
  if (expired) return '耗时超出预估，正在继续'
  if (seconds < 60) return `预计剩余约 ${Math.max(1, Math.ceil(seconds))} 秒 · 按已完成批次估算`
  return `预计剩余约 ${Math.ceil(seconds / 60)} 分钟 · 按已完成批次估算`
}

export default function CreationBrainstormStages({ workflow, busy, synchronizing = false, reviewInputsAvailable = false,
  onConfirmStage, onConfirmChapter, onRetry, onReviewInputs }: {
  workflow: CreationBrainstormStageWorkflow
  busy: boolean
  synchronizing?: boolean
  reviewInputsAvailable?: boolean
  onConfirmStage: () => void
  onConfirmChapter: () => void
  onRetry: () => void
  onReviewInputs?: () => void
}) {
  const stage = workflow.stages.find(item => item.id === workflow.active_stage)
  const chapter = workflow.chapters.find(item => item.id === workflow.active_chapter_id)
  const progress = stage?.progress
  const preparation = stage?.preparation_progress
  const recovering = stage?.status === 'thinking' && Boolean(stage.recovery_progress)
  const detailStage = workflow.stages.find(item => item.id === 'detail')
  const questionKey = (prompt: string) => prompt.replace(/[^\p{L}\p{N}]+/gu, '').toLocaleLowerCase()
  const confirmedEarlierQuestions = new Set(workflow.stages
    .filter(item => item.id !== 'writing' && item.status === 'completed')
    .flatMap(item => item.questions.map(question => questionKey(question.prompt))))
  const repeatedWritingQuestion = workflow.active_stage === 'writing' && stage?.status === 'ready'
    && stage.questions.some(question => confirmedEarlierQuestions.has(questionKey(question.prompt)))
  const stagesWithoutContentQuestions = workflow.stages.filter(item =>
    (item.status === 'ready' || item.status === 'completed')
    && Boolean(item.content_no_new_decision_reason?.trim()))
  const needsInputReview = reviewInputsAvailable && workflow.active_stage === 'writing'
    && Boolean(detailStage) && !detailStage?.input_preflight
  const percent = Math.max(0, Math.min(100, Number(progress?.percent) || 0))
  const [expiredEstimate, setExpiredEstimate] = useState<number | null>(null)
  const seconds = recovering || synchronizing ? null : progress?.estimated_remaining_seconds
  const estimateDeadline = typeof seconds === 'number' && Number.isFinite(seconds) && progress && progress.updated_at_ms > 0
    ? progress.updated_at_ms + Math.max(0, seconds) * 1000 : null
  const estimateExpired = estimateDeadline !== null && (Date.now() >= estimateDeadline || expiredEstimate === estimateDeadline)
  useEffect(() => {
    if (stage?.status !== 'thinking' || estimateDeadline === null || estimateDeadline <= Date.now()) return
    // Only mark a persisted estimate as expired. Elapsed wall time cannot
    // advance model progress or invent a new remaining-duration estimate.
    const timer = window.setTimeout(() => setExpiredEstimate(estimateDeadline), estimateDeadline - Date.now() + 1)
    return () => window.clearTimeout(timer)
  }, [stage?.status, estimateDeadline])
  return <section className="creation-brainstorm-stages" aria-label="三阶段创作脑暴">
    <ol className="creation-brainstorm-stages__steps">
      {workflow.stages.map((item, index) => <li key={item.id} aria-current={item.id === workflow.active_stage ? 'step' : undefined}
        className={item.status === 'completed' ? 'is-completed' : item.id === workflow.active_stage ? 'is-active' : ''}>
        <span className="creation-brainstorm-stages__number" aria-hidden>{item.status === 'completed' ? <Check size={13} /> : index + 1}</span>
        <span><strong>{brainstormStageTitle(item.id)}</strong><small>{synchronizing && item.id === workflow.active_stage ? '进度同步中' : statusLabels[item.status]}</small></span>
      </li>)}
    </ol>
    {stagesWithoutContentQuestions.map(item => <p key={item.id} className="creation-brainstorm-stages__hint">
      {brainstormStageTitle(item.id)}未新增内容题：{item.content_no_new_decision_reason?.trim()}
    </p>)}
    {stage && (stage.status === 'thinking' || synchronizing) && <div className="creation-brainstorm-stages__thinking" role="status">
      <div className="creation-brainstorm-stages__thinking-title"><Loader2 size={19} className="spin" aria-hidden />
        <strong>{synchronizing ? `正在同步${brainstormStageTitle(stage.id)}进度` : `${brainstormStageTitle(stage.id)}阶段模型思考中`}</strong><span>{percent}%</span></div>
      <p>{synchronizing ? '正在读取已保存的思考进度。' : recovering ? '正在结合已确认内容完善本阶段思路。' : '正在分批准备本阶段的完整问题、选项和下钻思路。'}</p>
      <div className="creation-brainstorm-stages__progress" role="progressbar" aria-label={`${brainstormStageTitle(stage.id)}思考进度`}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="creation-brainstorm-stages__metrics"><span>{progress?.total_batches ? `已完成 ${progress.completed_batches || 0} / ${progress.total_batches} 批` : '正在规划本阶段批次'}</span>
        <span>{remainingLabel(seconds ?? null, estimateExpired)}</span></div>
      {preparation && <p className="creation-brainstorm-stages__hint">{preparation.topic_title}：已保存 {preparation.saved_nodes} 道题，待准备 {preparation.pending_nodes} 项思路。</p>}
      <p className="creation-brainstorm-stages__hint">{synchronizing ? '已确认内容和已保存进度会保留，同步后自动显示当前状态。' : '你可以先切换到其他页面，思考会在后台继续。回来后自动恢复进度。'}</p>
    </div>}
    {stage?.status === 'failed' && !synchronizing && <div className="creation-brainstorm-stages__failure" role="alert">
      <strong>{brainstormStageTitle(stage.id)}阶段思考暂停</strong>
      <p>{stage.error?.message || '本阶段尚未准备完成，已保存完成的批次和之前确认的内容。'}</p>
      {preparation && <p className="creation-brainstorm-stages__hint">{preparation.topic_title}：已保存 {preparation.saved_nodes} 道题，待准备 {preparation.pending_nodes} 项思路。继续后从未完成处接续。</p>}
      <button type="button" onClick={onRetry} disabled={busy || stage.error?.retryable === false}>继续本阶段思考</button>
    </div>}
    {stage?.status === 'answering' && <p className="creation-brainstorm-stages__hint">{stage.preparation_contract_version && stage.preparation_contract_version >= 17
      ? `本阶段已准备 ${stage.questions.length} 题，请逐题确认。可在创作简报中查看全部候选问题与选项。`
      : `本阶段已准备 ${stage.questions.length} 题，按选择展开。可在创作简报中查看所有候选分支，选择后直接进入已准备的问题。`}</p>}
    {stage?.status === 'ready' && workflow.active_stage !== 'writing' && <div className="creation-brainstorm-stages__confirm">
      <p>{stage.content_no_new_decision_reason?.trim()
        ? `${brainstormStageTitle(stage.id)}没有新增待确认的内容题。确认后开始下一阶段思考。`
        : `${brainstormStageTitle(stage.id)}已逐项确认。确认阶段结果后，开始下一阶段思考。`}</p>
      <button type="button" onClick={onConfirmStage} disabled={busy}>确认{brainstormStageTitle(stage.id)}，进入{workflow.active_stage === 'direction' ? '创作细节' : '创作行文'}</button>
    </div>}
    {needsInputReview && onReviewInputs && <div className="creation-brainstorm-stages__confirm" role="status">
      <p>这次会话尚未在创作细节核对报告周期和本地资料。返回补齐选题时，已确认答案与现有正文会保留。</p>
      <button type="button" onClick={onReviewInputs} disabled={busy}>补齐资料选题</button>
    </div>}
    {repeatedWritingQuestion && <p className="creation-brainstorm-stages__hint" role="status">
      本章行文题与前面已确认的问题重复，原有选择已保留。无需再回答同一道题，可继续确认本章设计。
    </p>}
    {workflow.active_stage === 'writing' && workflow.chapters.length > 0 && <section className="creation-brainstorm-stages__chapters" aria-label="章节创作进度">
      <div><strong>逐章创作</strong><span>{workflow.chapters.filter(item => item.status === 'completed').length} / {workflow.chapters.length} 章已完成</span></div>
      <ol>{workflow.chapters.map((item, index) => <li key={item.id} aria-current={item.id === chapter?.id ? 'step' : undefined}>
        <span>{index + 1}. {item.title}</span><small>{{ pending: '待设计', answering: '设计确认中', ready: '设计已确认', writing: '内容创作中', completed: '内容已完成' }[item.status]}</small>
      </li>)}</ol>
      {chapter && !needsInputReview && ['ready', 'writing'].includes(chapter.status) && <div className="creation-brainstorm-stages__confirm">
        <p>{chapter.status === 'writing' ? '正在完成本章内容与交付检查，通过后继续下一章。已有内容会实时显示在创作文档中。'
          : stage?.content_no_new_decision_reason?.trim()
            ? `“${chapter.title}”没有新增待确认的内容题，确认后开始创作。`
            : `“${chapter.title}”的设计已确认，接下来创作本章内容。`}</p>
        <button type="button" onClick={onConfirmChapter} disabled={busy}>{busy && chapter.status === 'writing' ? <><Loader2 size={14} className="spin" /> 本章创作中</> : chapter.status === 'writing' ? '继续本章创作' : '确认本章设计并创作'}</button>
      </div>}
    </section>}
  </section>
}
