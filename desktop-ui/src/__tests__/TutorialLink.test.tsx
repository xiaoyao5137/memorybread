import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TutorialLink, { TUTORIAL_URLS } from '../components/TutorialLink'
import BakeHeader from '../components/bake/BakeHeader'

const appMetadataMocks = vi.hoisted(() => ({
  openExternalUrl: vi.fn(),
}))

vi.mock('../utils/appMetadata', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/appMetadata')>()),
  openExternalUrl: appMetadataMocks.openExternalUrl,
}))

describe('TutorialLink', () => {
  beforeEach(() => {
    appMetadataMocks.openExternalUrl.mockReset().mockResolvedValue(undefined)
  })

  it('使用系统浏览器打开指定的飞书教程', () => {
    render(<TutorialLink url={TUTORIAL_URLS.consult} />)

    fireEvent.click(screen.getByRole('button', { name: '查看教程（在浏览器中打开）' }))

    expect(appMetadataMocks.openExternalUrl).toHaveBeenCalledWith(TUTORIAL_URLS.consult)
  })

  it('记忆页会按当前子页面切换教程链接', () => {
    const { rerender } = render(<BakeHeader currentTab="knowledge" />)
    fireEvent.click(screen.getByRole('button', { name: '查看教程（在浏览器中打开）' }))
    expect(appMetadataMocks.openExternalUrl).toHaveBeenLastCalledWith(TUTORIAL_URLS.knowledge)

    rerender(<BakeHeader currentTab="data" />)
    fireEvent.click(screen.getByRole('button', { name: '查看教程（在浏览器中打开）' }))
    expect(appMetadataMocks.openExternalUrl).toHaveBeenLastCalledWith(TUTORIAL_URLS.data)
  })
})
