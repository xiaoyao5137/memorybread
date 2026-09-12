import { useCallback, useEffect, useRef, useState } from 'react'
import { getLocalServiceBaseUrl } from '../utils/localServices'

export interface ConsultationReadiness {
  ready: boolean
  runtime: boolean
  llm: boolean
  embedding: boolean
  message: string
  action: 'models' | 'initialization' | 'retry'
  error_code?: string | null
}
const unavailable: ConsultationReadiness = {
  ready: false, runtime: false, llm: false, embedding: false,
  message: '暂时无法连接本地 AI 服务，正在自动重新检查，请稍候。', action: 'retry',
  error_code: 'LOCAL_AI_STATUS_UNAVAILABLE',
}
export async function fetchConsultationReadiness(): Promise<ConsultationReadiness> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 8000)
  try {
    const response = await fetch(
      `${getLocalServiceBaseUrl('model_api')}/api/initialization/readiness`,
      { signal: controller.signal },
    )
    if (!response.ok) return response.status === 404
      ? { ...unavailable, message: '本地 AI 服务需要更新，请打开主界面检查。', action: 'initialization' }
      : unavailable
    const data = await response.json()
    if (typeof data.ready !== 'boolean' || typeof data.message !== 'string') return unavailable
    return data
  } catch { return unavailable }
  finally { clearTimeout(timeout) }
}

export function useConsultationReadiness() {
  const [status, setStatus] = useState<ConsultationReadiness>(unavailable)
  const [loading, setLoading] = useState(true)
  const mounted = useRef(false)
  const pending = useRef<Promise<ConsultationReadiness> | null>(null)
  const refresh = useCallback(async () => {
    if (!pending.current) pending.current = fetchConsultationReadiness()
    const next = await pending.current
    pending.current = null
    if (mounted.current) { setStatus(next); setLoading(false) }
    return next.ready
  }, [])
  useEffect(() => {
    mounted.current = true
    void refresh()
    const interval = setInterval(() => void refresh(), 10000)
    const focus = () => { void refresh() }
    window.addEventListener('focus', focus)
    return () => { mounted.current = false; clearInterval(interval); window.removeEventListener('focus', focus) }
  }, [refresh])
  return { status, ready: status.ready, loading, refresh }
}
