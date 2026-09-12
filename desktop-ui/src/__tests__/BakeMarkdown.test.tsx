import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
// @ts-expect-error Vitest runs in Node; the UI tsconfig omits Node types.
import { readFileSync } from 'node:fs'
import { BakeMarkdown } from '../components/bake/BakeShared'
import { prepareBakeMarkdown } from '../utils/bakeMarkdown'

// Same structure as document #366: adjacent heading/table, optional trailing
// pipes, aligned columns, multiple tables and a list with emphasized labels.
const content = `### 按 GPU 卡型分布
| GPU 型号 | 卡数 | 使用率 |
| :--- | ---: | :---: |
| X40 | 720 | 76.9%
| 4090 | 527 | 81.9%
| X40-QUARTER | inf | - |



### 按子领域分布
| 子领域 | GPU 卡数 |
| --- | --- |
| 示例业务 | 459 |

## 关键指标说明
- **UTIL**: 峰均值约 60%。
- **SMACT**: 峰均值约 39.7%。`

describe('BakeMarkdown', () => {
  it('recognizes a table directly following a list label in historical content', () => {
    render(<BakeMarkdown content={'- **补充明细**\n| 型号 | 数量 |\n| :--- | ---: |\n| 示例卡型 | 307 |'} />)
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '307' })).toBeInTheDocument()
    expect(screen.getByRole('listitem')).toHaveTextContent('补充明细')
  })

  it('recognizes a single-column table after a list label', () => {
    render(<BakeMarkdown content={'- label\n| value |\n| --- |\n| cell |'} />)
    expect(screen.getByRole('cell', { name: 'cell' })).toBeInTheDocument()
  })

  it('does not repair table-like code or non-table pipe text', () => {
    for (const source of [
      '````md\n```\n- label\n| A | B |\n| --- | --- |\n````',
      '~~~md\n- label\n| A | B |\n| --- | --- |\n~~~',
      '    - label\n    | A | B |\n    | --- | --- |',
      '- label\na | b\nnot a delimiter',
    ]) expect(prepareBakeMarkdown(source)).toBe(source)
  })

  it('renders extracted tables, alignment and lists without empty blocks', () => {
    const { container } = render(<section style={{ whiteSpace: 'pre-wrap' }}><BakeMarkdown content={content} /></section>)
    const tables = screen.getAllByRole('table')
    expect(tables).toHaveLength(2)
    expect(within(tables[0]).getAllByRole('columnheader')).toHaveLength(3)
    expect(within(tables[0]).getAllByRole('row')).toHaveLength(4)
    expect(within(tables[0]).getByRole('cell', { name: '76.9%' })).toHaveStyle({ textAlign: 'center' })
    expect(within(tables[0]).getByRole('cell', { name: '720' })).toHaveStyle({ textAlign: 'right' })
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(container.querySelectorAll('p:empty, br')).toHaveLength(0)
    expect(screen.getAllByRole('region', { name: '表格，可横向滚动' })[0]).toHaveAttribute('tabindex', '0')
  })

  it('overrides inherited whitespace while preserving code whitespace and table overflow', () => {
    // jsdom cannot lay out inherited whitespace; verify the scoped CSS contract.
    const css: string = readFileSync('src/components/bake/BakePanel.css', 'utf8')
    expect(css.match(/\.bake-markdown \{([^}]+)\}/)?.[1]).toContain('white-space: normal')
    expect(css.match(/\.bake-markdown pre \{([^}]+)\}/)?.[1]).toContain('white-space: pre')
    expect(css.match(/\.bake-markdown__table-scroll \{([^}]+)\}/)?.[1]).toContain('overflow-x: auto')
  })

  it('preserves code blocks, intentional hard breaks and escaped pipes', () => {
    const { container } = render(<BakeMarkdown content={'第一行  \n第二行\n\n```text\n  one\n\n\n  two\n```\n\n| 值 |\n| --- |\n| a\\|b |'} />)
    expect(container.querySelector('br')).toBeInTheDocument()
    expect(container.querySelector('pre code')?.textContent).toBe('  one\n\n\n  two\n')
    expect(screen.getByRole('cell', { name: 'a|b' })).toBeInTheDocument()
  })

  it('keeps untrusted HTML and unsafe links inert', () => {
    const { container } = render(<BakeMarkdown content={'<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[unsafe](javascript:alert%281%29)'} />)
    expect(container.querySelector('script, img, [onerror]')).toBeNull()
    expect(container.querySelector('a')).not.toHaveAttribute('href', expect.stringContaining('javascript:'))
  })

  it('handles empty content', () => {
    render(<BakeMarkdown content={'  \n\n'} />)
    expect(screen.getByText('暂无详细内容')).toBeInTheDocument()
  })
})
