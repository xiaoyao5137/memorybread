import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryExportPicker } from '../components/IntegrationPanel'

const integrationMocks = vi.hoisted(() => ({
  listIntegrationMemoryOptions: vi.fn(),
  pickLocalDirectory: vi.fn(),
}))

vi.mock('../utils/integrationSkills', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/integrationSkills')>()),
  ...integrationMocks,
}))

const options = [
  { id: 11, title: 'Alpha 决策记录', category: 'decision', observedAt: 1722900000000 },
  { id: 22, title: 'Beta 方案评审', category: 'review', observedAt: 1722903600000 },
  { id: 33, title: 'Gamma 周报', category: 'report', observedAt: 1722907200000 },
]

const setRect = (element: Element, rect: Partial<DOMRect>) => {
  element.getBoundingClientRect = () => ({
    left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
    toJSON: () => ({}),
    ...rect,
  } as DOMRect)
}

const PickerHarness: React.FC<{ onSelected?: (ids: number[]) => void }> = ({ onSelected }) => {
  const [vaultPath, setVaultPath] = useState('')
  const [subfolder, setSubfolder] = useState('MemoryBread')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  return (
    <MemoryExportPicker
      apiBaseUrl="http://127.0.0.1:7070"
      vaultPath={vaultPath}
      onVaultPathChange={setVaultPath}
      subfolder={subfolder}
      onSubfolderChange={setSubfolder}
      selectedIds={selectedIds}
      onSelectedIdsChange={next => {
        setSelectedIds(next)
        onSelected?.(next)
      }}
    />
  )
}

const renderLoadedPicker = async (onSelected?: (ids: number[]) => void) => {
  const view = render(<PickerHarness onSelected={onSelected} />)
  await screen.findByText('Alpha 决策记录')
  const list = screen.getByRole('listbox')
  setRect(list, { left: 0, top: 0, width: 320, height: 300 } as Partial<DOMRect>)
  const cards = screen.getAllByRole('option')
  cards.forEach((card, index) => {
    setRect(card, { left: 0, top: index * 40, width: 300, height: 32 } as Partial<DOMRect>)
  })
  return { view, list, cards }
}

beforeEach(() => {
  integrationMocks.listIntegrationMemoryOptions.mockReset().mockResolvedValue(options)
  integrationMocks.pickLocalDirectory.mockReset().mockResolvedValue(null)
})

describe('MemoryExportPicker', () => {
  it('空白处拖拽圈选命中相交的记忆卡片', async () => {
    const onSelected = vi.fn<(ids: number[]) => void>()
    const { list } = await renderLoadedPicker(onSelected)

    fireEvent.mouseDown(list, { button: 0, clientX: 5, clientY: 5 })
    fireEvent.mouseMove(window, { clientX: 300, clientY: 60 })
    fireEvent.mouseUp(window)

    const lastCall = onSelected.mock.calls[onSelected.mock.calls.length - 1][0]
    expect(lastCall.sort((a, b) => a - b)).toEqual([11, 22])
    expect(document.querySelector('.integration-marquee')).not.toBeInTheDocument()
  })

  it('按住 Shift 拖拽圈选时保留已有选择', async () => {
    const { list } = await renderLoadedPicker()
    const cards = screen.getAllByRole('option')

    fireEvent.click(cards[0])
    expect(cards[0]).toHaveClass('is-selected')

    fireEvent.mouseDown(list, { button: 0, clientX: 5, clientY: 85, shiftKey: true })
    fireEvent.mouseMove(window, { clientX: 300, clientY: 115, shiftKey: true })
    fireEvent.mouseUp(window)

    const selected = screen.getAllByRole('option')
      .filter(card => card.className.includes('is-selected'))
      .map(card => Number(card.getAttribute('data-memory-card')))
    expect(selected.sort((a, b) => a - b)).toEqual([11, 33])
  })

  it('单击切换选择，Shift 单击做区间连选', async () => {
    await renderLoadedPicker()
    const cards = screen.getAllByRole('option')

    fireEvent.click(cards[0])
    expect(cards[0]).toHaveClass('is-selected')

    fireEvent.click(cards[0])
    expect(screen.getAllByRole('option')[0]).not.toHaveClass('is-selected')

    fireEvent.click(cards[0])
    fireEvent.click(cards[2], { shiftKey: true })
    expect(screen.getAllByRole('option').every(card => card.className.includes('is-selected'))).toBe(true)
  })

  it('全选与清空按钮作用于当前候选列表', async () => {
    await renderLoadedPicker()

    fireEvent.click(screen.getByRole('button', { name: '全选' }))
    await waitFor(() => {
      expect(screen.getAllByRole('option').every(card => card.className.includes('is-selected'))).toBe(true)
    })

    fireEvent.click(screen.getByRole('button', { name: '清空' }))
    await waitFor(() => {
      expect(screen.getAllByRole('option').every(card => !card.className.includes('is-selected'))).toBe(true)
    })
  })

  it('选择文件夹按钮调用本机目录选择器并回填路径', async () => {
    integrationMocks.pickLocalDirectory.mockResolvedValue('/tmp/memorybread-test/Vault')
    await renderLoadedPicker()

    fireEvent.click(within(screen.getByRole('listbox').parentElement as HTMLElement)
      .getByRole('button', { name: /选择文件夹/ }))

    await waitFor(() => {
      expect((screen.getByLabelText('Obsidian Vault 文件夹') as HTMLInputElement).value)
        .toBe('/tmp/memorybread-test/Vault')
    })
  })

  it('滚动到底部时按 offset 追加加载下一页记忆', async () => {
    const firstPage = Array.from({ length: 60 }, (_, index) => ({
      id: index + 1, title: `记忆 ${index + 1}`, category: 'memory', observedAt: null,
    }))
    const secondPage = [{ id: 101, title: '追加的记忆', category: 'memory', observedAt: null }]
    integrationMocks.listIntegrationMemoryOptions.mockReset()
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce(secondPage)

    render(<PickerHarness />)
    await screen.findByText('记忆 1')
    expect(screen.getByText(/已加载 60 条 · 滚动加载更多/)).toBeInTheDocument()

    fireEvent.scroll(screen.getByRole('listbox'))

    await waitFor(() => {
      expect(screen.getByText('追加的记忆')).toBeInTheDocument()
    })
    expect(screen.getByText(/已加载 61 条/)).toBeInTheDocument()
    expect(integrationMocks.listIntegrationMemoryOptions)
      .toHaveBeenLastCalledWith('http://127.0.0.1:7070', '', 60, 60)
  })
})
