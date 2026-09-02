import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { sha256Hex } from '../components/creation-selection/creationInlineEdit'
import { useAppStore } from '../store/useAppStore'

const content = '# 测试文档\n\n这是一段可以润色的正文。'

describe('创作文档划选操作', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
    Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ left: 120, top: 240, width: 160, height: 22, bottom: 262 }),
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('恢复已完成文档后，鼠标划选跨标题与正文时会显示润色、扩充和细化', async () => {
    const baseHash = await sha256Hex(content)
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 101,
            prompt: '测试划选操作',
            root_request: '测试划选操作',
            generated_content: content,
            session_id: 'session-inline-test',
            lifecycle_status: 'completed',
            revision_no: 2,
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/capabilities') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          enabled: true,
          actions: ['polish', 'expand', 'elaborate'],
          max_selection_bytes: 12000,
          max_custom_prompt_bytes: 2000,
          supported_node_kinds: ['p', 'h1', 'h2', 'h3', 'li', 'blockquote'],
          history_id: 101,
          revision_no: 2,
          base_document_hash: baseHash,
          disabled_reason: null,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/run') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          request_id: 'inline-test-request',
          status: 'no_change',
          operation_fingerprint: 'inline-test-fingerprint',
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('测试划选操作'))

    const paragraph = await waitFor(() => {
      const node = document.querySelector('.creation-document-content p')
      expect(node).not.toBeNull()
      expect(node).toHaveAttribute('data-md-start')
      return node as HTMLParagraphElement
    })
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => (
      new URL(String(input)).pathname === '/api/creation/inline-edit/capabilities'
    ))).toBe(true))

    const heading = document.querySelector('.creation-document-content h1') as HTMLHeadingElement
    const headingText = heading.firstChild as Text
    const paragraphText = paragraph.firstChild as Text
    const range = document.createRange()
    range.setStart(headingText, 0)
    range.setEnd(paragraphText, 4)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(paragraph)

    expect(await screen.findByRole('toolbar', { name: '所选内容操作' })).toBeInTheDocument()
    const polishButton = screen.getByRole('button', { name: '润色' })
    expect(polishButton).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '扩充' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '细化' })).toBeInTheDocument()

    // WKWebView may collapse the document selection while the toolbar button is
    // being clicked. The toolbar interaction lock must keep the snapshot alive
    // long enough to render the optional polish prompt.
    fireEvent.pointerDown(polishButton)
    selection.removeAllRanges()
    fireEvent(document, new Event('selectionchange'))
    fireEvent.pointerUp(polishButton)
    fireEvent.click(polishButton)

    expect(await screen.findByLabelText('补充你的润色要求（可选）')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '开始润色' }))

    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input, init]) => {
      const url = new URL(String(input))
      if (url.pathname !== '/api/creation/inline-edit/run') return false
      const payload = JSON.parse(String(init?.body || '{}')) as { action?: string; selection?: { selected_text?: string } }
      return payload.action === 'polish' && Boolean(payload.selection?.selected_text)
    })).toBe(true))
    expect(await screen.findByText('所选内容已经符合要求，未产生修改')).toBeInTheDocument()
  })

  it('最新记录中的异常粗体列表跨项选中后仍弹出操作指令', async () => {
    const malformedContent = [
      '# AIGC 规模化生产',
      '',
      '### 模型使用与占比',
      '',
      '灵机独立站混剪/切片走的是 Tianmu-Omini 纯 AIGC 方案，模型配置如下:',
      '',
      '- **- **Tianmu-Video（基于 LTX2.3 微调）:** 承担主要生产负载，占比**95%**。**触发条件**: 电商场景视频生成需求；**执行步骤**: 调用开源模型 LTX2.3 基础架构并加载针对电商场景的定向微调参数；**验收维度**: 输出内容需符合自研微调策略定义的电商风格与规范。',
      '- **Minimax H3:** 作为备选方案覆盖**5%**流量。**触发条件**: 需要模型效果对比测试或特定非标准场景补充需求；**执行步骤**: 调用 Minimax H3 接口生成视频片段；**风险边界**: 该路径不用于常规生产负载，仅保留灵活调度空间以应对突发场景。',
    ].join('\n')
    const baseHash = await sha256Hex(malformedContent)
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 105,
            prompt: 'AIGC 模型总结',
            root_request: 'AIGC 模型总结',
            generated_content: malformedContent,
            session_id: 'session-malformed-selection',
            lifecycle_status: 'completed',
            revision_no: 3,
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/capabilities') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          enabled: true,
          actions: ['polish', 'expand', 'elaborate'],
          max_selection_bytes: 12000,
          max_custom_prompt_bytes: 2000,
          supported_node_kinds: ['p', 'h1', 'h2', 'h3', 'li', 'blockquote'],
          history_id: 105,
          revision_no: 3,
          base_document_hash: baseHash,
          disabled_reason: null,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('AIGC 模型总结'))
    const { paragraph, items } = await waitFor(() => {
      const nodes = document.querySelectorAll('.creation-document-content li')
      expect(nodes).toHaveLength(2)
      const paragraphs = [...document.querySelectorAll('.creation-document-content p')]
      const intro = paragraphs.find(node => node.textContent?.includes('Tianmu-Omini'))
      expect(intro).toBeTruthy()
      return { paragraph: intro as HTMLParagraphElement, items: nodes }
    })
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => (
      new URL(String(input)).pathname === '/api/creation/inline-edit/capabilities'
    ))).toBe(true))

    const paragraphText = paragraph.firstChild as Text
    const firstTrailingText = items[0].lastChild as Text
    const selectedTail = '输出内容需符合自研微调策略'
    const selectedTailEnd = firstTrailingText.data.indexOf(selectedTail) + selectedTail.length
    const range = document.createRange()
    range.setStart(paragraphText, 0)
    range.setEnd(firstTrailingText, selectedTailEnd)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(items[0])

    expect(await screen.findByRole('toolbar', { name: '所选内容操作' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '润色' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '扩充' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '细化' })).toBeInTheDocument()
  })

  it('能力服务未启动时不再静默，而是在选区旁给出恢复指引', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 102,
            prompt: '测试服务提示',
            root_request: '测试服务提示',
            generated_content: content,
            session_id: 'session-inline-unavailable',
            lifecycle_status: 'completed',
            revision_no: 1,
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('测试服务提示'))
    const paragraph = await waitFor(() => {
      const node = document.querySelector('.creation-document-content p')
      expect(node).toHaveAttribute('data-md-start')
      return node as HTMLParagraphElement
    })
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => (
      new URL(String(input)).pathname === '/api/creation/inline-edit/capabilities'
    ))).toBe(true))

    const range = document.createRange()
    range.setStart(paragraph.firstChild as Text, 0)
    range.setEnd(paragraph.firstChild as Text, 4)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent(document, new Event('selectionchange'))

    expect(await screen.findByText('选区编辑服务未启动，请重启客户端后再试')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '润色' })).not.toBeInTheDocument()
  })

  it('Core 已提交细化但本地响应校验失败时恢复持久化版本和右侧指令', async () => {
    const original = '# 中文文档\n\n模型承担主要生产负载，占比 95%。'
    const selected = '模型承担主要生产负载，占比 95%。'
    const replacement = '模型承担主要生产负载，占比 95%；触发条件、执行步骤与验收边界均需明确。'
    const committed = original.replace(selected, replacement)
    const baseHash = await sha256Hex(original)
    const resultHash = await sha256Hex(committed)
    let committedOnServer = false

    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history/104') {
        return Response.json({
          id: 104,
          prompt: '总结模型使用情况',
          root_request: '总结模型使用情况',
          generated_content: committed,
          session_id: 'session-inline-recovery',
          lifecycle_status: 'completed',
          revision_no: 2,
          edit_operation: 'elaborate_selection',
          document_patch_json: JSON.stringify({ operation: 'elaborate_selection', result_hash: resultHash }),
          references_json: '[]',
          conversation_json: JSON.stringify([
            { id: 'root-user', role: 'user', content: '总结模型使用情况', createdAt: 1 },
            {
              id: 'inline-user',
              role: 'user',
              content: `细化要求：细化所选内容，补齐对象、条件、步骤、边界、风险或验收维度\n选取内容：${selected}`,
              createdAt: 2,
              runId: 'inline-recovery-request',
            },
            { id: 'inline-assistant', role: 'assistant', content: '已完成细化所选内容。', createdAt: 3, runId: 'inline-recovery-request' },
          ]),
          agent_trace_json: '[]',
          evidence_json: '[]',
          created_at: 1,
          updated_at: 3,
        })
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 104,
            prompt: '总结模型使用情况',
            root_request: '总结模型使用情况',
            generated_content: committedOnServer ? committed : original,
            session_id: 'session-inline-recovery',
            lifecycle_status: 'completed',
            revision_no: committedOnServer ? 2 : 1,
            references_json: '[]',
            conversation_json: JSON.stringify([{ id: 'root-user', role: 'user', content: '总结模型使用情况', createdAt: 1 }]),
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/capabilities') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          enabled: true,
          actions: ['polish', 'expand', 'elaborate'],
          max_selection_bytes: 12000,
          max_custom_prompt_bytes: 2000,
          supported_node_kinds: ['p', 'h1'],
          history_id: 104,
          revision_no: 1,
          base_document_hash: baseHash,
          disabled_reason: null,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/run') {
        committedOnServer = true
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          request_id: 'inline-recovery-request',
          status: 'committed',
          operation_fingerprint: 'inline-recovery-fingerprint',
          content: committed,
          replacement_markdown: replacement,
          revision_no: 2,
          // 模拟传输层拿到的 patch 局部损坏；Core 中的提交结果仍完整。
          patch: {
            base_hash: baseHash,
            result_hash: resultHash,
            prefix_hash: 'invalid-prefix-hash',
            suffix_hash: await sha256Hex(''),
            selection: { selected_markdown_hash: await sha256Hex(selected) },
            replacement: { replacement_markdown_hash: await sha256Hex(replacement) },
            preserved_untouched: true,
            operation: 'elaborate_selection',
          },
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('总结模型使用情况'))
    const paragraph = await waitFor(() => document.querySelector('.creation-document-content p') as HTMLParagraphElement)
    const paragraphText = paragraph.firstChild as Text
    const range = document.createRange()
    range.setStart(paragraphText, 0)
    range.setEnd(paragraphText, paragraphText.data.length)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(paragraph)
    fireEvent.click(await screen.findByRole('button', { name: '细化' }))

    expect(await screen.findByText((_, element) => (
      element?.tagName === 'P' && element.textContent === replacement
    ))).toBeInTheDocument()
    expect(screen.getByText((_, element) => (
      element?.tagName === 'P'
      && Boolean(element.textContent?.includes('细化要求：细化所选内容'))
      && Boolean(element.textContent?.includes(`选取内容：${selected}`))
    ))).toBeInTheDocument()
    expect(screen.queryByText('修改结果校验失败，原文未在本地应用，请重新划选')).not.toBeInTheDocument()
  })

  it('扩充完成后保留执行片段的视口位置，不自动滚到文档底部', async () => {
    const baseHash = await sha256Hex(content)
    let latestCapabilitiesHash = baseHash
    let latestRevision = 1
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const top = this.classList.contains('creation-document-content') ? 100 : 260
      return {
        x: 0,
        y: top,
        left: 0,
        right: 600,
        top,
        bottom: top + 24,
        width: 600,
        height: 24,
        toJSON: () => ({}),
      } as DOMRect
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 103,
            prompt: '测试执行片段定位',
            root_request: '测试执行片段定位',
            generated_content: content,
            session_id: 'session-inline-scroll',
            lifecycle_status: 'completed',
            revision_no: 1,
            references_json: '[]',
            conversation_json: JSON.stringify([{
              id: 'prior-user-message',
              role: 'user',
              content: '请生成一份测试文档',
              createdAt: 1,
            }]),
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/capabilities') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          enabled: true,
          actions: ['polish', 'expand', 'elaborate'],
          max_selection_bytes: 12000,
          max_custom_prompt_bytes: 2000,
          supported_node_kinds: ['p', 'h1', 'h2', 'h3', 'li', 'blockquote'],
          history_id: 103,
          revision_no: latestRevision,
          base_document_hash: latestCapabilitiesHash,
          disabled_reason: null,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/run') {
        const payload = JSON.parse(String(init?.body || '{}')) as {
          current_document: string
          selection: { selected_markdown: string; selected_markdown_hash: string }
        }
        const selected = payload.selection.selected_markdown
        const start = payload.current_document.indexOf(selected)
        const prefix = payload.current_document.slice(0, start)
        const suffix = payload.current_document.slice(start + selected.length)
        const replacement = `${selected}，并补充执行细节`
        const nextContent = `${prefix}${replacement}${suffix}`
        latestCapabilitiesHash = await sha256Hex(nextContent)
        latestRevision = 2
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          request_id: 'inline-scroll-request',
          status: 'committed',
          operation_fingerprint: 'inline-scroll-fingerprint',
          content: nextContent,
          replacement_markdown: replacement,
          revision_no: 2,
          patch: {
            base_hash: baseHash,
            result_hash: latestCapabilitiesHash,
            prefix_hash: await sha256Hex(prefix),
            suffix_hash: await sha256Hex(suffix),
            selection: { selected_markdown_hash: payload.selection.selected_markdown_hash },
            replacement: { replacement_markdown_hash: await sha256Hex(replacement) },
            preserved_untouched: true,
            operation: 'expand_selection',
          },
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('测试执行片段定位'))
    const paragraph = await waitFor(() => {
      const node = document.querySelector('.creation-document-content p')
      expect(node).toHaveAttribute('data-md-start')
      return node as HTMLParagraphElement
    })
    const documentViewport = document.querySelector('.creation-document-content') as HTMLDivElement
    Object.defineProperty(documentViewport, 'scrollHeight', { configurable: true, value: 1200 })
    documentViewport.scrollTop = 320
    const chatTimeline = document.querySelector('.creation-chat-timeline') as HTMLDivElement
    Object.defineProperty(chatTimeline, 'scrollHeight', { configurable: true, value: 900 })
    chatTimeline.scrollTop = 180

    const paragraphText = paragraph.firstChild as Text
    const range = document.createRange()
    range.setStart(paragraphText, 0)
    range.setEnd(paragraphText, paragraphText.data.length)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(paragraph)

    const expandButton = await screen.findByRole('button', { name: '扩充' })
    fireEvent.pointerDown(expandButton)
    selection.removeAllRanges()
    fireEvent(document, new Event('selectionchange'))
    fireEvent.pointerUp(expandButton)
    fireEvent.click(expandButton)

    await screen.findByText('这是一段可以润色的正文。，并补充执行细节')
    expect(await screen.findByText((_, element) => (
      element?.tagName === 'P'
      && Boolean(element.textContent?.includes('扩充要求：基于已有上下文扩充所选内容'))
      && Boolean(element.textContent?.includes('选取内容：这是一段可以润色的正文。'))
    ))).toBeInTheDocument()
    await waitFor(() => expect(documentViewport.scrollTop).toBe(320))
    expect(documentViewport.scrollTop).not.toBe(documentViewport.scrollHeight)
    expect(chatTimeline.scrollTop).toBe(180)
    expect(chatTimeline.scrollTop).not.toBe(chatTimeline.scrollHeight)

    await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([input]) => (
      new URL(String(input)).pathname === '/api/creation/inline-edit/capabilities'
    )).length).toBeGreaterThan(1))
    const updatedParagraph = document.querySelector('.creation-document-content p') as HTMLParagraphElement
    const updatedText = updatedParagraph.firstChild as Text
    const secondRange = document.createRange()
    secondRange.setStart(updatedText, 0)
    secondRange.setEnd(updatedText, 4)
    selection.removeAllRanges()
    selection.addRange(secondRange)
    fireEvent.pointerUp(updatedParagraph)

    expect(await screen.findByRole('toolbar', { name: '所选内容操作' })).toBeInTheDocument()
  })

  it('云端扩充超时时保留右侧指令、明确失败原因并清理暂停运行', async () => {
    const baseHash = await sha256Hex(content)
    useAppStore.setState({
      currentUser: {
        id: '00000000-0000-0000-0000-000000000123',
        status: 'active',
        roles: ['user'],
        locale: 'zh-CN',
        timezone: 'Asia/Shanghai',
        created_at: '2026-09-02T00:00:00Z',
      },
      cloudBalance: {
        available: '10.0000',
        reserved: '0.0000',
        currency: 'CREDIT',
        as_of: '2026-09-02T00:00:00Z',
      },
      creationModelConfigs: [
        { id: 'mbcd-plus-v1', enabled: true, apiKey: '' },
        { id: 'mbcd-std-v1', enabled: false, apiKey: '' },
      ],
    })
    let cancelledRequestId = ''
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 106,
            prompt: '测试云端失败反馈',
            root_request: '测试云端失败反馈',
            generated_content: content,
            session_id: 'session-inline-cloud-timeout',
            lifecycle_status: 'completed',
            revision_no: 1,
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/capabilities') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          enabled: true,
          actions: ['polish', 'expand', 'elaborate'],
          max_selection_bytes: 12000,
          max_custom_prompt_bytes: 2000,
          supported_node_kinds: ['p', 'h1'],
          history_id: 106,
          revision_no: 1,
          base_document_hash: baseHash,
          disabled_reason: null,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/run') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          request_id: 'inline-cloud-timeout',
          status: 'paused',
          operation_fingerprint: 'inline-cloud-timeout-fingerprint',
          resume_state: { token: 'resume-token' },
          model_request: {
            request_id: 'inline-cloud-model-request',
            messages: [{ role: 'user', content: '扩充选中的正文' }],
          },
        })
      }
      if (url.pathname === '/v1/gateway/chat') {
        return new Response(
          'data: {"type":"error","code":"MODEL_SERVICE_TIMEOUT","message":"云端模型响应超时，请重试","retryable":true}\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      }
      if (url.pathname === '/api/creation/inline-edit/cancel') {
        const payload = JSON.parse(String(init?.body || '{}')) as { request_id?: string }
        cancelledRequestId = String(payload.request_id || '')
        return Response.json({ status: 'cancelled' })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('测试云端失败反馈'))
    const paragraph = await waitFor(() => document.querySelector('.creation-document-content p') as HTMLParagraphElement)
    const paragraphText = paragraph.firstChild as Text
    const range = document.createRange()
    range.setStart(paragraphText, 0)
    range.setEnd(paragraphText, paragraphText.data.length)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(paragraph)
    fireEvent.click(await screen.findByRole('button', { name: '扩充' }))

    expect(await screen.findByText((_, element) => (
      element?.tagName === 'P'
      && Boolean(element.textContent?.includes('扩充要求：基于已有上下文扩充所选内容'))
      && Boolean(element.textContent?.includes('选取内容：这是一段可以润色的正文。'))
    ))).toBeInTheDocument()
    expect(await screen.findByText('云端模型响应超时，请重试，文档未修改。')).toBeInTheDocument()
    await waitFor(() => expect(cancelledRequestId).toMatch(/^inline-/))
    expect(screen.getByText((_, element) => (
      element?.tagName === 'P' && element.textContent === '这是一段可以润色的正文。'
    ))).toBeInTheDocument()
  })

  it('在润色左侧启动局部脑暴，右侧确认选项后将结论写回选区', async () => {
    const baseHash = await sha256Hex(content)
    const brainstormPayloads: Array<Record<string, unknown>> = []
    const inlinePayloads: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([])
      if (url.pathname === '/api/creation/history') {
        return Response.json({
          items: [{
            id: 107,
            prompt: '测试局部脑暴',
            root_request: '测试局部脑暴',
            generated_content: content,
            session_id: 'session-inline-brainstorm',
            lifecycle_status: 'completed',
            revision_no: 1,
            references_json: '[]',
            conversation_json: '[]',
            agent_trace_json: '[]',
            evidence_json: '[]',
            created_at: 1,
            updated_at: 2,
          }],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }
      if (url.pathname === '/api/creation/inline-edit/capabilities') {
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          enabled: true,
          actions: ['brainstorm', 'polish', 'expand', 'elaborate'],
          max_selection_bytes: 12000,
          max_custom_prompt_bytes: 2000,
          supported_node_kinds: ['p', 'h1'],
          history_id: 107,
          revision_no: 1,
          base_document_hash: baseHash,
          disabled_reason: null,
        })
      }
      if (url.pathname === '/api/creation/brainstorm/turn') {
        const payload = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>
        brainstormPayloads.push(payload)
        if (payload.action === 'answer') {
          return Response.json({
            session_id: payload.session_id,
            phase: 'ready',
            revision: 1,
            current_question: null,
            brief_markdown: '# 局部脑暴结论',
            answered_count: 1,
            depth: 1,
            can_continue_brainstorm: true,
            open_flags: [],
            readiness_reason: '方向明确',
            continuation_directions: [{ id: 'challenge', label: '挑战论证', description: '检查反例', recommended: true }],
            invalidated_question_ids: [],
            history: [],
            decisions: [{ question_id: 'direction', dimension: '表达方向', summary: '突出用户价值', source: 'user' }],
          })
        }
        return Response.json({
          session_id: payload.session_id,
          phase: 'exploring',
          revision: 0,
          current_question: {
            id: 'direction',
            dimension: '表达方向',
            type: 'single_choice',
            prompt: '这段内容最需要突出什么？',
            why_now: '先确认方向，避免直接改写偏离原意。',
            required: true,
            allow_custom: true,
            options: [
              { id: 'value', label: '用户价值', description: '突出读者能获得的结果', recommended: true },
              { id: 'mechanism', label: '实现机制', description: '突出这件事如何发生', recommended: false },
            ],
            answer_template: '描述其他方向',
          },
          brief_markdown: '',
          answered_count: 0,
          depth: 0,
          can_continue_brainstorm: false,
          open_flags: [],
          readiness_reason: '',
          continuation_directions: [],
          invalidated_question_ids: [],
          history: [],
          decisions: [],
        })
      }
      if (url.pathname === '/api/creation/inline-edit/run') {
        const payload = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>
        inlinePayloads.push(payload)
        return Response.json({
          schema_version: 'creation.inline-edit.v1',
          request_id: payload.request_id,
          status: 'no_change',
          operation_fingerprint: 'inline-brainstorm-fingerprint',
        })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '创作记录 (1)' }))
    fireEvent.click(await screen.findByText('测试局部脑暴'))
    const paragraph = await waitFor(() => document.querySelector('.creation-document-content p') as HTMLParagraphElement)
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => (
      new URL(String(input)).pathname === '/api/creation/inline-edit/capabilities'
    ))).toBe(true))
    const range = document.createRange()
    range.setStart(paragraph.firstChild as Text, 0)
    range.setEnd(paragraph.firstChild as Text, 8)
    const selection = window.getSelection() as Selection
    selection.removeAllRanges()
    selection.addRange(range)
    fireEvent.pointerUp(paragraph)

    const brainstormButton = await screen.findByRole('button', { name: '脑暴' })
    const polishButton = screen.getByRole('button', { name: '润色' })
    expect(brainstormButton.compareDocumentPosition(polishButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(brainstormButton)

    const instruction = await screen.findByText((_, element) => (
      element?.tagName === 'P' && Boolean(element.textContent?.includes('脑暴要求：围绕所选内容探索'))
    ))
    const card = await screen.findByText('这段内容最需要突出什么？')
    expect(instruction.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByRole('radio', { name: /用户价值/ })).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(screen.getByRole('button', { name: /确认并继续/ }))

    expect(await screen.findByText('选中的方向已经足够用于改写所选内容')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /应用到所选内容/ }))
    await waitFor(() => expect(inlinePayloads).toHaveLength(1))
    expect(brainstormPayloads.map(payload => payload.action)).toEqual(['start', 'answer'])
    expect(inlinePayloads[0]).toMatchObject({ action: 'brainstorm' })
    expect(String(inlinePayloads[0].custom_prompt)).toContain('表达方向：突出用户价值')
    expect(await screen.findByText('已应用到所选内容')).toBeInTheDocument()
  })
})
