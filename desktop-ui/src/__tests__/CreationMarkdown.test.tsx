import { render } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import ReactMarkdown from 'react-markdown'
import { prepareCreationMarkdown } from '../utils/creationMarkdown'
// @ts-expect-error Node is available in the test runtime.
import { readFileSync } from 'node:fs'

const cases: Array<{ source: string; expected: string }> = JSON.parse(readFileSync('../shared/creation-markdown/cases.json', 'utf8'))
describe('creation Markdown repairs', () => {
  it.each(cases)('normalizes and remains idempotent: $source', ({ source, expected }) => {
    expect(prepareCreationMarkdown(source).text).toBe(expected)
    expect(prepareCreationMarkdown(expected).text).toBe(expected)
  })
  it('renders CJK labels and broken list prefixes without literal stars', () => {
    const source = '- **控制维度：**通过输入控制。\n1. **- **策略**：每日执行。'
    const prepared = prepareCreationMarkdown(source)
    const { container } = render(<ReactMarkdown remarkPlugins={[prepared.restorePositions]}>{prepared.text}</ReactMarkdown>)
    expect(container.textContent).not.toContain('**')
    expect(container.querySelectorAll('strong')).toHaveLength(2)
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })
  it('maps a later label back to the original selection offsets', () => {
    const source = '- **- **策略**：执行。\n\n**后续**内容'
    const prepared = prepareCreationMarkdown(source)
    const positions: any[] = []
    render(<ReactMarkdown remarkPlugins={[prepared.restorePositions]} components={{ strong: ({ node, children }) => {
      positions.push(node?.position)
      return <strong>{children}</strong>
    } }}>{prepared.text}</ReactMarkdown>)
    expect(source.slice(positions[0].start.offset, positions[0].end.offset)).toBe('**策略**')
    expect(source.slice(positions[1].start.offset, positions[1].end.offset)).toBe('**后续**')
  })
})
