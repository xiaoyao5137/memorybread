import type { CreationBrainstormState } from '../store/useAppStore'
import { brainstormDecisionSummary, brainstormDiscussionSummary } from '../utils/brainstormSummary'

export default function CreationBrainstormSummary({ state }: { state: CreationBrainstormState }) {
  const discussionSummary = brainstormDiscussionSummary(state)
  return <div className="creation-brainstorm-summary">
    <p>{brainstormDecisionSummary(state.decisions)}。</p>
    {discussionSummary && <p>{discussionSummary}</p>}
    {state.open_flags.length > 0 ? (
      <section className="creation-brainstorm-summary__open" aria-label="待决定与补充">
        <p>待决定与补充（{state.open_flags.length} 项）</p>
        <ul>{state.open_flags.map((flag, index) => <li key={index}>{flag}</li>)}</ul>
      </section>
    ) : <p>当前没有待决定与补充事项。</p>}
  </div>
}
