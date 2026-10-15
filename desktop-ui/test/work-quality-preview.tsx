// Development acceptance uses the real Core/sidecar routes and isolated databases.
import React from 'react'
import { createRoot } from 'react-dom/client'
import CreationPanel from '../src/components/CreationPanel'
import { useAppStore } from '../src/store/useAppStore'
import { emptyWorkContext } from '../src/utils/creationWorkContext'
import '../src/index.css'

useAppStore.setState({ apiBaseUrl: 'http://127.0.0.1:17071', currentUser: null })
useAppStore.getState().setCreationDraft({ rootRequest: '基于给定内容形成素材交接试点评审，不使用历史资料或联网。',
  generatedContent: '# 素材交接方案\n\n通过多维能力协同持续赋能增长，构建高效闭环。后续分阶段推进，持续优化并形成长效机制。',
  workContext: { ...emptyWorkContext(), purpose: '内部方案评审', decision_goal: '是否同意开展素材交接试点',
    constraints: '负责人、日期、资源和效果尚未确认，不能写成承诺。', profile_id: 'proposal_review', review_enabled: true } })
createRoot(document.getElementById('root')!).render(<div style={{ height: '100vh', background: 'var(--mb-bg-base)' }}><CreationPanel /></div>)
