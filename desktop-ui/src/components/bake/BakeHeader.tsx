import React from 'react'
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
}> = ({
  title = '记忆',
  subtitle,
  currentTab = 'overview',
}) => {
  return (
    <BakeCard>
      <div className="bake-header">
        <div>
          <div className="tutorial-title-row">
            <h1 className="bake-title">{title}</h1>
            <TutorialLink url={tutorialUrlByTab[currentTab]} />
          </div>
          {subtitle && <p className="bake-subtitle">{subtitle}</p>}
        </div>
      </div>
    </BakeCard>
  )
}

export default BakeHeader
