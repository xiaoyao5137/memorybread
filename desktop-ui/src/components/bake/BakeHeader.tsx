import React from 'react'
import { ArrowLeft } from 'lucide-react'
import type { BakeTab } from '../../types'
import TutorialLink, { TUTORIAL_URLS } from '../TutorialLink'
import { BakeCard } from './BakeShared'

const tutorialUrlByTab: Record<BakeTab, string> = {
  overview: TUTORIAL_URLS.memory,
  templates: TUTORIAL_URLS.documents,
  knowledge: TUTORIAL_URLS.knowledge,
  sop: TUTORIAL_URLS.operations,
  data: TUTORIAL_URLS.data,
}

const BakeHeader: React.FC<{
  title?: string
  subtitle?: string
  currentTab?: BakeTab
  backAction?: {
    label: string
    onClick: () => void
  }
}> = ({
  title = '记忆',
  subtitle,
  currentTab = 'overview',
  backAction,
}) => {
  return (
    <BakeCard>
      <div className="bake-header">
        <div className="bake-header__main">
          {backAction && (
            <button
              type="button"
              className="bake-header__back"
              aria-label={backAction.label}
              title={backAction.label}
              onClick={backAction.onClick}
            >
              <ArrowLeft size={18} aria-hidden="true" />
            </button>
          )}
          <div className="bake-header__copy">
            <div className="tutorial-title-row">
              <h1 className="bake-title">{title}</h1>
              <TutorialLink url={tutorialUrlByTab[currentTab]} />
            </div>
            {subtitle && <p className="bake-subtitle">{subtitle}</p>}
          </div>
        </div>
      </div>
    </BakeCard>
  )
}

export default BakeHeader
