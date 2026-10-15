// Development-only preview. Storage is isolated by the HTML bootstrap and all APIs are mocked.
import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import CreationPanel from '../src/components/CreationPanel'
import { useAppStore } from '../src/store/useAppStore'
import { stageFixture } from './brainstorm-stage-fixture'
import type { CreationBrainstormStageId, CreationBrainstormState } from '../src/store/useAppStore'
import { normalizeBrainstormStageSummary } from '../src/utils/brainstormStageStatus'
import '../src/index.css'

const query = new URLSearchParams(location.search)
const mode = query.get('mode') || 'thinking'
let state = stageFixture(mode === 'writing' ? 'ready' : mode === 'answering' ? 'answering' : mode === 'failed' ? 'failed' : 'thinking', mode === 'writing' ? 'writing' : 'direction')
if (mode === 'failed') state.stage_workflow!.stages[0].error = { code: 'MODEL_UNAVAILABLE', message: '模型连接中断，已保存前两批思路。继续后从未完成的批次恢复。', retryable: true }
let documentBody = ''
let sequence = 0
const json = (value: unknown) => Response.json(value)
function readyQuestions() {
  const stageId = state.stage_workflow!.active_stage
  const ready = stageFixture('answering', stageId)
  const stage = state.stage_workflow!.stages.find(item => item.id === stageId)!
  Object.assign(stage, ready.stage_workflow!.stages.find(item => item.id === stageId))
  state.current_question = stage.questions[0]
  state.phase = 'exploring'
  state.revision++
}
window.fetch = async (input, init) => {
  const path = new URL(input instanceof Request ? input.url : String(input), location.href).pathname
  const payload = JSON.parse(String(init?.body || '{}'))
  if (path === '/api/creation/brainstorm/turn') {
    const workflow = state.stage_workflow!
    const stage = workflow.stages.find(item => item.id === workflow.active_stage)!
    if (payload.action === 'answer' || payload.action === 'skip' || payload.action === 'exclude') {
      const question = state.current_question!
      const answer = { selected_option_ids: payload.answer?.selected_option_ids || [], custom_text: payload.answer?.custom_text || '', source: payload.action === 'answer' ? 'user' : payload.action === 'exclude' ? 'user_excluded' : 'agent_assumption' }
      state.history!.push({ question, answer })
      state.decisions.push({ question_id: question.id, dimension: question.dimension,
        summary: [...question.options.filter(item => answer.selected_option_ids.includes(item.id)).map(item => item.label), answer.custom_text].filter(Boolean).join('；'), source: answer.source })
      const answered = new Set(state.history!.map(item => item.question.id))
      state.current_question = stage.questions.find(item => !answered.has(item.id) && (!item.parent_question_id || state.history!.some(turn => turn.question.id === item.parent_question_id && turn.answer.selected_option_ids.includes(item.parent_option_id || '')))) || null
      state.answered_count = state.history!.length
      if (!state.current_question) {
        state.phase = 'ready'; stage.status = 'ready'
        if (workflow.active_stage === 'writing') workflow.chapters.find(item => item.id === workflow.active_chapter_id)!.status = 'ready'
      }
      state.revision++
    }
    if (payload.action === 'confirm_stage') {
      stage.status = 'completed'
      const nextId = workflow.active_stage === 'direction' ? 'detail' : 'writing'
      workflow.active_stage = nextId
      const next = stageFixture('thinking', nextId)
      Object.assign(workflow.stages.find(item => item.id === nextId)!, next.stage_workflow!.stages.find(item => item.id === nextId))
      workflow.chapters = next.stage_workflow!.chapters
      workflow.active_chapter_id = next.stage_workflow!.active_chapter_id
      state.phase = 'preparing'; state.revision++
    }
    if (payload.action === 'retry_stage') { stage.status = 'thinking'; state.phase = 'preparing'; state.revision++ }
    if (payload.action === 'confirm_chapter') {
      const chapter = workflow.chapters.find(item => item.id === payload.chapter_id)!
      chapter.status = 'writing'; chapter.instruction_id = `preview-${chapter.id}-instruction`; state.phase = 'chapter_writing'; state.revision++
    }
    if (payload.action === 'chapter_completed') {
      const chapter = workflow.chapters.find(item => item.id === payload.chapter_id)!
      chapter.status = 'completed'
      const next = workflow.chapters.find(item => item.status !== 'completed')
      workflow.active_chapter_id = next?.id || null
      if (next) { next.status = 'ready'; state.phase = 'ready' }
      else { stage.status = 'completed'; state.phase = 'completed' }
      state.revision++
    }
    if (payload.action === 'edit_brief') { state.brief_edits = { ...state.brief_edits, ...payload.brief_edits }; state.revision++ }
    return json(structuredClone(state))
  }
  if (path === '/api/creation/agent/run') {
    const chapter = state.stage_workflow!.chapters.find(item => item.id === payload.brainstorm_chapter_id)!
    const encoder = new TextEncoder()
    const base = { schema_version: 'creation.agent.v1', session_id: state.session_id, run_id: `preview-${chapter.id}-run`, actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' }, timestamp: Date.now(), environment_patch: {} }
    const stream = new ReadableStream({ start(controller) {
      const push = (type: string, data: Record<string, unknown>, summary: string, status = 'running') => {
        sequence++
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ ...base, event_id: `preview-${sequence}`, sequence, type, status, summary, data })}\n\n`))
      }
      documentBody += `${documentBody ? '\n\n' : '# 影像质量评估方案\n\n'}## ${chapter.title}\n\n${chapter.goal}\n\n本章围绕已经确认的方向展开，通过明确目标、定义边界和设定可核验的评价方式，形成供业务团队共同讨论的初步方案。`
      push('run.started', {}, '正在创作本章内容')
      setTimeout(() => { push('document.replaced', { content: documentBody }, '本章内容已保存，可实时查看') }, 1600)
      setTimeout(() => { push('delivery.checked', { status: 'pass' }, '本章交付检查通过'); push('run.completed', { document: documentBody }, '本章已完成', 'completed'); controller.close() }, 3200)
    } })
    return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
  }
  if (path === '/api/creation/skills') return json([])
  if (path === '/api/creation/skills/match') return json({ matches: [] })
  if (path === '/api/creation/history/start') return json({ id: 901, progress_epoch: 1 })
  if (path.endsWith('/progress')) return new Response(null, { status: 204 })
  if (path === '/api/creation/history') {
    if (init?.method === 'POST') return json({ id: 901 })
    if (!query.has('history')) return json({ items: [], total: 0 })
    const current = { id: 901, session_id: state.session_id, root_request: state.root_request, creation_mode: 'brainstorm',
      generated_content: documentBody, lifecycle_status: 'completed', creation_brief_json: JSON.stringify(state),
      brainstorm_stage_summary: normalizeBrainstormStageSummary(null, state), updated_at: Date.now(), model: 'mbcd-plus-v1' }
    const chapterSummary = { version: 2, active_stage: 'writing', completed_chapters: 1, total_chapters: 2, active_chapter_title: '指标设计与执行安排' }
    return json({ items: [current,
      { ...current, id: 902, root_request: '第一章已交付，等待确认第二章', generated_content: '# 已完成的第一章', brainstorm_stage_summary: { ...chapterSummary, phase: 'ready', stage_status: 'ready' } },
      { ...current, id: 903, root_request: '正在创作第二章', generated_content: '# 已完成的第一章', brainstorm_stage_summary: { ...chapterSummary, phase: 'chapter_writing', stage_status: 'answering' } },
      { ...current, id: 904, root_request: '全部章节已交付', generated_content: '# 完整文章', brainstorm_stage_summary: { ...chapterSummary, phase: 'completed', stage_status: 'completed', completed_chapters: 2, active_chapter_title: null } },
    ], total: 4 })
  }
  return json({ connected: false })
}
useAppStore.setState({ apiBaseUrl: 'http://localhost:7070', currentUser: null })
useAppStore.getState().resetCreationDraft()
useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', sessionId: state.session_id, rootRequest: state.root_request,
  brainstormState: structuredClone(state), brainstormPaused: false, enableRag: false, enableWebSearch: false,
  conversation: [{ id: 'root', role: 'user', content: state.root_request!, createdAt: Date.now() }] })

