/** Repair known generated emphasis defects without touching code or literal syntax.
 * Offset mapping keeps inline edits anchored to the original stored document.
 */
export function prepareCreationMarkdown(source: string) {
  let text = ''
  const offsets: number[] = []
  let sourceOffset = 0
  let fence = ''
  for (const line of source.match(/[^\n]*\n|[^\n]+$/g) || []) {
    let value = line
    const map = Array.from({ length: line.length }, (_, i) => sourceOffset + i)
    const replace = (pattern: RegExp, replacement: (...args: any[]) => string) => {
      const edits: Array<{ start: number; length: number; value: string }> = []
      value.replace(pattern, (...args: any[]) => {
        edits.push({ start: args[args.length - 2], length: args[0].length, value: replacement(...args) })
        return args[0]
      })
      for (const edit of edits.reverse()) {
        const start = edit.start
        // Repairs either preserve width or remove syntax after a retained prefix.
        if (edit.value.length === edit.length) {
          // Punctuation may move across **; preserve the position of visible characters.
          const old = value.slice(start, start + edit.length)
          const positions = new Map<string, number[]>()
          for (let i = 0; i < old.length; i += 1) {
            positions.set(old[i], [...(positions.get(old[i]) || []), map[start + i]])
          }
          map.splice(start, edit.length, ...edit.value.split('').map(char => positions.get(char)!.shift()!))
        } else {
          let prefix = 0
          while (prefix < edit.value.length && value[start + prefix] === edit.value[prefix]) prefix += 1
          map.splice(start + prefix, edit.length - edit.value.length)
        }
        value = value.slice(0, start) + edit.value + value.slice(start + edit.length)
      }
    }
    const marker = line.replace(/\r?\n$/, '').match(/^\s*(`{3,}|~{3,})(.*)$/)
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = ''
    } else if (marker) {
      fence = marker[1]
    } else if (!/[`\\]|\]\(|\*\*\*/.test(line)
      && !/^(?: {4}|\t)(?!\s*(?:[-+*]|\d+[.)])\s)/.test(line)) {
      replace(/^(\s*(?:[-+*]|\d+[.)])\s+)\*\*- (?=\*\*)/, (_all, prefix) => prefix)
      if (value !== line && (value.match(/\*\*/g) || []).length % 2) {
        replace(/\*\*([ \t]*(?:\r?\n)?$)/, (_all, tail) => tail)
      }
      replace(/\*\*([^*\n]+?)\*\*/gu, (all, inner, index, input) => {
        if (!/^[\p{L}\p{N}]/u.test(input.slice(index + all.length)) || /^\s|\s$/.test(inner)) return all
        const match = inner.match(/^(.*?)(\p{P}+)$/u)
        return match?.[1] ? `**${match[1]}**${match[2]}` : all
      })
    }
    text += value
    offsets.push(...map)
    sourceOffset += line.length
  }
  offsets.push(source.length)
  const restorePositions = () => (tree: any) => {
    const point = (offset: number) => {
      const original = offsets[offset] ?? source.length
      const prefix = source.slice(0, original)
      return { offset: original, line: prefix.split('\n').length, column: original - prefix.lastIndexOf('\n') }
    }
    const visit = (node: any) => {
      if (node.position) {
        node.position = { start: point(node.position.start.offset), end: point(node.position.end.offset) }
      }
      node.children?.forEach(visit)
    }
    visit(tree)
  }
  return { text, offsets, restorePositions }
}
