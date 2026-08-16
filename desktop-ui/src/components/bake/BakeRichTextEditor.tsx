import DOMPurify from 'dompurify'
import { marked } from 'marked'
import React, { useEffect, useRef } from 'react'

const renderMarkdown = (value: string) => {
  const rendered = marked.parse(value || '', { async: false })
  return DOMPurify.sanitize(typeof rendered === 'string' ? rendered : '')
}

const inlineMarkdown = (node: Node): string => {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent || ''
  if (!(node instanceof HTMLElement)) return ''

  const children = () => Array.from(node.childNodes).map(inlineMarkdown).join('')
  const tag = node.tagName.toLowerCase()
  if (tag === 'br') return '\n'
  if (tag === 'strong' || tag === 'b') return `**${children()}**`
  if (tag === 'em' || tag === 'i') return `*${children()}*`
  if (tag === 's' || tag === 'strike') return `~~${children()}~~`
  if (tag === 'a') {
    const label = children().trim()
    const href = node.getAttribute('href') || ''
    return href ? `[${label || href}](${href})` : label
  }
  if (tag === 'code' && node.parentElement?.tagName.toLowerCase() !== 'pre') {
    return `\`${children()}\``
  }
  return children()
}

const blockMarkdown = (node: Node, listIndex?: number): string => {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent || ''
  if (!(node instanceof HTMLElement)) return ''

  const tag = node.tagName.toLowerCase()
  const inline = () => Array.from(node.childNodes).map(inlineMarkdown).join('').trim()
  const blocks = () => Array.from(node.childNodes).map(child => blockMarkdown(child)).join('')

  if (/^h[1-6]$/.test(tag)) {
    return `${'#'.repeat(Number(tag.slice(1)))} ${inline()}\n\n`
  }
  if (tag === 'p' || tag === 'div') return `${inline()}\n\n`
  if (tag === 'blockquote') {
    return `${blocks().trim().split('\n').map(line => `> ${line}`).join('\n')}\n\n`
  }
  if (tag === 'pre') return `\`\`\`\n${node.textContent?.trim() || ''}\n\`\`\`\n\n`
  if (tag === 'ul' || tag === 'ol') {
    return Array.from(node.children)
      .filter(child => child.tagName.toLowerCase() === 'li')
      .map((child, index) => blockMarkdown(child, tag === 'ol' ? index + 1 : undefined))
      .join('') + '\n'
  }
  if (tag === 'li') return `${listIndex ? `${listIndex}.` : '-'} ${inline()}\n`
  if (tag === 'br') return '\n'
  return inlineMarkdown(node) || blocks()
}

