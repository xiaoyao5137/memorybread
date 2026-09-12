import { describe, expect, it } from 'vitest'
// @ts-expect-error Vitest runs in Node; the UI tsconfig omits Node types.
import { readFileSync } from 'node:fs'

declare const process: { cwd: () => string }

const styles: string = readFileSync(`${process.cwd()}/src/components/IntegrationPanel.css`, 'utf8')

describe('IntegrationPanel section headings', () => {
  it('stacks the browser eyebrow above the title and keeps the Chinese title intact', () => {
    const headingRule = styles.match(
      /\.integration-content--browser \.integration-section-heading > div\s*\{([^}]*)\}/,
    )?.[1]
    expect(headingRule).toContain('display: grid')
    expect(headingRule).toContain('justify-items: start')

    const titleRule = styles.match(
      /\.integration-content--browser \.tutorial-title-row h2\s*\{([^}]*)\}/,
    )?.[1]
    expect(titleRule).toContain('white-space: nowrap')
  })
})
