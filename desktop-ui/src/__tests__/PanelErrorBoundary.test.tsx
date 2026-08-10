import React from 'react'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PanelErrorBoundary from '../components/PanelErrorBoundary'

const BrokenPanel = () => {
  throw new Error('render failed')
}

describe('PanelErrorBoundary', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('页面渲染异常时保留可见错误态，并在切换页面后恢复', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { rerender } = render(
      <PanelErrorBoundary resetKey="knowledge">
        <BrokenPanel />
      </PanelErrorBoundary>,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('这个页面暂时无法显示')
    expect(screen.getByRole('button', { name: '重试当前页面' })).toBeInTheDocument()

    rerender(
      <PanelErrorBoundary resetKey="rag">
        <div>咨询页面已恢复</div>
      </PanelErrorBoundary>,
    )

    expect(screen.getByText('咨询页面已恢复')).toBeInTheDocument()
  })
})
