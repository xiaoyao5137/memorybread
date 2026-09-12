// Development-only fixture: real component/styles, isolated storage, all fetches mocked.
import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import CreationPanel from '../src/components/CreationPanel'
import { useAppStore } from '../src/store/useAppStore'
import type { CreationBrainstormQuestion, CreationBrainstormState } from '../src/store/useAppStore'
import '../src/index.css'

const sessionId = 'brainstorm-draft-visual-fixture'
const rootRequest = '整理一份影像质量评估方案，先通过脑暴明确目标、评估指标和执行安排。'
const answeredQuestion = (id: string, dimension: string, prompt: string, label: string): CreationBrainstormQuestion => ({
  id, dimension, prompt, type: 'single_choice', required: true, allow_custom: true,
  why_now: '先确认评估范围，再细化执行方案。', answer_template: '补充你的想法。',
  options: [{ id: `${id}.confirmed`, label, description: '作为后续方案的已确认方向。' }],
})
const history = [
  {
    question: answeredQuestion('goal', '目标与决策', '这次评估主要服务什么目标？', '形成可复用的质量评估流程'),
    answer: { selected_option_ids: ['goal.confirmed'], custom_text: '', source: 'user' },
  },
  {
    question: answeredQuestion('metrics', '评估指标', '优先关注哪些质量指标？', '阴影合理性与色温均匀度'),
    answer: { selected_option_ids: ['metrics.confirmed'], custom_text: '', source: 'user' },
  },
]
const currentQuestion: CreationBrainstormQuestion = {
  id: 'execution.next', dimension: '光影一致性评分的落地执行安排', type: 'multi_choice',
  prompt: '承接“阴影合理性 + 色温均匀度”指标，具体执行安排是什么？',
  why_now: '将抽象指标转化为可执行的工程约束与验证条件，避免空泛讨论。',
  context_details: '当前方向：阴影合理性 + 色温均匀度。目标与指标已经确认，执行安排仍待讨论。',
  required: true, allow_custom: true, answer_template: '输入你希望采用的具体安排。',
  options: [
    { id: 'thresholds', label: '设定阈值与样本量', description: '定义具体合格阈值，并规划样本进行实测。', recommended: true },
    { id: 'budget', label: '先确认算力与 Token 预算', description: '检查现有资源能否支持试跑，再确定执行规模。' },
    { id: 'pilot', label: '先小批量试点验证', description: '先用小批量样本验证指标可行性，确认有效后再扩大。' },
  ],
}
const state: CreationBrainstormState = {
  session_id: sessionId, root_request: rootRequest, phase: 'exploring', revision: 2,
  current_question: currentQuestion, answered_count: 2, depth: 2,
  brief_markdown: '# 创作简报\n\n## 目标与决策\n- 已确认：形成可复用的质量评估流程。\n\n## 评估指标\n- 已确认：阴影合理性与色温均匀度。\n\n## 待确认\n- 阈值、样本量、预算与试点安排尚未决定。',
  can_continue_brainstorm: false, open_flags: ['具体执行安排待确认'],
  readiness_reason: '执行安排仍待讨论。', continuation_directions: [], invalidated_question_ids: [],
  history,
  decisions: history.map(item => ({
    question_id: item.question.id, dimension: item.question.dimension,
    summary: item.question.options[0].label, source: 'user',
  })),
}
const document = '# 影像质量评估方案（首版）\n\n## 目标\n建立可复用的质量评估流程。\n\n## 已确认指标\n关注阴影合理性与色温均匀度。\n\n## 待确认事项\n具体阈值、样本量、预算与试点安排尚未决定，后续继续补充。'
type RunPayload = { root_request?: string; creation_brief?: CreationBrainstormState }
let lastPayload: RunPayload | null = null
let brainstormTurnCount = 0
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), window.location.href)
  if (url.pathname === '/api/creation/agent/run') {
    lastPayload = JSON.parse(String(init?.body || '{}')) as RunPayload
    window.dispatchEvent(new Event('brainstorm-draft-preview:request'))
    const base = {
      schema_version: 'creation.agent.v1', session_id: sessionId, run_id: 'draft-preview-run',
      actor: { kind: 'agent', id: 'creation_main_agent', name: '创作 Agent' },
      timestamp: Date.now(), environment_patch: {},
    }
    const events = [
      { ...base, event_id: 'preview-1', sequence: 1, type: 'document.replaced', status: 'running', summary: '已基于当前确认内容生成首版', data: { content: document } },
      { ...base, event_id: 'preview-2', sequence: 2, type: 'run.completed', status: 'completed', summary: '首版文档已生成', data: { document } },
    ]
    return new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''), {
      headers: { 'Content-Type': 'text/event-stream' },
    })
  }
  if (url.pathname === '/api/creation/brainstorm/turn') {
    brainstormTurnCount += 1
    window.dispatchEvent(new Event('brainstorm-draft-preview:request'))
    return json(state)
  }
  if (url.pathname === '/api/creation/skills') return json([])
  if (url.pathname === '/api/creation/skills/match') return json({ matches: [] })
  if (url.pathname === '/api/creation/history/start') return json({ id: 900001, progress_epoch: 1 })
  if (url.pathname.endsWith('/progress')) return new Response(null, { status: 204 })
  if (url.pathname === '/api/creation/history') {
    return init?.method === 'POST' ? json({ id: 900001 }) : json({ items: [], total: 0 })
  }
  if (url.pathname === '/api/browser-integration/status') return json({ connected: false, jobs: [] })
  return new Response(JSON.stringify({ message: '开发预览未配置此接口' }), { status: 404, headers: { 'Content-Type': 'application/json' } })
}

