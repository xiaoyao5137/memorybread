import { createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ReactMarkdown from 'react-markdown'
import {
  CreationDiagram,
  CreationDiagramCode,
  CreationDiagramPre,
  diagramSvgDataUrl,
  getDiagramWheelZoom,
  normalizeDiagramSvg,
  plantUmlToMermaid,
} from '../components/CreationDiagram'

const renderDiagram = vi.fn(async () => ({
  svg: '<svg width="100%" viewBox="0 0 640 360" role="img"><text>本地图示</text></svg>',
}))

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: renderDiagram,
  },
}))

describe('创作文档代码图示', () => {
  beforeEach(() => {
    renderDiagram.mockClear()
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { write },
    })
    Object.defineProperty(globalThis, 'ClipboardItem', {
      configurable: true,
      value: class MockClipboardItem {
        data: Record<string, Blob | Promise<Blob>>

        constructor(data: Record<string, Blob | Promise<Blob>>) {
          this.data = data
        }
      },
    })
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:diagram') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    Object.defineProperty(globalThis, 'Image', {
      configurable: true,
      value: class MockImage {
        onload: null | (() => void) = null
        onerror: null | (() => void) = null

        set src(_value: string) {
          queueMicrotask(() => this.onload?.())
        }
      },
    })
    Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
      configurable: true,
      value: vi.fn(() => ({
        scale: vi.fn(),
        fillRect: vi.fn(),
        drawImage: vi.fn(),
        fillStyle: '',
      })),
    })
    Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', {
      configurable: true,
      value: vi.fn((callback: BlobCallback) => callback(new Blob(['png'], { type: 'image/png' }))),
    })
  })

  it('把常见 PlantUML 组件图转换为可本地渲染的 Mermaid', () => {
    expect(plantUmlToMermaid(`
@startuml
component 客户端
component 核心服务
客户端 --> 核心服务: 调用
@enduml
    `)).toContain('node1 -->|调用| node2')
  })

  it('保留最新记录中 stereotype 声明的中文节点和别名关系', () => {
    const converted = plantUmlToMermaid(`
@startuml
package "推理加速系统架构" {
  [业务请求层] as Request
  [智能路由] <<core>> as Router
  [SGLang 引擎] <<core>> as SGLang
  Request --> Router
  Router --> SGLang
}
@enduml
    `)
    expect(converted).toContain('node1["业务请求层"]')
    expect(converted).toContain('node2["智能路由"]')
    expect(converted).toContain('node3["SGLang 引擎"]')
    expect(converted).toContain('node1 --> node2')
    expect(converted).toContain('node2 --> node3')
    expect(converted).not.toContain('["Router"]')
  })

  it('保留 PlantUML 时序图的参与者和消息顺序', () => {
    const converted = plantUmlToMermaid(`
@startuml
actor 用户
participant 系统
用户 -> 系统: 发起请求
系统 --> 用户: 返回结果
@enduml
`)
    expect(converted).toContain('sequenceDiagram')
    expect(converted).toContain('participant1->>participant2: 发起请求')
    expect(converted).toContain('participant2-->>participant1: 返回结果')
  })

  it('把 Mermaid 的百分比宽度规范成 WebView 可见尺寸', () => {
    const normalized = normalizeDiagramSvg('<svg width="100%" viewBox="0 0 496 336"><g><text>GPU资源池</text></g></svg>')
    const wrapper = document.createElement('div')
    wrapper.innerHTML = normalized
    const root = wrapper.querySelector('svg')
    expect(root).not.toBeNull()
    expect(root!.getAttribute('width')).toBe('496')
    expect(root!.getAttribute('height')).toBe('336')
    expect(root!.getAttribute('preserveAspectRatio')).toBe('xMidYMid meet')
  })

  it('兼容最新记录里带 HTML 换行标签的 Mermaid SVG', () => {
    const mermaidSvg = '<svg width="100%" viewBox="-8 -8 496 336"><foreignObject><div xmlns="http://www.w3.org/1999/xhtml"><span><p>GPU资源池<br>720张卡</p></span></div></foreignObject></svg>'
    expect(() => normalizeDiagramSvg(mermaidSvg)).not.toThrow()
  })

  it('按滚轮方向等比缩放图片并限制缩放边界', () => {
    expect(getDiagramWheelZoom(1, -100)).toBeGreaterThan(1)
    expect(getDiagramWheelZoom(1, 100)).toBeLessThan(1)
    expect(getDiagramWheelZoom(4, -100)).toBe(4)
    expect(getDiagramWheelZoom(0.25, 100)).toBe(0.25)
  })

  it('大图弹窗用鼠标滚轮缩放，并在重新打开时恢复 100%', async () => {
    render(<CreationDiagram language="mermaid" code={'flowchart LR\nA --> B'} />)

    await screen.findByLabelText('查看图片')
    fireEvent.click(screen.getByLabelText('查看图片'))
    const dialog = screen.getByRole('dialog', { name: 'Mermaid 图示大图' })
    const scrollContent = dialog.querySelector<HTMLDivElement>('.creation-diagram__svg')
    const zoomLayer = dialog.querySelector<HTMLDivElement>('.creation-diagram-modal__zoom-layer')
    expect(zoomLayer).toHaveStyle({ width: '100%' })
    expect(screen.getByText('滚轮缩放 · 100%')).toBeInTheDocument()

    const zoomInEvent = createEvent.wheel(scrollContent!, { deltaY: -100 })
    fireEvent(scrollContent!, zoomInEvent)
    expect(zoomInEvent.defaultPrevented).toBe(true)
    await waitFor(() => expect(Number.parseFloat(zoomLayer!.style.width)).toBeGreaterThan(100))
    const zoomedInWidth = Number.parseFloat(zoomLayer!.style.width)
    expect(dialog).toHaveTextContent(`${Math.round(zoomedInWidth)}%`)

    const zoomOutEvent = createEvent.wheel(scrollContent!, { deltaY: 50 })
    fireEvent(scrollContent!, zoomOutEvent)
    expect(zoomOutEvent.defaultPrevented).toBe(true)
    await waitFor(() => expect(Number.parseFloat(zoomLayer!.style.width)).toBeLessThan(zoomedInWidth))

    fireEvent.click(screen.getByLabelText('关闭大图'))
    fireEvent.click(screen.getByLabelText('查看图片'))
    expect(screen.getByText('滚轮缩放 · 100%')).toBeInTheDocument()
  })

  it('默认只显示实际图片，悬浮操作只提供复制图片和编辑代码', async () => {
    const { container } = render(<CreationDiagram language="mermaid" code={'flowchart LR\nA --> B'} />)

    expect(screen.getByText('正在渲染图示…')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('查看图片')).toBeEnabled())
    expect(screen.queryByText('flowchart LR')).not.toBeInTheDocument()
    expect(screen.queryByText('Mermaid 图示')).not.toBeInTheDocument()
    expect(screen.queryByText('放大')).not.toBeInTheDocument()
    expect(screen.queryByText('下载')).not.toBeInTheDocument()
    expect(screen.queryByText('复制代码')).not.toBeInTheDocument()
    expect(container.querySelector('.creation-diagram__actions')).toBeInTheDocument()
    const image = screen.getByRole('img', { name: 'Mermaid 图示' })
    expect(image).toHaveAttribute('src', diagramSvgDataUrl(
      '<svg width="640" viewBox="0 0 640 360" role="img" height="360" preserveAspectRatio="xMidYMid meet" style="width:100%;max-width:640px;height:auto;"><text>本地图示</text></svg>',
    ))

    fireEvent.click(screen.getByLabelText('查看图片'))
    const dialog = screen.getByRole('dialog', { name: 'Mermaid 图示大图' })
    const scrollCanvas = dialog.querySelector<HTMLDivElement>('.creation-diagram-modal__canvas')
    const scrollContent = dialog.querySelector<HTMLDivElement>('.creation-diagram__svg')
    expect(scrollCanvas).toContainElement(scrollContent)
    expect(scrollCanvas).not.toBe(scrollContent)
    fireEvent.click(screen.getByLabelText('关闭大图'))

    fireEvent.click(screen.getByRole('button', { name: '复制图片' }))
    await waitFor(() => expect(navigator.clipboard.write).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('已复制')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '编辑 Mermaid 图片代码' }))
    const editor = screen.getByRole('dialog', { name: '编辑 Mermaid 图示' })
    expect(editor).toBeInTheDocument()
    const textarea = screen.getByLabelText('图片代码')
    expect(textarea).toHaveValue('flowchart LR\nA --> B')
    fireEvent.change(textarea, { target: { value: 'flowchart TD\nA --> C' } })
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    await waitFor(() => expect(renderDiagram).toHaveBeenLastCalledWith(
      expect.stringContaining('-edit-'),
      'flowchart TD\nA --> C',
    ))
    await waitFor(() => expect(editor).not.toBeInTheDocument())
  })

  it('父级用相同内容重复渲染时不会重新绘制 Mermaid', async () => {
    const props = { language: 'mermaid' as const, code: 'flowchart LR\nA --> B' }
    const { rerender } = render(<CreationDiagram {...props} />)
    await screen.findByLabelText('查看图片')
    expect(renderDiagram).toHaveBeenCalledTimes(1)

    rerender(<CreationDiagram {...props} />)
    await Promise.resolve()
    expect(renderDiagram).toHaveBeenCalledTimes(1)
  })

  it('在 Markdown 文档中用图片替换 Mermaid 代码块', async () => {
    render(
      <ReactMarkdown components={{ pre: CreationDiagramPre, code: CreationDiagramCode }}>
        {'```mermaid\nflowchart LR\nA --> B\n```'}
      </ReactMarkdown>,
    )

    expect(await screen.findByRole('figure', { name: 'Mermaid 图示' })).toBeInTheDocument()
    expect(screen.queryByText('flowchart LR')).not.toBeInTheDocument()
  })
})
