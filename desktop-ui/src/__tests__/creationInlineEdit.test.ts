import { beforeAll, describe, expect, it } from 'vitest'
import {
  resolveCreationSelection,
  sha256Hex,
  verifyInlineEditResponse,
} from '../components/creation-selection/creationInlineEdit'

beforeAll(() => {
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => ({ left: 20, top: 100, width: 80, height: 20, bottom: 120 }),
  })
})

describe('creation inline edit selection contract', () => {
  it('maps a Unicode DOM selection to exact UTF-8 byte offsets', async () => {
    const source = '# 标题\n\n第二段😀内容'
    const container = document.createElement('div')
    const paragraph = document.createElement('p')
    paragraph.dataset.mdStart = '6'
    paragraph.dataset.mdEnd = '13'
    paragraph.dataset.mdKind = 'p'
    paragraph.textContent = '第二段😀内容'
    container.appendChild(paragraph)
    document.body.appendChild(container)

    const range = document.createRange()
    range.setStart(paragraph.firstChild as Text, 0)
    range.setEnd(paragraph.firstChild as Text, 5)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)

    const baseHash = await sha256Hex(source)
    const snapshot = await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 4,
      baseDocumentHash: baseHash,
      maxSelectionBytes: 100,
      supportedNodeKinds: ['p'],
    })

    expect(snapshot).toMatchObject({
      baseRevisionNo: 4,
      startOffset: 6,
      endOffset: 11,
      startByte: 10,
      endByte: 23,
      selectedMarkdown: '第二段😀',
      selectedText: '第二段😀',
      startLine: 3,
      endLine: 3,
      nodeKind: 'p',
    })
  })

  it('maps a long selection across a heading and multiple supported Markdown nodes', async () => {
    const headingSource = '### 新手与老手差异化路径'
    const firstParagraphSource = '**新手路径：** 系统提供预设选项，通过引导流程降低输入门槛。'
    const secondParagraphSource = '**老手路径：** 支持自由描述与高级参数控制，满足精细化需求。'
    const source = `${headingSource}\n\n${firstParagraphSource}\n\n${secondParagraphSource}`
    const firstParagraphStart = source.indexOf(firstParagraphSource)
    const secondParagraphStart = source.indexOf(secondParagraphSource)
    const container = document.createElement('div')
    container.innerHTML = [
      `<h3 data-md-start="0" data-md-end="${headingSource.length}" data-md-kind="h3">新手与老手差异化路径</h3>`,
      `<p data-md-start="${firstParagraphStart}" data-md-end="${firstParagraphStart + firstParagraphSource.length}" data-md-kind="p"><strong>新手路径：</strong> 系统提供预设选项，通过引导流程降低输入门槛。</p>`,
      `<p data-md-start="${secondParagraphStart}" data-md-end="${source.length}" data-md-kind="p"><strong>老手路径：</strong> 支持自由描述与高级参数控制，满足精细化需求。</p>`,
    ].join('')
    document.body.appendChild(container)
    const headingText = container.querySelector('h3')?.firstChild as Text
    const lastParagraphText = container.querySelectorAll('p')[1].lastChild as Text
    const range = document.createRange()
    range.setStart(headingText, 0)
    range.setEnd(lastParagraphText, lastParagraphText.length)
    Object.defineProperty(range, 'getClientRects', {
      configurable: true,
      value: () => [
        { left: 40, top: 30, width: 250, height: 24, bottom: 54 },
        { left: 40, top: 180, width: 320, height: 24, bottom: 204 },
      ],
    })
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)

    const snapshot = await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 1,
      baseDocumentHash: await sha256Hex(source),
      maxSelectionBytes: 1000,
      supportedNodeKinds: ['h3', 'p'],
    })

    expect(snapshot).toMatchObject({
      startOffset: 4,
      endOffset: source.length,
      selectedMarkdown: source.slice(4),
      nodeKind: 'multi_block',
      anchorRect: { top: 180, bottom: 204 },
    })
  })

  it('maps text inside common inline Markdown instead of silently hiding actions', async () => {
    const source = '**关键目标**是在 `MVP` 阶段完成验证。'
    const container = document.createElement('div')
    const paragraph = document.createElement('p')
    paragraph.dataset.mdStart = '0'
    paragraph.dataset.mdEnd = String(source.length)
    paragraph.dataset.mdKind = 'p'
    paragraph.innerHTML = '<strong>关键目标</strong>是在 <code>MVP</code> 阶段完成验证。'
    container.appendChild(paragraph)
    document.body.appendChild(container)

    const range = document.createRange()
    range.setStart(paragraph.querySelector('strong')?.firstChild as Text, 0)
    range.setEnd(paragraph.querySelector('strong')?.firstChild as Text, 4)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    const snapshot = await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 1,
      baseDocumentHash: await sha256Hex(source),
      maxSelectionBytes: 100,
      supportedNodeKinds: ['p'],
    })

    expect(snapshot).toMatchObject({
      startOffset: 2,
      endOffset: 6,
      selectedMarkdown: '关键目标',
      selectedText: '关键目标',
    })
  })

  it('keeps legacy unmatched emphasis markers selectable so the next edit can repair them', async () => {
    const source = '占比95%**。'
    const container = document.createElement('div')
    const paragraph = document.createElement('p')
    paragraph.dataset.mdStart = '0'
    paragraph.dataset.mdEnd = String(source.length)
    paragraph.dataset.mdKind = 'p'
    paragraph.textContent = source
    container.appendChild(paragraph)
    document.body.appendChild(container)

    const range = document.createRange()
    range.setStart(paragraph.firstChild as Text, 0)
    range.setEnd(paragraph.firstChild as Text, source.length)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)

    expect(await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 1,
      baseDocumentHash: await sha256Hex(source),
      maxSelectionBytes: 100,
      supportedNodeKinds: ['p'],
    })).toMatchObject({
      selectedMarkdown: source,
      selectedText: source,
    })
  })

  it('maps the malformed nested emphasis selection from the latest creation record', async () => {
    const first = '- **- **Tianmu-Video（基于 LTX2.3 微调）:** 承担主要生产负载，占比**95%**。**触发条件**: 电商场景视频生成需求；**执行步骤**: 调用开源模型 LTX2.3 基础架构并加载针对电商场景的定向微调参数；**验收维度**: 输出内容需符合自研微调策略定义的电商风格与规范。'
    const second = '- **Minimax H3:** 作为备选方案覆盖**5%**流量。**触发条件**: 需要模型效果对比测试或特定非标准场景补充需求；**执行步骤**: 调用 Minimax H3 接口生成视频片段；**风险边界**: 该路径不用于常规生产负载，仅保留灵活调度空间以应对突发场景。'
    const source = `${first}\n${second}`
    const secondStart = first.length + 1
    const container = document.createElement('div')
    container.innerHTML = [
      '<ul>',
      `<li data-md-start="0" data-md-end="${first.length}" data-md-kind="li"><strong>- <strong>Tianmu-Video（基于 LTX2.3 微调）:</strong> 承担主要生产负载，占比</strong>95%**。<strong>触发条件</strong>: 电商场景视频生成需求；<strong>执行步骤</strong>: 调用开源模型 LTX2.3 基础架构并加载针对电商场景的定向微调参数；<strong>验收维度</strong>: 输出内容需符合自研微调策略定义的电商风格与规范。</li>`,
      `<li data-md-start="${secondStart}" data-md-end="${source.length}" data-md-kind="li"><strong>Minimax H3:</strong> 作为备选方案覆盖**5%**流量。<strong>触发条件</strong>: 需要模型效果对比测试或特定非标准场景补充需求；<strong>执行步骤</strong>: 调用 Minimax H3 接口生成视频片段；<strong>风险边界</strong>: 该路径不用于常规生产负载，仅保留灵活调度空间以应对突发场景。</li>`,
      '</ul>',
    ].join('')
    document.body.appendChild(container)

    const firstVisibleDash = container.querySelector('li strong')?.firstChild as Text
    const secondTrailingText = container.querySelectorAll('li')[1].lastChild as Text
    const range = document.createRange()
    range.setStart(firstVisibleDash, 0)
    range.setEnd(secondTrailingText, secondTrailingText.length)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)

    const snapshot = await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 3,
      baseDocumentHash: await sha256Hex(source),
      maxSelectionBytes: 12000,
      supportedNodeKinds: ['li'],
    })

    expect(snapshot).not.toBeNull()
    expect(snapshot?.startOffset).toBe(0)
    expect(snapshot?.endOffset).toBe(source.length)
    expect(snapshot?.selectedMarkdown).toBe(source)
    expect(snapshot?.selectedText).toContain('Tianmu-Video')
    expect(snapshot?.selectedText).toContain('Minimax H3')
  })

  it('maps a paragraph-to-partial-malformed-list selection from the latest creation record', async () => {
    const paragraph = '灵机独立站混剪/切片走的是 Tianmu-Omini 纯 AIGC 方案，模型配置如下:'
    const item = '- **- **Tianmu-Video（基于 LTX2.3 微调）:** 承担主要生产负载，占比**95%**。**触发条件**: 电商场景视频生成需求；**执行步骤**: 调用开源模型 LTX2.3 基础架构并加载针对电商场景的定向微调参数；**验收维度**: 输出内容需符合自研微调策略定义的电商风格与规范。'
    const source = `${paragraph}\n\n${item}`
    const itemStart = paragraph.length + 2
    const selectedItemTail = '输出内容需符合自研微调策略'
    const selectedEnd = source.indexOf(selectedItemTail) + selectedItemTail.length
    const container = document.createElement('div')
    container.innerHTML = [
      `<p data-md-start="0" data-md-end="${paragraph.length}" data-md-kind="p">${paragraph}</p>`,
      '<ul>',
      `<li data-md-start="${itemStart}" data-md-end="${source.length}" data-md-kind="li"><strong>- <strong>Tianmu-Video（基于 LTX2.3 微调）:</strong> 承担主要生产负载，占比</strong>95%**。<strong>触发条件</strong>: 电商场景视频生成需求；<strong>执行步骤</strong>: 调用开源模型 LTX2.3 基础架构并加载针对电商场景的定向微调参数；<strong>验收维度</strong>: 输出内容需符合自研微调策略定义的电商风格与规范。</li>`,
      '</ul>',
    ].join('')
    document.body.appendChild(container)

    const paragraphText = container.querySelector('p')?.firstChild as Text
    const itemTrailingText = container.querySelector('li')?.lastChild as Text
    const trailingEnd = itemTrailingText.data.indexOf(selectedItemTail) + selectedItemTail.length
    const range = document.createRange()
    range.setStart(paragraphText, 0)
    range.setEnd(itemTrailingText, trailingEnd)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)

    const snapshot = await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 3,
      baseDocumentHash: await sha256Hex(source),
      maxSelectionBytes: 12000,
      supportedNodeKinds: ['p', 'li'],
    })

    expect(snapshot).not.toBeNull()
    expect(snapshot?.startOffset).toBe(0)
    expect(snapshot?.endOffset).toBe(selectedEnd)
    expect(snapshot?.selectedMarkdown).toBe(source.slice(0, selectedEnd))
    expect(snapshot?.selectedText).toContain('模型配置如下')
    expect(snapshot?.selectedText).toContain(selectedItemTail)
  })

  it('expands a cross-list selection to include the opening emphasis boundary', async () => {
    const first = '- **Tianmu-Video:** 核心生产层主力模型，占比 95%，已验证。'
    const second = '- **Minimax H3:** 核心生产层备选方案，占比 5%，已验证。'
    const source = `${first}\n${second}`
    const secondStart = first.length + 1
    const container = document.createElement('div')
    container.innerHTML = [
      `<li data-md-start="0" data-md-end="${first.length}" data-md-kind="li"><strong>Tianmu-Video:</strong> 核心生产层主力模型，占比 95%，已验证。</li>`,
      `<li data-md-start="${secondStart}" data-md-end="${source.length}" data-md-kind="li"><strong>Minimax H3:</strong> 核心生产层备选方案，占比 5%，已验证。</li>`,
    ].join('')
    document.body.appendChild(container)

    const firstStrongText = container.querySelector('li strong')?.firstChild as Text
    const secondTrailingText = container.querySelectorAll('li')[1].lastChild as Text
    const range = document.createRange()
    range.setStart(firstStrongText, 0)
    range.setEnd(secondTrailingText, secondTrailingText.length)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)

    const snapshot = await resolveCreationSelection({
      selection,
      container,
      originalSource: source,
      baseRevisionNo: 3,
      baseDocumentHash: await sha256Hex(source),
      maxSelectionBytes: 1000,
      supportedNodeKinds: ['li'],
    })

    expect(snapshot).not.toBeNull()
    expect(snapshot?.selectedMarkdown).toBe(source.slice(2))
    expect(snapshot?.startOffset).toBe(2)
  })

  it('only accepts an exactly composed and hashed server patch', async () => {
    const currentDocument = '前缀选中文本后缀'
    const replacement = '更清晰的文本'
    const content = `前缀${replacement}后缀`
    const selectedMarkdownHash = await sha256Hex('选中文本')
    const snapshot = {
      baseRevisionNo: 2,
      baseDocumentHash: await sha256Hex(currentDocument),
      sourceVersion: await sha256Hex(currentDocument),
      startOffset: 2,
      endOffset: 6,
      startByte: 6,
      endByte: 18,
      selectedMarkdown: '选中文本',
      selectedMarkdownHash,
      selectedText: '选中文本',
      startLine: 1,
      endLine: 1,
      nodeKind: 'p',
      anchorRect: { left: 0, top: 0, width: 10, height: 10, bottom: 10 },
    }
    const response = {
      schema_version: 'creation.inline-edit.v1' as const,
      request_id: 'inline-request',
      status: 'committed' as const,
      operation_fingerprint: 'fingerprint',
      content,
      replacement_markdown: replacement,
      revision_no: 3,
      patch: {
        base_hash: snapshot.baseDocumentHash,
        result_hash: await sha256Hex(content),
        prefix_hash: await sha256Hex('前缀'),
        suffix_hash: await sha256Hex('后缀'),
        selection: { selected_markdown_hash: selectedMarkdownHash },
        replacement: { replacement_markdown_hash: await sha256Hex(replacement) },
        preserved_untouched: true,
      },
    }

    expect(await verifyInlineEditResponse(snapshot, currentDocument, response)).toBe(true)
    expect(await verifyInlineEditResponse(snapshot, currentDocument, {
      ...response,
      content: `${content}被篡改`,
    })).toBe(false)
  })
})
