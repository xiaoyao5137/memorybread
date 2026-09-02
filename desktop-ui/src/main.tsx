let appMounted = false

const readableError = (error: unknown) => {
  if (error instanceof Error) {
    return [error.message, error.stack].filter(Boolean).join('\n\n')
  }
  return String(error || '未知错误')
}

export const renderBootstrapFailure = (error: unknown) => {
  if (appMounted) return

  const rootElement = document.getElementById('root')
  if (!rootElement) return

  const page = document.createElement('main')
  page.className = 'memorybread-bootstrap memorybread-bootstrap--error'
  page.dataset.bootstrapState = 'failed'
  page.setAttribute('role', 'alert')

  const content = document.createElement('section')
  content.className = 'memorybread-bootstrap__content'

  const title = document.createElement('h1')
  title.textContent = '界面资源加载失败'

  const description = document.createElement('p')
  description.textContent = '页面资源没有完整加载。请先重新加载；若仍未恢复，请重启桌面界面。'

  const details = document.createElement('details')
  const summary = document.createElement('summary')
  summary.textContent = '查看错误信息'
  const technicalDetail = document.createElement('pre')
  technicalDetail.textContent = readableError(error)
  details.append(summary, technicalDetail)

  const reload = document.createElement('button')
  reload.type = 'button'
  reload.textContent = '重新加载'
  reload.addEventListener('click', () => window.location.reload())

  content.append(title, description, details, reload)
  page.append(content)
  rootElement.replaceChildren(page)
}

// 入口不再静态依赖 App。模块图解析失败时，这些监听器和错误页仍然可用。
window.addEventListener('error', (event) => {
  console.error('全局错误:', event.error || event.message)
  renderBootstrapFailure(event.error || event.message)
})

window.addEventListener('unhandledrejection', (event) => {
  console.error('未处理的 Promise 拒绝:', event.reason)
  renderBootstrapFailure(event.reason)
})

export const bootstrapApp = async () => {
  const rootElement = document.getElementById('root')
  if (!rootElement) {
    throw new Error('找不到 root 元素')
  }

  console.log('=== 记忆面包前端启动 ===')
  console.log('根元素:', rootElement)

  try {
    const [{ default: React }, ReactDOM, { default: App }] = await Promise.all([
      import('react'),
      import('react-dom/client'),
      import('./App'),
      import('./index.css'),
    ])

    console.log('React 版本:', React.version)
    console.log('开始渲染 React 应用...')
    rootElement.replaceChildren()
    ReactDOM.createRoot(rootElement).render(
      React.createElement(
        React.StrictMode,
        null,
        React.createElement(App),
      ),
    )
    appMounted = true
    console.log('React 应用渲染成功')
  } catch (error) {
    console.error('前端入口加载失败:', error)
    renderBootstrapFailure(error)
  }
}

export const bootstrapPromise = bootstrapApp()
