export type CreationInlineEditAction = 'brainstorm' | 'polish' | 'expand' | 'elaborate'

export interface CreationInlineEditCapabilities {
  schema_version: 'creation.inline-edit.v1'
  enabled: boolean
  actions: CreationInlineEditAction[]
  max_selection_bytes: number
  max_custom_prompt_bytes: number
  supported_node_kinds: string[]
  history_id: number | null
  revision_no: number | null
  base_document_hash: string | null
  disabled_reason: string | null
}

export interface CreationSelectionSnapshot {
  baseRevisionNo: number
  baseDocumentHash: string
  sourceVersion: string
  startOffset: number
  endOffset: number
  startByte: number
  endByte: number
  selectedMarkdown: string
  selectedMarkdownHash: string
  selectedText: string
  startLine: number
  endLine: number
  nodeKind: string
  anchorRect: { left: number; top: number; width: number; height: number; bottom: number }
}

export interface InlineEditResponse {
  schema_version: 'creation.inline-edit.v1'
  request_id: string
  status: 'paused' | 'committed' | 'cancelled' | 'no_change' | 'undone'
  operation_fingerprint: string
  content?: string
  replacement_markdown?: string
  revision_no?: number
  patch?: Record<string, unknown>
  model_request?: {
    request_id?: string
    messages?: Array<{ role: string; content: string }>
  }
  resume_state?: Record<string, unknown>
}

const INTERNAL_MARKER_RE = /\\?<!--\s*\/?memorybread:data-risks(?::[a-f0-9]+)?\s*-->/gi

export const visibleCreationSource = (source: string) => {
  let visible = ''
  const visibleToOriginal: number[] = [0]
  let cursor = 0
  for (const match of source.matchAll(INTERNAL_MARKER_RE)) {
    const index = match.index ?? cursor
    for (let offset = cursor; offset < index; offset += 1) {
      visible += source[offset]
      visibleToOriginal.push(offset + 1)
    }
    cursor = index + match[0].length
    visibleToOriginal[visibleToOriginal.length - 1] = cursor
  }
  for (let offset = cursor; offset < source.length; offset += 1) {
    visible += source[offset]
    visibleToOriginal.push(offset + 1)
  }
  return { visible, visibleToOriginal }
}

export const sha256Hex = async (value: string) => {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}

const nodeElement = (node: Node | null): HTMLElement | null => {
  if (!node) return null
  const element = node.nodeType === Node.ELEMENT_NODE
    ? node as HTMLElement
    : node.parentElement
  return element?.closest<HTMLElement>('[data-md-start][data-md-end][data-md-kind]') || null
}

const textOffsetWithin = (element: HTMLElement, container: Node, offset: number) => {
  const range = document.createRange()
  range.selectNodeContents(element)
  range.setEnd(container, offset)
  return range.toString().length
}

const selectionIntersectsUnsupportedContent = (range: Range, container: HTMLElement) => {
  const unsupported = container.querySelectorAll('pre, table, figure, img')
  return [...unsupported].some((element) => {
    try {
      return range.intersectsNode(element)
    } catch {
      return true
    }
  })
}

const lineAtOffset = (source: string, offset: number) => source.slice(0, offset).split('\n').length

