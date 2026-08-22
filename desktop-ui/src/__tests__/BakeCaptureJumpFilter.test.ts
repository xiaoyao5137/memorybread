import { beforeEach, describe, expect, it } from 'vitest'
import { useAppStore } from '../store/useAppStore'

describe('采集关联跳转筛选条件', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
  })

  it('跳转到目标 capture 时清空已应用的筛选条件，避免目标被旧筛选过滤掉', () => {
    useAppStore.setState({
      repositoryCaptureQuery: '周报',
      repositoryCaptureApp: 'Google Chrome',
      repositoryCaptureFrom: '2026-08-01',
      repositoryCaptureTo: '2026-08-10',
      bakeCaptureOffset: 40,
    })

    useAppStore.getState().setRepositoryCaptureSourceCaptureId('42761')

    const state = useAppStore.getState()
    expect(state.repositoryCaptureSourceCaptureId).toBe('42761')
    expect(state.repositoryCaptureQuery).toBe('')
    expect(state.repositoryCaptureApp).toBe('')
    expect(state.repositoryCaptureFrom).toBe('')
    expect(state.repositoryCaptureTo).toBe('')
    expect(state.bakeCaptureOffset).toBe(0)
  })

  it('清除跳转状态时保留原有筛选条件', () => {
    useAppStore.setState({
      repositoryCaptureQuery: '周报',
      repositoryCaptureApp: 'Google Chrome',
    })

    useAppStore.getState().setRepositoryCaptureSourceCaptureId(null)

    const state = useAppStore.getState()
    expect(state.repositoryCaptureSourceCaptureId).toBeNull()
    expect(state.repositoryCaptureQuery).toBe('周报')
    expect(state.repositoryCaptureApp).toBe('Google Chrome')
  })
})
