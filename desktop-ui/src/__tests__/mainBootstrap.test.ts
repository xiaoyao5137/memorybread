import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

describe('frontend bootstrap', () => {
  afterEach(() => {
    vi.doUnmock('../App')
    vi.restoreAllMocks()
    document.body.replaceChildren()
  })

  it('shows a recoverable error instead of a white page when the App module graph fails', async () => {
    document.body.innerHTML = '<div id="root"></div>'
    vi.resetModules()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.doMock('../App', () => {
      throw new SyntaxError(
        "gatewayChatStream.ts does not provide an export named 'fetchGatewayChat'",
      )
    })

    const { bootstrapPromise } = await import('../main')
    await bootstrapPromise

    expect(screen.getByRole('alert')).toHaveAttribute('data-bootstrap-state', 'failed')
    expect(screen.getByRole('heading', { name: '界面资源加载失败' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新加载' })).toBeInTheDocument()
    expect(screen.getByText(/error when mocking a module/i)).toBeInTheDocument()
  })
})