const markdownVisibleTextMap = (source: string) => {
  let visible = ''
  const charStarts: number[] = []
  const charEnds: number[] = []
  let offset = 0
  let lineStart = true
  const doubleAsteriskIsBalanced = (source.match(/\*\*/g)?.length || 0) % 2 === 0
  const append = (value: string, sourceStart: number, sourceEnd: number) => {
    visible += value
    for (let index = 0; index < value.length; index += 1) {
      charStarts.push(sourceStart + index)
      charEnds.push(index === value.length - 1 ? sourceEnd : sourceStart + index + 1)
    }
  }

  while (offset < source.length) {
    if (lineStart) {
      const blockPrefix = source.slice(offset).match(/^(?:#{1,6}|>|[-+*]|\d+\.)[ \t]+/)
      if (blockPrefix) {
        offset += blockPrefix[0].length
        lineStart = false
        continue
      }
    }

    const link = source.slice(offset).match(/^\[([^\]\n]+)]\(([^)\n]+)\)/)
    if (link) {
      const labelStart = offset + 1
      append(link[1], labelStart, labelStart + link[1].length)
      offset += link[0].length
      lineStart = false
      continue
    }
    const image = source.slice(offset).match(/^!\[[^\]\n]*]\([^)\n]+\)/)
    if (image) {
      offset += image[0].length
      lineStart = false
      continue
    }
    if (!doubleAsteriskIsBalanced && source.startsWith('**', offset)) {
      append('**', offset, offset + 2)
      offset += 2
      lineStart = false
      continue
    }
    const marker = ['***', '___', '**', '__', '~~', '`', '*', '_']
      .find(candidate => source.startsWith(candidate, offset))
    if (marker) {
      offset += marker.length
      continue
    }
    if (source[offset] === '\\' && offset + 1 < source.length) {
      append(source[offset + 1], offset, offset + 2)
      offset += 2
      lineStart = false
      continue
    }

    const char = source[offset]
    append(char, offset, offset + 1)
    offset += 1
    lineStart = char === '\n'
  }
  return { visible, charStarts, charEnds }
}

