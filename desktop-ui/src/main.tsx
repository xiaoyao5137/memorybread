import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// 全局错误捕获
window.addEventListener('error', (event) => {
  console.error('全局错误:', event.error)
  console.error('错误信息:', event.message)
  console.error('错误栈:', event.error?.stack)
  document.body.innerHTML = `
    <div style="padding: 20px; font-family: monospace; background: #fee; color: #c00;">
      <h1>前端加载错误</h1>
      <pre>${event.message}\n\n${event.error?.stack || ''}</pre>
    </div>
  `
})

window.addEventListener('unhandledrejection', (event) => {
  console.error('未处理的 Promise 拒绝:', event.reason)
  document.body.innerHTML = `
    <div style="padding: 20px; font-family: monospace; background: #fee; color: #c00;">
      <h1>Promise 错误</h1>
      <pre>${event.reason}</pre>
    </div>
  `
})

console.log('=== 记忆面包前端启动 ===')
console.log('React 版本:', React.version)
console.log('根元素:', document.getElementById('root'))

try {
  const rootElement = document.getElementById('root')
  if (!rootElement) {
    throw new Error('找不到 root 元素')
  }

  console.log('开始渲染 React 应用...')
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
  console.log('React 应用渲染成功')
} catch (error) {
  console.error('渲染失败:', error)
  document.body.innerHTML = `
    <div style="padding: 20px; font-family: monospace; background: #fee; color: #c00;">
      <h1>React 渲染失败</h1>
      <pre>${error}</pre>
    </div>
  `
}