useAppStore.setState({ apiBaseUrl: 'http://localhost:7070', currentUser: null })
useAppStore.getState().resetCreationDraft()
useAppStore.getState().setCreationDraft({
  creationMode: 'brainstorm', sessionId, rootRequest, brainstormState: state,
  docType: '评估方案', enableRag: false, enableWebSearch: false,
  conversation: [{ id: 'original-request', role: 'user', content: rootRequest, createdAt: Date.now() }],
})

function Preview() {
  const [payload, setPayload] = useState(lastPayload)
  const [turns, setTurns] = useState(brainstormTurnCount)
  const currentState = useAppStore(store => store.creationDraft.brainstormState)
  const paused = useAppStore(store => store.creationDraft.brainstormPaused)
  useEffect(() => {
    const update = () => { setPayload(lastPayload); setTurns(brainstormTurnCount) }
    window.addEventListener('brainstorm-draft-preview:request', update)
    return () => window.removeEventListener('brainstorm-draft-preview:request', update)
  }, [])
  const brief = payload?.creation_brief
  const onlyAnswered = JSON.stringify(brief?.history) === JSON.stringify(history)
  const noUnsubmittedAnswer = !brief?.history?.some(item => item.question.id === currentQuestion.id)
    && JSON.stringify(brief?.decisions) === JSON.stringify(state.decisions)
  const originalQuestionPreserved = currentState?.current_question?.id === currentQuestion.id
    && brief?.current_question?.id === currentQuestion.id
  return <>
    <main style={{ width: '100%', maxWidth: new URLSearchParams(location.search).has('narrow') ? 390 : undefined }}>
      <CreationPanel />
    </main>
    <aside role="status" aria-label="开发验收状态" style={{
      position: 'fixed', left: 8, bottom: 8, zIndex: 5000, pointerEvents: 'none',
      maxWidth: 330, padding: '8px 10px', border: '1px solid var(--mb-border-strong)',
      borderRadius: 8, background: 'var(--mb-bg-card)', color: 'var(--mb-text-secondary)',
      fontSize: 11, lineHeight: 1.6, boxShadow: 'var(--mb-shadow-card)',
    }}>
      <strong>开发验收 · 全部接口模拟</strong>
      {payload ? <div>
        <div>仅已答 2 题：{onlyAnswered ? '通过' : '失败'}；未提交选择未采纳：{noUnsubmittedAnswer ? '通过' : '失败'}</div>
        <div>原问题保留：{originalQuestionPreserved ? '通过' : '失败'}；原始需求保留：{payload.root_request === rootRequest ? '通过' : '失败'}</div>
      </div> : <div>待生成：已答 2 题，当前题尚未提交</div>}
      <div>脑暴提交请求：{turns} 次</div>
      {payload && <div>问题交互：{paused ? '已暂停，等待显式继续' : '已恢复'}</div>}
    </aside>
  </>
}
createRoot(window.document.getElementById('root')!).render(<Preview />)