const MARKDOWN_ALIGNMENT_GAP_RE = /^[\s*_~`#>+.!\-[\]()]*$/

// CommonMark can recover from malformed emphasis by pairing only part of an
// odd `**` sequence. The lightweight mapper above deliberately stays simple,
// so use a fail-closed alignment fallback when its visible text differs from
// the browser DOM. Only Markdown punctuation may be skipped; ordinary source
// text must still match the rendered element character for character.
const alignRenderedTextToMarkdown = (source: string, rendered: string) => {
  if (!rendered) return null
  const charStarts: number[] = []
  const charEnds: number[] = []
  let sourceCursor = 0

  for (let renderedOffset = 0; renderedOffset < rendered.length; renderedOffset += 1) {
    const character = rendered[renderedOffset]
    const sourceOffset = source.indexOf(character, sourceCursor)
    if (sourceOffset < 0) return null
    if (!MARKDOWN_ALIGNMENT_GAP_RE.test(source.slice(sourceCursor, sourceOffset))) return null
    charStarts.push(sourceOffset)
    charEnds.push(sourceOffset + 1)
    sourceCursor = sourceOffset + 1
  }

  if (!MARKDOWN_ALIGNMENT_GAP_RE.test(source.slice(sourceCursor))) return null
  return { visible: rendered, charStarts, charEnds }
}

const sourceBoundaryWithinElement = ({
  element,
  boundaryContainer,
  boundaryOffset,
  sourceSlice,
  edge,
}: {
  element: HTMLElement
  boundaryContainer: Node
  boundaryOffset: number
  sourceSlice: string
  edge: 'start' | 'end'
}) => {
  const elementText = element.textContent || ''
  if (!elementText) return null
  let localTextOffset: number
  try {
    localTextOffset = textOffsetWithin(element, boundaryContainer, boundaryOffset)
  } catch {
    return null
  }
  if (localTextOffset < 0 || localTextOffset > elementText.length) return null

  let mappedSource = markdownVisibleTextMap(sourceSlice)
  let elementTextStart = 0
  if (mappedSource.visible !== elementText) {
    elementTextStart = mappedSource.visible.indexOf(elementText)
    if (
      elementTextStart < 0
      || mappedSource.visible.indexOf(elementText, elementTextStart + 1) >= 0
    ) {
      const aligned = alignRenderedTextToMarkdown(sourceSlice, elementText)
      if (!aligned) return null
      mappedSource = aligned
      elementTextStart = 0
    }
  }

  const mappedTextOffset = elementTextStart + localTextOffset
  if (edge === 'start') {
    if (mappedTextOffset === mappedSource.visible.length) return sourceSlice.length
    return mappedSource.charStarts[mappedTextOffset] ?? null
  }
  if (mappedTextOffset === 0) return 0
  return mappedSource.charEnds[mappedTextOffset - 1] ?? null
}

const comparableSelectionText = (value: string) => value.replace(/\s+/g, '')

const selectionAnchorRect = (range: Range) => {
  const fallback = range.getBoundingClientRect()
  if (typeof range.getClientRects !== 'function') return fallback
  const visibleRects = [...range.getClientRects()].filter(rect => (
    rect.width > 0
    && rect.height > 0
    && rect.bottom >= 0
    && rect.top <= window.innerHeight
  ))
  return visibleRects[visibleRects.length - 1] || fallback
}

export const resolveCreationSelection = async ({
  selection,
  container,
  originalSource,
  baseRevisionNo,
  baseDocumentHash,
  maxSelectionBytes,
  supportedNodeKinds,
}: {
  selection: Selection | null
  container: HTMLElement
  originalSource: string
  baseRevisionNo: number
  baseDocumentHash: string
  maxSelectionBytes: number
  supportedNodeKinds: string[]
}): Promise<CreationSelectionSnapshot | null> => {
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null
  const range = selection.getRangeAt(0)
  if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) return null
  const startElement = nodeElement(range.startContainer)
  const endElement = nodeElement(range.endContainer)
  if (!startElement || !endElement || selectionIntersectsUnsupportedContent(range, container)) return null

  const startNodeKind = startElement.dataset.mdKind || ''
  const endNodeKind = endElement.dataset.mdKind || ''
  if (!supportedNodeKinds.includes(startNodeKind) || !supportedNodeKinds.includes(endNodeKind)) return null
  const startMdStart = Number(startElement.dataset.mdStart)
  const startMdEnd = Number(startElement.dataset.mdEnd)
  const endMdStart = Number(endElement.dataset.mdStart)
  const endMdEnd = Number(endElement.dataset.mdEnd)
  if (
    !Number.isSafeInteger(startMdStart)
    || !Number.isSafeInteger(startMdEnd)
    || !Number.isSafeInteger(endMdStart)
    || !Number.isSafeInteger(endMdEnd)
    || startMdStart < 0
    || startMdEnd <= startMdStart
    || endMdStart < startMdStart
    || endMdEnd <= endMdStart
  ) return null

  const { visible, visibleToOriginal } = visibleCreationSource(originalSource)
  const selectedText = range.toString()
  if (!selectedText.trim()) return null
  const localSourceStart = sourceBoundaryWithinElement({
    element: startElement,
    boundaryContainer: range.startContainer,
    boundaryOffset: range.startOffset,
    sourceSlice: visible.slice(startMdStart, startMdEnd),
    edge: 'start',
  })
  const localSourceEnd = sourceBoundaryWithinElement({
    element: endElement,
    boundaryContainer: range.endContainer,
    boundaryOffset: range.endOffset,
    sourceSlice: visible.slice(endMdStart, endMdEnd),
    edge: 'end',
  })
  if (!Number.isSafeInteger(localSourceStart) || !Number.isSafeInteger(localSourceEnd)) return null
  const visibleStart = startMdStart + Number(localSourceStart)
  const visibleEnd = endMdStart + Number(localSourceEnd)
  let startOffset = visibleToOriginal[visibleStart]
  let endOffset = visibleToOriginal[visibleEnd]
  if (!Number.isSafeInteger(startOffset) || !Number.isSafeInteger(endOffset) || endOffset <= startOffset) return null
  const boundaryCandidates: Array<[number, number]> = [[startOffset, endOffset]]
  const startsAfterEmphasis = originalSource.slice(Math.max(0, startOffset - 2), startOffset) === '**'
  const endsBeforeEmphasis = originalSource.slice(endOffset, endOffset + 2) === '**'
  if (startsAfterEmphasis) boundaryCandidates.push([startOffset - 2, endOffset])
  if (endsBeforeEmphasis) boundaryCandidates.push([startOffset, endOffset + 2])
  if (startsAfterEmphasis && endsBeforeEmphasis) {
    boundaryCandidates.push([startOffset - 2, endOffset + 2])
  }
  const exactBoundary = boundaryCandidates.find(([candidateStart, candidateEnd]) => {
    const candidate = originalSource.slice(candidateStart, candidateEnd)
    return comparableSelectionText(markdownVisibleTextMap(candidate).visible)
      === comparableSelectionText(selectedText)
  })
  const resolvedBoundary = exactBoundary || boundaryCandidates.find(([candidateStart, candidateEnd]) => (
    Boolean(alignRenderedTextToMarkdown(
      originalSource.slice(candidateStart, candidateEnd),
      selectedText,
    ))
  ))
  if (!resolvedBoundary) return null
  ;[startOffset, endOffset] = resolvedBoundary
  const selectedMarkdown = originalSource.slice(startOffset, endOffset)
  if (/memorybread:|<!--|```|~~~/.test(selectedMarkdown)) return null
  // Existing documents may already contain a broken `**` delimiter. CommonMark
  // renders it as visible text, so keep the selection actionable; the sidecar
  // deterministically removes that damaged emphasis during the next edit.
  if (
    (selectedMarkdown.match(/__/g)?.length || 0) % 2 !== 0
    || (selectedMarkdown.match(/~~/g)?.length || 0) % 2 !== 0
    || (selectedMarkdown.match(/`/g)?.length || 0) % 2 !== 0
  ) return null
  const selectedVisibleText = markdownVisibleTextMap(selectedMarkdown).visible
  if (
    comparableSelectionText(selectedVisibleText) !== comparableSelectionText(selectedText)
    && !alignRenderedTextToMarkdown(selectedMarkdown, selectedText)
  ) return null
  const startByte = new TextEncoder().encode(originalSource.slice(0, startOffset)).length
  const endByte = startByte + new TextEncoder().encode(selectedMarkdown).length
  if (endByte - startByte > maxSelectionBytes) return null
  const rect = selectionAnchorRect(range)

  return {
    baseRevisionNo,
    baseDocumentHash,
    sourceVersion: await sha256Hex(originalSource),
    startOffset,
    endOffset,
    startByte,
    endByte,
    selectedMarkdown,
    selectedMarkdownHash: await sha256Hex(selectedMarkdown),
    selectedText,
    startLine: lineAtOffset(originalSource, startOffset),
    endLine: lineAtOffset(originalSource, endOffset),
    nodeKind: startElement === endElement ? startNodeKind : 'multi_block',
    anchorRect: {
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
      bottom: rect.bottom,
    },
  }
}

const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' ? value as Record<string, unknown> : {}
)

export const verifyInlineEditResponse = async (
  snapshot: CreationSelectionSnapshot,
  currentDocument: string,
  response: InlineEditResponse,
) => {
  if (response.status !== 'committed' || !response.content || response.replacement_markdown == null || !response.patch) {
    return false
  }
  if (await sha256Hex(currentDocument) !== snapshot.baseDocumentHash) return false
  const patch = response.patch
  if (patch.base_hash !== snapshot.baseDocumentHash || patch.result_hash !== await sha256Hex(response.content)) return false
  const prefix = currentDocument.slice(0, snapshot.startOffset)
  const suffix = currentDocument.slice(snapshot.endOffset)
  if (patch.prefix_hash !== await sha256Hex(prefix) || patch.suffix_hash !== await sha256Hex(suffix)) return false
  if (`${prefix}${response.replacement_markdown}${suffix}` !== response.content) return false
  const selection = asRecord(patch.selection)
  const replacement = asRecord(patch.replacement)
  if (selection.selected_markdown_hash !== snapshot.selectedMarkdownHash) return false
  if (replacement.replacement_markdown_hash !== await sha256Hex(response.replacement_markdown)) return false
  return patch.preserved_untouched === true
}

export const inlineEditActionLabel = (action: CreationInlineEditAction) => ({
  brainstorm: '脑暴',
  polish: '润色',
  expand: '扩充',
  elaborate: '细化',
}[action])
