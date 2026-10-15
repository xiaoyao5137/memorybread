import thinkingImage from '../assets/creation-trace/thinking.png'
import agentImage from '../assets/creation-trace/agent.png'
import skillImage from '../assets/creation-trace/skill.png'
import toolImage from '../assets/creation-trace/tool.png'
import memorySearchImage from '../assets/creation-trace/memory_search.png'
import internetSearchImage from '../assets/creation-trace/internet_search.png'
import dataSearchImage from '../assets/creation-trace/data_search.png'
import webpageScrapeImage from '../assets/creation-trace/webpage_scrape.png'
import githubSearchImage from '../assets/creation-trace/github_search.png'
import plantUmlImage from '../assets/creation-trace/plantuml_diagram.png'
import mermaidImage from '../assets/creation-trace/mermaid_diagram.png'
import industryResearchImage from '../assets/creation-trace/industry_research_agent.png'
import dataAnalysisImage from '../assets/creation-trace/data_analysis_agent.png'
import solutionDesignImage from '../assets/creation-trace/solution_design_agent.png'
import chapterDesignImage from '../assets/creation-trace/chapter_design_agent.png'
import documentWriterImage from '../assets/creation-trace/document_writer_agent.png'
import qualityReviewImage from '../assets/creation-trace/quality_review_agent.png'
import antiAiStyleImage from '../assets/creation-trace/anti_ai_style_agent.png'
import detailPolishImage from '../assets/creation-trace/detail_polish_agent.png'
import tablePolishImage from '../assets/creation-trace/table_polish_agent.png'
import typographyPolishImage from '../assets/creation-trace/typography_polish_agent.png'
import imagePolishImage from '../assets/creation-trace/image_polish_agent.png'
import type { CreationAgentEvent } from '../store/useAppStore'

// Execution status is separate from the actor's visual identity. This keeps
// the same generated emblem visible while a step runs and after it finishes.
const TOOL_IMAGES: Record<string, string> = {
  memory_search: memorySearchImage,
  internet_search: internetSearchImage,
  data_search: dataSearchImage,
  webpage_scrape: webpageScrapeImage,
  github_search: githubSearchImage,
  plantuml_diagram: plantUmlImage,
  mermaid_diagram: mermaidImage,
}

const AGENT_IMAGES: Record<string, string> = {
  industry_research_agent: industryResearchImage,
  data_analysis_agent: dataAnalysisImage,
  solution_design_agent: solutionDesignImage,
  chapter_design_agent: chapterDesignImage,
  document_writer_agent: documentWriterImage,
  quality_review_agent: qualityReviewImage,
  anti_ai_style_agent: antiAiStyleImage,
  detail_polish_agent: detailPolishImage,
  table_polish_agent: tablePolishImage,
  typography_polish_agent: typographyPolishImage,
  image_polish_agent: imagePolishImage,
}

const FALLBACK_IMAGES = { agent: agentImage, skill: skillImage, tool: toolImage }

export function CreationTraceIcon({ event, status }: {
  event: CreationAgentEvent
  status: string
}) {
  const kind = event.actor?.kind || 'agent'
  const id = event.actor?.id || ''
  const configured = kind === 'tool' && Object.prototype.hasOwnProperty.call(TOOL_IMAGES, id)
    ? TOOL_IMAGES[id]
    : kind === 'agent' && Object.prototype.hasOwnProperty.call(AGENT_IMAGES, id)
      ? AGENT_IMAGES[id]
      : undefined
  const fallbackKind = kind === 'tool' || kind === 'skill' ? kind : 'agent'
  // An actor keeps the same image across events, stages and status changes.
  const image = configured || FALLBACK_IMAGES[fallbackKind]
  const iconKey = configured ? id : fallbackKind
  const isTerminal = ['completed', 'warning', 'failed', 'cancelled'].includes(status)

  return (
    <span className="creation-agent-event__icon" data-trace-icon={iconKey} aria-hidden="true">
      <img src={image} alt="" draggable={false} />
      {isTerminal && (
        <span className="creation-agent-event__status" data-status={status}>
          <span className={`creation-agent-event__dot is-${status === 'completed' ? 'done' : status}`} />
        </span>
      )}
      {status === 'running' && <span className="creation-agent-event__activity" />}
    </span>
  )
}

export function CreationThinkingIcon() {
  return (
    <span className="creation-trace-thinking__icon" data-trace-icon="thinking" aria-hidden="true">
      <img src={thinkingImage} alt="" draggable={false} />
      <span className="creation-trace-thinking__dot" />
    </span>
  )
}
