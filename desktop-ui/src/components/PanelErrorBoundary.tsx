import React from 'react'

interface PanelErrorBoundaryProps {
  children: React.ReactNode
  resetKey: string
}

interface PanelErrorBoundaryState {
  hasError: boolean
}

class PanelErrorBoundary extends React.Component<PanelErrorBoundaryProps, PanelErrorBoundaryState> {
  state: PanelErrorBoundaryState = { hasError: false }

  static getDerivedStateFromError(): PanelErrorBoundaryState {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Panel render failed', error, info.componentStack)
  }

  componentDidUpdate(previousProps: PanelErrorBoundaryProps) {
    if (this.state.hasError && previousProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false })
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <section className="panel-error-state" role="alert">
          <div className="panel-error-state__card">
            <strong>这个页面暂时无法显示</strong>
            <span>你可以切换到左侧其他页面，或重试当前页面。</span>
            <button type="button" onClick={() => this.setState({ hasError: false })}>
              重试当前页面
            </button>
          </div>
        </section>
      )
    }

    return this.props.children
  }
}

export default PanelErrorBoundary