const editorMarkdown = (editor: HTMLElement) => (
  Array.from(editor.childNodes)
    .map(node => blockMarkdown(node))
    .join('')
    .replace(/\u200B/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
)

const selectionRangeInside = (editor: HTMLElement) => {
  const selection = window.getSelection()
  if (!selection?.rangeCount) return null
  const range = selection.getRangeAt(0)
  const anchor = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentNode
  return anchor && editor.contains(anchor) ? { range, selection } : null
}

const topLevelEditorChild = (editor: HTMLElement, node: Node | null) => {
  let element = node instanceof HTMLElement ? node : node?.parentElement
  if (!element || element === editor || !editor.contains(element)) return null
  while (element.parentElement && element.parentElement !== editor) element = element.parentElement
  return element.parentElement === editor ? element : null
}

const blocksInRange = (editor: HTMLElement, range: Range) => {
  if (range.collapsed) {
    const block = topLevelEditorChild(editor, range.startContainer)
    return block ? [block] : []
  }
  return Array.from(editor.children).filter(child => {
    try {
      return range.intersectsNode(child)
    } catch {
      return false
    }
  }) as HTMLElement[]
}

const placeCaretAtEnd = (node: Node) => {
  const selection = window.getSelection()
  if (!selection) return
  const range = document.createRange()
  range.selectNodeContents(node)
  range.collapse(false)
  selection.removeAllRanges()
  selection.addRange(range)
}

const BakeRichTextEditor: React.FC<{
  value: string
  onChange: (value: string) => void
  ariaLabel?: string
  placeholder?: string
}> = ({ value, onChange, ariaLabel = '富文本编辑框', placeholder = '输入文档内容…' }) => {
  const editorRef = useRef<HTMLDivElement>(null)
  const lastEmittedValue = useRef<string | null>(null)
  const preservedRangeRef = useRef<Range | null>(null)
  const toolbarSelectionPendingRef = useRef(false)

  useEffect(() => {
    const editor = editorRef.current
    if (!editor || lastEmittedValue.current === value) return
    editor.innerHTML = renderMarkdown(value)
  }, [value])

  const emitChange = () => {
    const next = editorRef.current ? editorMarkdown(editorRef.current) : ''
    lastEmittedValue.current = next
    onChange(next)
  }

  const ensureSelection = () => {
    const editor = editorRef.current
    if (!editor) return null
    const preservedRange = preservedRangeRef.current
    const preservedAnchor = preservedRange?.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
      ? preservedRange.commonAncestorContainer
      : preservedRange?.commonAncestorContainer.parentNode
    if (toolbarSelectionPendingRef.current && preservedRange && preservedAnchor && editor.contains(preservedAnchor)) {
      const selection = window.getSelection()
      if (!selection) return null
      const range = preservedRange.cloneRange()
      selection.removeAllRanges()
      selection.addRange(range)
      toolbarSelectionPendingRef.current = false
      return { editor, range, selection }
    }
    toolbarSelectionPendingRef.current = false
    const current = selectionRangeInside(editor)
    if (current) return { editor, ...current }
    editor.focus()
    const selection = window.getSelection()
    if (!selection) return null
    const range = document.createRange()
    range.selectNodeContents(editor)
    range.collapse(false)
    selection.removeAllRanges()
    selection.addRange(range)
    return { editor, range, selection }
  }

  const formatBlock = (tagName: 'p' | 'h2') => {
    const context = ensureSelection()
    if (!context) return
    const { editor, range } = context
    const blocks = blocksInRange(editor, range)
    if (!blocks.length) {
      const block = document.createElement(tagName)
      block.appendChild(document.createElement('br'))
      editor.appendChild(block)
      placeCaretAtEnd(block)
      emitChange()
      return
    }
    let lastBlock: HTMLElement | null = null
    blocks.forEach(block => {
      if (block.tagName.toLowerCase() === 'ul' || block.tagName.toLowerCase() === 'ol') return
      if (block.tagName.toLowerCase() === tagName) {
        lastBlock = block
        return
      }
      const replacement = document.createElement(tagName)
      while (block.firstChild) replacement.appendChild(block.firstChild)
      block.replaceWith(replacement)
      lastBlock = replacement
    })
    if (lastBlock) placeCaretAtEnd(lastBlock)
    emitChange()
  }

  const formatInline = (tagName: 'strong' | 'em') => {
    const context = ensureSelection()
    if (!context) return
    const { range, selection } = context
    const startElement = range.startContainer instanceof HTMLElement
      ? range.startContainer
      : range.startContainer.parentElement
    const existing = startElement?.closest(tagName)
    if (existing && editorRef.current?.contains(existing)) {
      const parent = existing.parentNode
      if (!parent) return
      while (existing.firstChild) parent.insertBefore(existing.firstChild, existing)
      existing.remove()
      emitChange()
      return
    }
    const wrapper = document.createElement(tagName)
    if (range.collapsed) {
      const marker = document.createTextNode('\u200B')
      wrapper.appendChild(marker)
      range.insertNode(wrapper)
      range.setStart(marker, marker.length)
      range.collapse(true)
      selection.removeAllRanges()
      selection.addRange(range)
      return
    }
    try {
      range.surroundContents(wrapper)
    } catch {
      wrapper.appendChild(range.extractContents())
      range.insertNode(wrapper)
    }
    selection.removeAllRanges()
    const nextRange = document.createRange()
    nextRange.selectNodeContents(wrapper)
    selection.addRange(nextRange)
    emitChange()
  }

  const toggleList = (ordered: boolean) => {
    const context = ensureSelection()
    if (!context) return
    const { editor, range } = context
    const requestedTag = ordered ? 'ol' : 'ul'
    const startElement = range.startContainer instanceof HTMLElement
      ? range.startContainer
      : range.startContainer.parentElement
    const existingList = startElement?.closest('ul, ol') as HTMLElement | null

    if (existingList && editor.contains(existingList)) {
      if (existingList.tagName.toLowerCase() !== requestedTag) {
        const replacement = document.createElement(requestedTag)
        while (existingList.firstChild) replacement.appendChild(existingList.firstChild)
        existingList.replaceWith(replacement)
        placeCaretAtEnd(replacement.lastElementChild || replacement)
      } else {
        const fragment = document.createDocumentFragment()
        let lastParagraph: HTMLParagraphElement | null = null
        Array.from(existingList.children).forEach(item => {
          const paragraph = document.createElement('p')
          while (item.firstChild) paragraph.appendChild(item.firstChild)
          fragment.appendChild(paragraph)
          lastParagraph = paragraph
        })
        existingList.replaceWith(fragment)
        if (lastParagraph) placeCaretAtEnd(lastParagraph)
      }
      emitChange()
      return
    }

    const list = document.createElement(requestedTag)
    const blocks = blocksInRange(editor, range)
    if (blocks.length) {
      blocks.forEach(block => {
        const item = document.createElement('li')
        while (block.firstChild) item.appendChild(block.firstChild)
        list.appendChild(item)
      })
      blocks[0].replaceWith(list)
      blocks.slice(1).forEach(block => block.remove())
    } else if (!range.collapsed) {
      const item = document.createElement('li')
      item.appendChild(range.extractContents())
      list.appendChild(item)
      range.insertNode(list)
    } else {
      const item = document.createElement('li')
      item.appendChild(document.createElement('br'))
      list.appendChild(item)
      range.insertNode(list)
    }
    placeCaretAtEnd(list.lastElementChild || list)
    emitChange()
  }

  const preserveToolbarSelection = (event: React.SyntheticEvent<HTMLElement>) => {
    if (!(event.target as HTMLElement).closest('button')) return
    const editor = editorRef.current
    const current = editor ? selectionRangeInside(editor) : null
    if (current) {
      preservedRangeRef.current = current.range.cloneRange()
      toolbarSelectionPendingRef.current = true
    }
    event.preventDefault()
  }

  return (
    <div className="bake-rich-editor">
      <div
        className="bake-rich-editor__toolbar"
        role="toolbar"
        aria-label="文本格式"
        onPointerDown={preserveToolbarSelection}
        onMouseDown={preserveToolbarSelection}
      >
        <button type="button" onClick={() => formatBlock('p')}>正文</button>
        <button type="button" onClick={() => formatBlock('h2')}>标题</button>
        <button type="button" aria-label="加粗" onClick={() => formatInline('strong')}><strong>B</strong></button>
        <button type="button" aria-label="斜体" onClick={() => formatInline('em')}><em>I</em></button>
        <button type="button" onClick={() => toggleList(false)}>项目列表</button>
        <button type="button" onClick={() => toggleList(true)}>编号列表</button>
      </div>
      <div
        ref={editorRef}
        className="bake-rich-editor__content"
        contentEditable
        suppressContentEditableWarning
        role="textbox"
        aria-label={ariaLabel}
        aria-multiline="true"
        data-placeholder={placeholder}
        onInput={(event) => {
          const next = editorMarkdown(event.currentTarget)
          lastEmittedValue.current = next
          onChange(next)
        }}
      />
    </div>
  )
}

export default BakeRichTextEditor
