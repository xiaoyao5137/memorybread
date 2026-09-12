import { useEffect } from 'react'
import { invoke } from '@tauri-apps/api/core'

// Hold across OCR, retrieval, cloud/local generation and tool calls, not only model tokens.
// A crashed WebView cannot freeze background work: sidecar expires unrenewed leases.
export function useInteractiveOcrActivity(active: boolean) {
  useEffect(() => {
    if (!active) return
    const activityId = crypto.randomUUID()
    let pending: Promise<unknown> | null = null
    const renew = () => {
      if (pending) return
      pending = invoke('set_interactive_ocr_activity', { activityId, active: true })
        .catch(() => undefined).finally(() => { pending = null })
    }
    renew()
    const timer = window.setInterval(renew, 5000)
    return () => {
      window.clearInterval(timer)
      // Finish an in-flight renewal before release, avoiding a late renewal after cleanup.
      void (pending || Promise.resolve()).then(() =>
        invoke('set_interactive_ocr_activity', { activityId, active: false }),
      ).catch(() => undefined)
    }
  }, [active])
}
