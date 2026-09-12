// Isolated visual fixture. No real sessions, storage, inference or API writes.
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import CreationPanel from '../src/components/CreationPanel'
import { useAppStore, type CreationBrainstormQuestion, type CreationBrainstormState } from '../src/store/useAppStore'
import '../src/index.css'

const sessionId = 'brainstorm-siblings-visual-fixture'
const rootRequest = '设计一场社区阅读活动，完整讨论活动内容与参与体验。'
const root: CreationBrainstormQuestion = {
  id: 'root', dimension: '活动方向', prompt: '这次活动优先探索哪些方向？', type: 'multi_choice',
  why_now: '明确希望探索的活动内容。', required: true, allow_custom: true, answer_template: '',
  options: [
    { id: 'reading', label: '共读与交流', description: '围绕书籍内容建立互动。' },
    { id: 'participation', label: '参与体验', description: '帮助不同读者融入活动。' },
  ],
}
const makeQuestion = (id: string, dimension: string, prompt: string, parentOption: string,
  options: CreationBrainstormQuestion['options']): CreationBrainstormQuestion => ({
  id, dimension, prompt, type: 'multi_choice', exploration_stage: 'solutions',
  parent_question_id: root.id, parent_option_id: parentOption,
  why_now: '从不同角度完整展开这个已经确认的思路。',
  context_details: '本开发示例只使用虚构活动需求与模拟答案。选项为开放推演，无需历史记忆。',
  required: true, allow_custom: true, answer_template: '可补充偏好或限制。', options,
})
const questions = [
  makeQuestion('reading.content', '共读内容', '如何组织共读的内容？', 'reading', [
    { id: 'a', label: '同读一篇短文', description: '准备门槛低，更容易围绕同一主题交流。' },
    { id: 'b', label: '各自分享一本书', description: '题材更丰富，需要主持人串联讨论。' },
  ]),
  makeQuestion('reading.exchange', '交流形式', '用什么形式让共读讨论更充分？', 'reading', [
    { id: 'a', label: '小组轮流交流', description: '每个人都有表达机会，之后汇总共同发现。' },
    { id: 'b', label: '自由圆桌讨论', description: '交流更自然，需要适度引导保持主题。' },
  ]),
  makeQuestion('participation.welcome', '新人参与', '怎样帮助第一次来的读者融入？', 'participation', [
    { id: 'a', label: '先做简短破冰', description: '用轻松问题帮助成员熟悉彼此。' },
    { id: 'b', label: '安排共读伙伴', description: '由熟悉活动的成员主动引导。' },
  ]),
]
let state: CreationBrainstormState = {
  session_id: sessionId, root_request: rootRequest, phase: 'exploring', revision: 1,
  current_question: questions[0], answered_count: 1, depth: 1, brief_markdown: '# 社区阅读活动简报',
  can_continue_brainstorm: false, open_flags: ['共读内容与交流形式待确认'], readiness_reason: '',
  continuation_directions: [], invalidated_question_ids: [],
  history: [{ question: root, answer: { selected_option_ids: ['reading', 'participation'], custom_text: '', source: 'user' } }],
  decisions: [{ question_id: root.id, dimension: root.dimension, summary: '共读与交流；参与体验', source: 'user' }],
}
let submissions = 0
const json = (value: unknown) => Response.json(value)
window.fetch = async (input, init) => {
  const path = new URL(input instanceof Request ? input.url : String(input), location.href).pathname
  if (path === '/api/creation/brainstorm/turn') {
    const payload = JSON.parse(String(init?.body || '{}'))
    if (payload.action === 'start') return json(state)
    if (payload.action !== 'answer' || payload.revision !== state.revision || payload.question_id !== state.current_question?.id) {
      return json({ code: 'BRAINSTORM_MODEL_OUTPUT_INVALID', message: '开发示例仅支持顺序回答当前问题。' })
    }
    const current = state.current_question
    const index = questions.findIndex(question => question.id === current.id)
    const selected = current.options.filter(option => payload.answer.selected_option_ids.includes(option.id))
    state = { ...state, revision: state.revision + 1, answered_count: state.answered_count + 1,
      depth: state.depth + 1, current_question: questions[index + 1] || null,
      phase: questions[index + 1] ? 'exploring' : 'ready',
      history: [...state.history!, { question: current, answer: { ...payload.answer, source: 'user' } }],
      decisions: [...state.decisions, { question_id: current.id, dimension: current.dimension,
        summary: selected.map(option => option.label).join('；') || payload.answer.custom_text, source: 'user' }],
    }
    submissions += 1
    window.dispatchEvent(new Event('siblings-preview:submission'))
    return json(state)
  }
  if (path === '/api/creation/skills') return json([])
  if (path === '/api/creation/skills/match') return json({ matches: [] })
  if (path === '/api/creation/history') return json({ items: [], total: 0 })
  if (path === '/api/browser-integration/status') return json({ connected: false, jobs: [] })
  return new Response(JSON.stringify({ message: '隔离开发示例未启用此操作' }), { status: 404 })
}
useAppStore.setState({ apiBaseUrl: 'http://localhost:7070', currentUser: null })
useAppStore.getState().resetCreationDraft()
useAppStore.getState().setCreationDraft({ creationMode: 'brainstorm', sessionId, rootRequest, brainstormState: state,
  enableRag: false, enableWebSearch: false,
  conversation: [{ id: 'fixture-request', role: 'user', content: rootRequest, createdAt: Date.now() }],
})

function Preview() {
  const [count, setCount] = useState(submissions)
  useEffect(() => {
    const update = () => setCount(submissions)
    window.addEventListener('siblings-preview:submission', update)
    return () => window.removeEventListener('siblings-preview:submission', update)
  }, [])
  return <>
    <main style={{ width: '100%', maxWidth: new URLSearchParams(location.search).has('narrow') ? 390 : undefined }}><CreationPanel /></main>
    <aside role="status" aria-label="隔离开发验收状态" style={{ position: 'fixed', left: 8, bottom: 8, zIndex: 5000,
      pointerEvents: 'none', padding: '8px 12px', border: '1px solid var(--mb-border-strong)', borderRadius: 8,
      background: 'var(--mb-bg-card)', color: 'var(--mb-text-secondary)', fontSize: 11 }}>
      <strong>隔离开发验收 · 全部接口模拟</strong><div>本页确认提交：{count} 次；真实会话写入：0 次</div>
    </aside>
  </>
}
createRoot(document.getElementById('root')!).render(<Preview />)