function Preview() {
  const [active, setActive] = useState(true)
  const load = (stageId: CreationBrainstormStageId, thinking: boolean) => {
    state = stageFixture(thinking ? 'thinking' : stageId === 'writing' ? 'ready' : 'answering', stageId)
    useAppStore.getState().setCreationDraft({ brainstormState: structuredClone(state), generatedContent: '', agentEvents: [], brainstormPaused: false })
    documentBody = ''
  }
  return <>
    <aside aria-label="开发预览控制" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', padding: '10px 18px', fontSize: 12, background: '#f5f5f5', borderBottom: '1px solid #ddd' }}>
      <strong>开发验收 · 全部接口模拟</strong>
      <button onClick={() => load('direction', true)}>阶段思考</button>
      <button onClick={() => load('direction', false)}>完整问题</button>
      <button onClick={() => load('writing', false)}>逐章创作</button>
      <button onClick={() => { readyQuestions() }}>完成后台思考</button>
      <button onClick={() => setActive(value => !value)}>{active ? '切出页面' : '返回创作'}</button>
    </aside>
    {!active && <p style={{ padding: 20 }}>已切出创作页。可点击“完成后台思考”，再返回检查进度恢复。</p>}
    <main style={{ width: query.has('narrow') ? 390 : '100%', maxWidth: '100%', display: active ? undefined : 'none' }}><CreationPanel active={active} /></main>
  </>
}
createRoot(document.getElementById('root')!).render(<Preview />)
