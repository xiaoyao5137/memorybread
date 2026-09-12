// Development-only visual fixture. All requests are mocked; no user state or backend writes.
import React from 'react'
import { createRoot } from 'react-dom/client'
import OnboardingWizard from '../src/components/OnboardingWizard'
const stages = [
  ['preflight', '检查运行环境'], ['inference_engine', '准备本地 AI 引擎'],
  ['capture_model', '准备采集提炼能力'], ['vector_model', '准备语义检索能力'],
  ['database', '准备本地记忆库'], ['skills_tools', '准备技能与工具'],
  ['quality_gate', '执行完整质检'], ['feature_smoke_tests', '验证核心功能'],
]
window.fetch = async () => new Response(JSON.stringify({status:'ok', initialization: {
  schema_version:'initialization.v1', run_id:'visual-fixture', mode:'normal', state:'failed',
  progress:68, current_stage:'database', error_code:'DATABASE_READ_ONLY',
  message:'本地记忆库或所在目录为只读', suggestion:'请恢复记忆面包数据目录的写入权限后重试。',
  can_retry:true, can_report:true, test_mode_enabled:false, quality_gate:{passed:false,checks:[]}, smoke_tests:[],
  stages:stages.map(([id,label],i)=>({id,label,status:i<4?'succeeded':i===4?'failed':'pending'})),
}}), {headers:{'Content-Type':'application/json'}})
createRoot(document.getElementById('root')!).render(<OnboardingWizard />)
