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
const creationPanelSource = readFileSync(
  resolve(process.cwd(), 'src/components/CreationPanel.tsx'),
  'utf8',
)

const cssRule = (selector: string) => {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = creationPanelStyles.match(new RegExp(`(?:^|\\n)${escapedSelector}\\s*\\{([^}]*)\\}`))
  expect(match, `missing CSS rule: ${selector}`).not.toBeNull()
  return match?.[1] || ''
}

describe('CreationPanel constrained-window layout', () => {
  it('keeps document header actions readable without duplicating the global run controls', () => {
    const title = cssRule('.creation-document-header__title')
    const actions = cssRule('.creation-document-header__actions')
    const actionButtons = cssRule('.creation-document-header__actions > button')
    expect(title).toContain('min-width: 0')
    expect(title).toContain('white-space: nowrap')
    expect(actions).toContain('flex: 0 1 auto')
    expect(actionButtons).toContain('flex: 0 0 auto')
    expect(actionButtons).toContain('white-space: nowrap')

    const documentHeader = creationPanelSource.match(
      /<div className="creation-document-header"[\s\S]*?<div className="creation-document-skills"/,
    )?.[0] || ''
    expect(documentHeader).not.toContain('generationProgress')
    expect(documentHeader).not.toContain('elapsedSeconds')
    expect(documentHeader).not.toContain('handleStopGenerate')
  })

  it('lets long evidence and unbroken tokens wrap within constrained card and option grids', () => {
    const card = cssRule('.creation-brainstorm-card')
    expect(card).toContain('grid-template-columns: minmax(0, 1fr)')
    expect(card).toContain('overflow-wrap: anywhere')
    expect(cssRule('.creation-brainstorm-options')).toContain('grid-template-columns: minmax(0, 1fr)')
    expect(cssRule('.creation-brainstorm-options > button')).toContain('white-space: normal')
    expect(cssRule('.creation-brainstorm-options > button > span:last-child')).toContain('min-width: 0')
    expect(cssRule('.creation-brainstorm-options > button strong > span')).toContain('min-width: 0')
    expect(cssRule('.creation-brainstorm-copy-details')).toContain('overflow-wrap: anywhere')
  })

  it('stacks loading copy without inheriting the question header grid placement', () => {
    const copy = cssRule('.creation-brainstorm-card__loading-copy')
    expect(copy).toContain('display: grid')
    expect(copy).toContain('grid-template-columns: minmax(0, 1fr)')
    expect(cssRule('.creation-brainstorm-card__eyebrow')).not.toContain('grid-area:')
    expect(cssRule('.creation-brainstorm-card > header .creation-brainstorm-card__eyebrow'))
      .toContain('grid-area: eyebrow')
  })

  it('lets the brainstorm prompt span the full card below its navigation', () => {
    const header = cssRule('.creation-brainstorm-card > header')
    expect(header).toContain('display: grid')
    expect(header).toContain('grid-template-areas: "eyebrow actions" "prompt prompt"')
    expect(cssRule('.creation-brainstorm-card > header > div')).toContain('display: contents')
    expect(cssRule('.creation-brainstorm-card > header strong')).toContain('grid-area: prompt')
    expect(cssRule('.creation-brainstorm-card > header > .creation-brainstorm-card__header-actions'))
      .toContain('grid-area: actions')
    expect(creationPanelStyles).toContain('grid-template-areas: "eyebrow" "actions" "prompt"')
  })

  it('aligns recommendation badges with the first title line and prevents wrapping', () => {
    const title = cssRule('.creation-brainstorm-options > button strong')
    expect(title).toContain('align-items: baseline')
    const badge = cssRule('.creation-brainstorm-options > button strong small')
    expect(badge).toContain('flex: 0 0 auto')
    expect(badge).toContain('white-space: nowrap')
  })

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
