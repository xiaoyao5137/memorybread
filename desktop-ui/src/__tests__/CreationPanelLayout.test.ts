// @ts-expect-error Vitest runs in Node, while the desktop UI tsconfig intentionally omits Node types.
import { readFileSync } from 'node:fs'
// @ts-expect-error Vitest runs in Node, while the desktop UI tsconfig intentionally omits Node types.
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

declare const process: { cwd: () => string }

const creationPanelStyles = readFileSync(
  resolve(process.cwd(), 'src/components/CreationPanel.css'),
  'utf8',
)

const cssRule = (selector: string) => {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = creationPanelStyles.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`))
  expect(match, `missing CSS rule: ${selector}`).not.toBeNull()
  return match?.[1] || ''
}

describe('CreationPanel constrained-window layout', () => {
  it('keeps vertically overflowing controls reachable in the chat pane', () => {
    const chatShell = cssRule('.creation-chat-shell')

    expect(chatShell).toContain('overflow-x: hidden')
    expect(chatShell).toContain('overflow-y: auto')
    expect(chatShell).toContain('overscroll-behavior-y: contain')
  })

  it('wraps brainstorm actions instead of clipping the confirmation button', () => {
    const brainstormFooter = cssRule('.creation-brainstorm-card > footer,\n.creation-brainstorm-ready')

    expect(brainstormFooter).toContain('flex-wrap: wrap')
  })

  it('scopes primary brainstorm button colors to action rows', () => {
    expect(creationPanelStyles).not.toContain('.creation-brainstorm-ready button {')
    expect(creationPanelStyles).toContain('.creation-brainstorm-ready__actions > button')
    expect(creationPanelStyles).toContain('.creation-brainstorm-continuation__actions > button')
  })

  it('gives continuation choices an explicit high-contrast card palette', () => {
    const continuationCard = cssRule(
      '.creation-brainstorm-continuation .creation-brainstorm-options--continuation > button',
    )
    const selectedContinuationCard = cssRule(
      '.creation-brainstorm-continuation .creation-brainstorm-options--continuation > button.is-selected',
    )
    const continuationDescription = cssRule(
      '.creation-brainstorm-options--continuation > button > span:last-child > small',
    )

    expect(continuationCard).toContain('background: #fffdfb')
    expect(continuationCard).toContain('color: #2f2924')
    expect(selectedContinuationCard).toContain('background: #fff1e3')
    expect(continuationDescription).toContain('color: #596272')
  })

  it('keeps the inline polish primary action visible before hover', () => {
    const primaryAction = cssRule('.creation-selection-toolbar button.is-primary')

    expect(primaryAction).toContain('background: var(--mb-brand-strong)')
    expect(primaryAction).toContain('color: var(--mb-bg-card)')
    expect(primaryAction).not.toContain('--mb-brand-primary')
  })
})
