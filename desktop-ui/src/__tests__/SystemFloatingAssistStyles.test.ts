// @ts-expect-error Vitest runs in Node, while the desktop UI tsconfig intentionally omits Node types.
import { readFileSync } from 'node:fs'
// @ts-expect-error Vitest runs in Node, while the desktop UI tsconfig intentionally omits Node types.
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

declare const process: { cwd: () => string }

const floatingAssistStyles = readFileSync(
  resolve(process.cwd(), 'src/components/SystemFloatingAssist.css'),
  'utf8',
)

const cssSection = (startMarker: string, endMarker: string) => {
  const start = floatingAssistStyles.indexOf(startMarker)
  const end = floatingAssistStyles.indexOf(endMarker, start + startMarker.length)
  expect(start, `missing CSS section: ${startMarker}`).toBeGreaterThanOrEqual(0)
  expect(end, `missing CSS section terminator: ${endMarker}`).toBeGreaterThan(start)
  return floatingAssistStyles.slice(start, end)
}

describe('SystemFloatingAssist capturing animation', () => {
  it('keeps the task card above the mascot face while dropping and reading', () => {
    const taskCard = cssSection(
      '.system-floating-assist__task-card {',
      '.system-floating-assist__task-card::before',
    )
    const taskDrop = cssSection(
      '@keyframes floating-assist-bread-task-drop',
      '@keyframes floating-assist-bread-scan-bob',
    )
    const taskRead = cssSection(
      '@keyframes floating-assist-bread-task-read',
      '@keyframes floating-assist-bread-scan-ring',
    )

    expect(taskCard).toContain('top: 12px')
    expect(taskDrop).not.toMatch(/translate\([^;]*,\s*3[2-9]px\)/)
    expect(taskRead).not.toMatch(/translate\([^;]*,\s*3[2-9]px\)/)
  })
})
