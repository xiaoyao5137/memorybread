import { Children, isValidElement, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, Copy, Pencil, X } from 'lucide-react'

type DiagramLanguage = 'mermaid' | 'plantuml'

interface CreationDiagramProps {
  language: DiagramLanguage
  code: string
}

let mermaidInitialized = false
let mermaidRenderQueue: Promise<void> = Promise.resolve()
let diagramRenderSequence = 0

const DIAGRAM_MIN_ZOOM = 0.25
const DIAGRAM_MAX_ZOOM = 4

const clampDiagramZoom = (zoom: number) => (
  Math.min(DIAGRAM_MAX_ZOOM, Math.max(DIAGRAM_MIN_ZOOM, zoom))
)

export const getDiagramWheelZoom = (currentZoom: number, deltaY: number) => (
  clampDiagramZoom(currentZoom * Math.exp(-deltaY * 0.003))
)

const enqueueMermaidRender = <T,>(render: () => Promise<T>) => {
  const result = mermaidRenderQueue.then(render)
  mermaidRenderQueue = result.then(() => undefined, () => undefined)
  return result
}

const cleanLabel = (value: string) => value
  .trim()
  .replace(/^[["']|[\]"']$/g, '')
  .replace(/\s+/g, ' ')

const mermaidLabel = (value: string) => cleanLabel(value)
  .replace(/&/g, '&amp;')
  .replace(/"/g, '&quot;')
  .replace(/[{}]/g, '')

const plantUmlLines = (code: string) => code
  .replace(/^\s*@startuml[^\n]*$/gim, '')
  .replace(/^\s*@enduml\s*$/gim, '')
  .split('\n')
  .map(line => line.trim())
  .filter(line => line && !line.startsWith("'") && !/^skinparam\b/i.test(line))

const declarationPattern = /^(actor|participant|boundary|control|entity|database|collections|queue|component|node|cloud|rectangle|folder|package|artifact|interface|storage)\s+(?:"([^"]+)"|\[([^\]]+)\]|([^\s{]+))(?:\s+<<[^>]+>>)*(?:\s+as\s+(?:"([^"]+)"|([^\s{]+)))?(?:\s+<<[^>]+>>)*/i
const implicitComponentPattern = /^\[([^\]]+)\](?:\s+<<[^>]+>>)*(?:\s+as\s+(?:"([^"]+)"|([^\s{]+)))?(?:\s+<<[^>]+>>)*/i

const parseDeclaration = (line: string) => {
  const match = line.match(declarationPattern)
  if (match) {
    const label = match[2] || match[3] || match[4]
    const alias = match[5] || match[6] || match[4] || label
    return { kind: match[1].toLowerCase(), label: cleanLabel(label), alias: cleanLabel(alias) }
  }
  const implicit = line.match(implicitComponentPattern)
  if (!implicit) return null
  const label = implicit[1]
  const alias = implicit[2] || implicit[3] || label
  return { kind: 'component', label: cleanLabel(label), alias: cleanLabel(alias) }
}

const toSequenceDiagram = (lines: string[]) => {
  const declarations = lines.map(parseDeclaration).filter(Boolean) as Array<NonNullable<ReturnType<typeof parseDeclaration>>>
  const aliases = new Map<string, string>()
  const output = ['sequenceDiagram']

  declarations.forEach((item, index) => {
    const id = `participant${index + 1}`
    aliases.set(item.alias, id)
    aliases.set(item.label, id)
    output.push(`    ${item.kind === 'actor' ? 'actor' : 'participant'} ${id} as ${mermaidLabel(item.label)}`)
  })

  const resolve = (raw: string) => {
    const name = cleanLabel(raw)
    const existing = aliases.get(name)
    if (existing) return existing
    const id = `participant${aliases.size + 1}`
    aliases.set(name, id)
    output.push(`    participant ${id} as ${mermaidLabel(name)}`)
    return id
  }

  lines.forEach((line) => {
    if (parseDeclaration(line)) return
    const message = line.match(/^("[^"]+"|[^\s:]+)\s*(-->>|-->|->>|->|<--|<-)\s*("[^"]+"|[^\s:]+)\s*(?::\s*(.*))?$/)
    if (message) {
      const reversed = message[2].startsWith('<')
      const from = resolve(reversed ? message[3] : message[1])
      const to = resolve(reversed ? message[1] : message[3])
      const dotted = message[2].includes('--')
      output.push(`    ${from}${dotted ? '-->>' : '->>'}${to}: ${mermaidLabel(message[4] || '调用')}`)
      return
    }
    const control = line.match(/^(alt|else|opt|loop|par|and|break|critical|rect)\b\s*(.*)$/i)
    if (control) output.push(`    ${control[1].toLowerCase()} ${mermaidLabel(control[2])}`.trimEnd())
    else if (/^end$/i.test(line)) output.push('    end')
    else {
      const note = line.match(/^note\s+(?:left|right|over)(?:\s+of)?\s+([^:]+):\s*(.*)$/i)
      if (note) output.push(`    Note right of ${resolve(note[1])}: ${mermaidLabel(note[2])}`)
    }
  })

  return output.join('\n')
}

const toActivityDiagram = (lines: string[]) => {
  const output = ['flowchart TD']
  let previous = 'startNode'
  let index = 0
  output.push('    startNode([开始])')

  lines.forEach((line) => {
    if (/^(start|@startuml)$/i.test(line)) return
    if (/^(stop|end)$/i.test(line)) {
      output.push('    endNode([结束])')
      output.push(`    ${previous} --> endNode`)
      previous = 'endNode'
      return
    }
    const action = line.match(/^:(.+);$/)
    const decision = line.match(/^if\s*\((.+)\)\s*then(?:\s*\((.+)\))?/i)
    const text = action?.[1] || decision?.[1]
    if (!text) return
    index += 1
    const id = `step${index}`
    output.push(`    ${id}${decision ? `{${mermaidLabel(text)}}` : `["${mermaidLabel(text)}"]`}`)
    output.push(`    ${previous} -->${decision?.[2] ? `|${mermaidLabel(decision[2])}|` : ''} ${id}`)
    previous = id
  })

  if (previous === 'startNode') throw new Error('这段 PlantUML 活动图没有可渲染的步骤')
  if (previous !== 'endNode') {
    output.push('    endNode([结束])')
    output.push(`    ${previous} --> endNode`)
  }
  return output.join('\n')
}

const toRelationshipDiagram = (lines: string[]) => {
  const output = ['flowchart LR']
  const aliases = new Map<string, string>()
  let nodeIndex = 0
  const ensureNode = (raw: string, preferredLabel?: string) => {
    const name = cleanLabel(raw)
    const existing = aliases.get(name)
    if (existing) return existing
    nodeIndex += 1
    const id = `node${nodeIndex}`
    aliases.set(name, id)
    if (preferredLabel) aliases.set(cleanLabel(preferredLabel), id)
    output.push(`    ${id}["${mermaidLabel(preferredLabel || name)}"]`)
    return id
  }

  lines.forEach((line) => {
    const declaration = parseDeclaration(line)
    if (declaration && declaration.kind !== 'package') {
      ensureNode(declaration.alias, declaration.label)
      return
    }
    const relation = line.match(/^("[^"]+"|\[[^\]]+\]|[^\s:]+)\s*(?:-->|->|\.\.>|==>|<--|<-)\s*("[^"]+"|\[[^\]]+\]|[^\s:]+)\s*(?::\s*(.*))?$/)
    if (!relation) return
    const reversed = /<--|<-/.test(line)
    const from = ensureNode(reversed ? relation[2] : relation[1])
    const to = ensureNode(reversed ? relation[1] : relation[2])
    output.push(`    ${from} -->${relation[3] ? `|${mermaidLabel(relation[3])}|` : ''} ${to}`)
  })

  if (!nodeIndex) throw new Error('这段 PlantUML 暂时无法在本机转换为图片')
  return output.join('\n')
}

export const plantUmlToMermaid = (code: string) => {
  const lines = plantUmlLines(code)
  const hasStructuralNodes = lines.some(line => /^(component|node|cloud|rectangle|folder|package|artifact|interface|storage)\b/i.test(line))
  const hasSequenceParticipants = lines.some(line => /^(participant|boundary|control|entity|collections|queue)\b/i.test(line))
    || (!hasStructuralNodes && lines.some(line => /^actor\b/i.test(line)))
  const hasSequenceMessages = lines.some(line => /(?:-->>|-->|->>|->|<--|<-)/.test(line))
  if (hasSequenceParticipants && hasSequenceMessages) return toSequenceDiagram(lines)
  if (lines.some(line => /^(start|stop|if\s*\(|:[^;]+;)/i.test(line))) return toActivityDiagram(lines)
  return toRelationshipDiagram(lines)
}

const parseDiagramSvg = (svg: string) => {
  const template = document.createElement('template')
  template.innerHTML = svg.trim()
  const root = template.content.querySelector('svg')
  if (!root) throw new Error('图示返回了无效的 SVG')
  return root
}

export const normalizeDiagramSvg = (svg: string) => {
  const root = parseDiagramSvg(svg)
  const viewBox = (root.getAttribute('viewBox') || '').trim().split(/\s+/).map(Number)
  const width = viewBox.length === 4 && Number.isFinite(viewBox[2]) && viewBox[2] > 0
    ? viewBox[2]
    : Number.parseFloat(root.getAttribute('width') || '')
  const height = viewBox.length === 4 && Number.isFinite(viewBox[3]) && viewBox[3] > 0
    ? viewBox[3]
    : Number.parseFloat(root.getAttribute('height') || '')
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0) {
    throw new Error('图示没有有效的可视尺寸')
  }
  root.setAttribute('width', String(width))
  root.setAttribute('height', String(height))
  root.setAttribute('preserveAspectRatio', 'xMidYMid meet')
  const existingStyle = root.getAttribute('style')?.trim().replace(/;?$/, ';') || ''
  root.setAttribute('style', `${existingStyle}width:100%;max-width:${width}px;height:auto;`)
  return root.outerHTML
}

const svgToPngBlob = async (svg: string) => {
  const parsed = parseDiagramSvg(svg)
  const viewBox = (parsed.getAttribute('viewBox') || '').split(/\s+/).map(Number)
  const width = Math.max(320, viewBox[2] || Number.parseFloat(parsed.getAttribute('width') || '') || 960)
  const height = Math.max(180, viewBox[3] || Number.parseFloat(parsed.getAttribute('height') || '') || 540)
  parsed.setAttribute('width', String(width))
  parsed.setAttribute('height', String(height))
  const serialized = new XMLSerializer().serializeToString(parsed)
  const source = URL.createObjectURL(new Blob([serialized], { type: 'image/svg+xml;charset=utf-8' }))

  try {
    const image = new Image()
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error('图片加载失败'))
      image.src = source
    })
    const scale = 2
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(width * scale)
    canvas.height = Math.round(height * scale)
    const context = canvas.getContext('2d')
    if (!context) throw new Error('当前环境不支持复制图片')
    context.scale(scale, scale)
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, width, height)
    context.drawImage(image, 0, 0, width, height)
    const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'))
    if (!blob) throw new Error('图片生成失败')
    return blob
  } finally {
    URL.revokeObjectURL(source)
  }
}

