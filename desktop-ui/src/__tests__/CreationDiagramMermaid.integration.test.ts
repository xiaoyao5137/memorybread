import { beforeAll, describe, expect, it } from 'vitest'
import mermaid from 'mermaid'
import { normalizeDiagramSvg, plantUmlToMermaid } from '../components/CreationDiagram'

describe('最新创作记录 Mermaid 兼容性', () => {
  beforeAll(() => {
    Object.defineProperty(SVGElement.prototype, 'getBBox', {
      configurable: true,
      value: () => ({ x: 0, y: 0, width: 640, height: 420 }),
    })
    Object.defineProperty(SVGElement.prototype, 'getComputedTextLength', {
      configurable: true,
      value: () => 96,
    })
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'neutral',
      suppressErrorRendering: true,
      flowchart: { htmlLabels: false },
    })
  })

  it('渲染带 HTML 换行和节点样式的真实记录图示', async () => {
    const source = `flowchart LR
    A[电商多业务] --> B[弹性调度]
    A --> C[潮汐调度]
    B --> D[GPU资源池]
    C --> D
    D --> E{收益统计模块}
    E -->|当前状态| F[收益不透明]
    E -->|优化目标| G[整体GPU节省<br/>约10%]

    style F fill:#ffcccc
    style G fill:#ccffcc`

    const result = await mermaid.render('latest-creation-record-diagram', source)
    const normalized = normalizeDiagramSvg(result.svg)
    const wrapper = document.createElement('div')
    wrapper.innerHTML = normalized
    const svg = wrapper.querySelector('svg')

    expect(svg).not.toBeNull()
    expect(svg!.getAttribute('width')).not.toBe('100%')
    expect(svg!.getAttribute('height')).toBeTruthy()
    expect(svg!.textContent).toContain('GPU资源池')
    expect(svg!.querySelectorAll('g').length).toBeGreaterThan(0)
  })

  it('渲染最新记录中带嵌套分层、stereotype 和注释的 PlantUML 架构图', async () => {
    const source = `@startuml
skinparam componentStyle rectangle
package "推理加速系统架构" {
  [业务请求层] as Request
  package "流量调度层" {
    [智能路由] <<core>> as Router
    [负载均衡] <<core>> as LB
    [潮汐调度器] <<core>> as Tidal
  }
  package "推理引擎层" {
    [SGLang 引擎] <<core>> as SGLang
    [量化模块 (FP8)] <<core>> as Quant
  }
  Request --> Router
  Router --> LB
  LB --> SGLang
  SGLang --> Quant
  Tidal --> SGLang
}
note right of Router
  根据模型类型选择最优推理实例
end note
@enduml`

    const converted = plantUmlToMermaid(source)
    expect(converted).toContain('业务请求层')
    expect(converted).toContain('智能路由')
    expect(converted).toContain('SGLang 引擎')
    const result = await mermaid.render('latest-plantuml-creation-record-diagram', converted)
    const normalized = normalizeDiagramSvg(result.svg)
    const wrapper = document.createElement('div')
    wrapper.innerHTML = normalized
    const svg = wrapper.querySelector('svg')

    expect(svg).not.toBeNull()
    expect(svg!.getAttribute('width')).not.toBe('100%')
    expect(svg!.textContent).toContain('智能路由')
    expect(svg!.textContent).toContain('量化模块 (FP8)')
  })
})
