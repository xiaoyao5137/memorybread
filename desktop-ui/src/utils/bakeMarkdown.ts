/** Give extracted top-level tables a block boundary, including after list labels.
 * Some historical memories omit this blank line, making the table part of the
 * preceding list paragraph. Only repair recognizable table headers, never code
 * or stored content.
 */
export function prepareBakeMarkdown(content: string): string {
  const lines = content.replace(/\r\n?/g, '\n').split('\n')
  const result: string[] = []
  let fence: { marker: string; length: number } | null = null
  const delimiter = /^\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/

  lines.forEach((line, index) => {
    const fenceMatch = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/)
    if (fence) {
      result.push(line)
      if (fenceMatch && fenceMatch[1][0] === fence.marker
        && fenceMatch[1].length >= fence.length && !fenceMatch[2].trim()) fence = null
      return
    }
    if (fenceMatch) {
      fence = { marker: fenceMatch[1][0], length: fenceMatch[1].length }
    } else if (/^\S/.test(line) && line.includes('|')
      && (lines[index + 1] || '').includes('|') && delimiter.test(lines[index + 1] || '') && result.length && result[result.length - 1].trim()) {
      result.push('')
    }
    result.push(line)
  })
  return result.join('\n')
}
