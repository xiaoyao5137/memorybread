export const OPTIONAL_CLOUD_REQUEST_TIMEOUT_MS = 8_000

export const optionalCloudIsReachable = (): boolean => (
  typeof navigator === 'undefined' || navigator.onLine !== false
)

interface OptionalCloudRequestSignal {
  signal: AbortSignal
  dispose: () => void
}

/**
 * 云端增强能力必须有明确截止时间，并跟随所属页面或任务一起取消。
 * 本地核心能力不等待这个 signal，也不应根据云端结果决定是否可用。
 */
export const createOptionalCloudRequestSignal = (
  parentSignal?: AbortSignal,
  timeoutMs = OPTIONAL_CLOUD_REQUEST_TIMEOUT_MS,
): OptionalCloudRequestSignal => {
  const controller = new AbortController()
  const abortFromParent = () => controller.abort(parentSignal?.reason)

  if (parentSignal?.aborted) {
    abortFromParent()
  } else {
    parentSignal?.addEventListener('abort', abortFromParent, { once: true })
  }

  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  return {
    signal: controller.signal,
    dispose: () => {
      window.clearTimeout(timeout)
      parentSignal?.removeEventListener('abort', abortFromParent)
    },
  }
}