const renderDiagramSvg = async (language: DiagramLanguage, code: string, renderId: string) => {
  const { default: mermaid } = await import('mermaid')
  if (!mermaidInitialized) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'neutral',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      suppressErrorRendering: true,
      flowchart: { htmlLabels: false },
    })
    mermaidInitialized = true
  }
  const source = language === 'plantuml' ? plantUmlToMermaid(code) : code
  const result = await enqueueMermaidRender(() => mermaid.render(renderId, source))
  return normalizeDiagramSvg(result.svg)
}

export const diagramSvgDataUrl = (svg: string) => (
  `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
)

export const CreationDiagram = ({ language, code }: CreationDiagramProps) => {
  const reactId = useId()
  const renderId = useMemo(() => `creation-diagram-${reactId.replace(/[^a-z0-9]/gi, '')}`, [reactId])
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [zoom, setZoom] = useState(1)
  const zoomRef = useRef(1)
  const modalCanvasRef = useRef<HTMLDivElement>(null)
  const [renderCode, setRenderCode] = useState(code)
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copying' | 'copied' | 'error'>('idle')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorDraft, setEditorDraft] = useState(code)
  const [editorError, setEditorError] = useState('')
  const [applyingEdit, setApplyingEdit] = useState(false)

  useEffect(() => {
    let active = true
    setSvg('')
    setError('')
    const render = async () => {
      try {
        const nextSvg = await renderDiagramSvg(language, code, renderId)
        if (active) {
          setRenderCode(code)
          setEditorDraft(code)
          setSvg(nextSvg)
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : '图示渲染失败')
      }
    }
    void render()
    return () => { active = false }
  }, [code, language, renderId])

  useEffect(() => {
    if (!expanded && !editorOpen) return undefined
    const close = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (editorOpen) setEditorOpen(false)
      else setExpanded(false)
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [editorOpen, expanded])

  useEffect(() => {
    if (!expanded) return undefined
    const canvas = modalCanvasRef.current
    if (!canvas) return undefined

    const handleWheel = (event: WheelEvent) => {
      if (!event.deltaY) return
      event.preventDefault()
      event.stopPropagation()

      const currentZoom = zoomRef.current
      const nextZoom = getDiagramWheelZoom(currentZoom, event.deltaY)
      if (nextZoom === currentZoom) return

      const rect = canvas.getBoundingClientRect()
      const pointerX = event.clientX - rect.left
      const pointerY = event.clientY - rect.top
      const contentX = canvas.scrollLeft + pointerX
      const contentY = canvas.scrollTop + pointerY
      const zoomRatio = nextZoom / currentZoom

      zoomRef.current = nextZoom
      setZoom(nextZoom)

      const keepPointerAnchored = () => {
        if (!canvas.isConnected) return
        canvas.scrollLeft = Math.max(0, contentX * zoomRatio - pointerX)
        canvas.scrollTop = Math.max(0, contentY * zoomRatio - pointerY)
      }
      if (typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(keepPointerAnchored)
      } else {
        window.setTimeout(keepPointerAnchored, 0)
      }
    }

    canvas.addEventListener('wheel', handleWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', handleWheel)
  }, [expanded])

  const openPreview = () => {
    zoomRef.current = 1
    setZoom(1)
    setExpanded(true)
  }

  const copyImage = async () => {
    if (!svg || copyStatus === 'copying') return
    setCopyStatus('copying')
    try {
      if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
        throw new Error('当前环境不支持复制图片')
      }
      // Start the clipboard write in the click gesture. WebKit may revoke
      // clipboard permission if PNG conversion is awaited first.
      const blob = svgToPngBlob(svg)
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
      setCopyStatus('copied')
    } catch {
      setCopyStatus('error')
    }
    window.setTimeout(() => setCopyStatus('idle'), 1800)
  }

  const openEditor = () => {
    setEditorDraft(renderCode)
    setEditorError('')
    setEditorOpen(true)
  }

  const applyEdit = async () => {
    if (!editorDraft.trim() || applyingEdit) {
      if (!editorDraft.trim()) setEditorError('图片代码不能为空')
      return
    }
    setApplyingEdit(true)
    setEditorError('')
    try {
      diagramRenderSequence += 1
      const nextSvg = await renderDiagramSvg(language, editorDraft, `${renderId}-edit-${diagramRenderSequence}`)
      setRenderCode(editorDraft)
      setSvg(nextSvg)
      setError('')
      setEditorOpen(false)
    } catch (reason) {
      setEditorError(reason instanceof Error ? reason.message : '代码无法渲染，请检查后重试')
    } finally {
      setApplyingEdit(false)
    }
  }

  const label = language === 'plantuml' ? 'PlantUML' : 'Mermaid'
  const diagram = svg ? (
    <div className="creation-diagram__svg">
      <img
        className="creation-diagram__image"
        src={diagramSvgDataUrl(svg)}
        alt={`${label} 图示`}
      />
    </div>
  ) : null

  return (
    <figure className="creation-diagram" aria-label={`${label} 图示`}>
      {svg ? (
        <div className="creation-diagram__frame">
          <div className="creation-diagram__actions">
            <button type="button" onClick={() => void copyImage()} disabled={copyStatus === 'copying'} aria-label="复制图片">
              {copyStatus === 'copied' ? <Check size={14} /> : <Copy size={14} />}
              {copyStatus === 'copying' ? '处理中' : copyStatus === 'copied' ? '已复制' : copyStatus === 'error' ? '复制失败' : '复制'}
            </button>
            <button type="button" onClick={openEditor} aria-label={`编辑 ${label} 图片代码`}><Pencil size={14} />编辑</button>
          </div>
          <button type="button" className="creation-diagram__preview" onClick={openPreview} aria-label="查看图片">
            {!expanded && diagram}
          </button>
        </div>
      ) : error ? (
        <div className="creation-diagram__error" role="alert">
          <strong>图示暂时无法渲染</strong>
          <span>{error}</span>
          <pre><code>{code}</code></pre>
        </div>
      ) : (
        <div className="creation-diagram__loading" aria-live="polite">正在渲染图示…</div>
      )}
      {expanded && svg && createPortal(
        <div className="creation-diagram-modal" role="dialog" aria-modal="true" aria-label={`${label} 图示大图`} onMouseDown={event => {
          if (event.target === event.currentTarget) setExpanded(false)
        }}>
          <div className="creation-diagram-modal__card">
            <div className="creation-diagram-modal__zoom-status">滚轮缩放 · {Math.round(zoom * 100)}%</div>
            <button type="button" className="creation-diagram-modal__close" onClick={() => setExpanded(false)} aria-label="关闭大图"><X size={18} /></button>
            <div
              ref={modalCanvasRef}
              className="creation-diagram-modal__canvas"
              aria-label="图片查看区域，使用鼠标滚轮缩放"
            >
              <div className="creation-diagram-modal__zoom-layer" style={{ width: `${zoom * 100}%` }}>
                {diagram}
              </div>
            </div>
          </div>
        </div>,
        document.body,
      )}
      {editorOpen && createPortal(
        <div className="creation-diagram-editor" role="dialog" aria-modal="true" aria-label={`编辑 ${label} 图示`} onMouseDown={event => {
          if (event.target === event.currentTarget && !applyingEdit) setEditorOpen(false)
        }}>
          <div className="creation-diagram-editor__card">
            <div className="creation-diagram-editor__header">
              <div>
                <strong>编辑 {label} 图示</strong>
                <span>修改代码后完成，图片会立即重新渲染</span>
              </div>
              <button type="button" onClick={() => setEditorOpen(false)} disabled={applyingEdit} aria-label="关闭编辑器"><X size={18} /></button>
            </div>
            <textarea
              className="creation-diagram-editor__textarea"
              value={editorDraft}
              onChange={event => {
                setEditorDraft(event.target.value)
                if (editorError) setEditorError('')
              }}
              aria-label="图片代码"
              spellCheck={false}
              autoFocus
            />
            {editorError && <div className="creation-diagram-editor__error" role="alert">{editorError}</div>}
            <div className="creation-diagram-editor__footer">
              <button type="button" className="is-secondary" onClick={() => setEditorOpen(false)} disabled={applyingEdit}>取消</button>
              <button type="button" className="is-primary" onClick={() => void applyEdit()} disabled={applyingEdit || !editorDraft.trim()}>
                {applyingEdit ? '正在渲染…' : '完成'}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </figure>
  )
}

export const CreationDiagramPre = ({ node, children, ...props }: any) => {
  const child = Children.toArray(children)[0]
  if (isValidElement(child) && /language-(?:mermaid|plantuml)/i.test(String((child.props as any).className || ''))) {
    return <>{child}</>
  }
  return <pre {...props}>{children}</pre>
}

export const CreationDiagramCode = ({ node, className, children, ...props }: any) => {
  const language = String(className || '').match(/language-(mermaid|plantuml)/i)?.[1]?.toLowerCase()
  if (language === 'mermaid' || language === 'plantuml') {
    return <CreationDiagram language={language} code={String(children).replace(/\n$/, '')} />
  }
  return (
    <code
      className={className}
      style={{ background: 'var(--mb-bg-inset)', padding: '2px 5px', borderRadius: 4 }}
      {...props}
    >
      {children}
    </code>
  )
}
