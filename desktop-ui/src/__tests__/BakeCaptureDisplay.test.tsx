import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import BakeCaptureTab from '../components/bake/BakeCaptureTab'
import { useAppStore } from '../store/useAppStore'
import type { BakeCaptureItem } from '../types'

const noop = vi.fn()

const baseCapture: BakeCaptureItem = {
  id: '42',
  ts: 1_720_000_000_000,
  appName: 'Safari',
  winTitle: '项目文档',
  eventType: 'app_switch',
  semanticTypeLabel: '界面片段',
  rawTypeLabel: '原始模态：AX / UI',
  axText: '界面读取到的正文',
  ocrText: '截图识别到的补充正文',
  isSensitive: false,
  piiScrubbed: false,
}

const renderCapture = (capture: BakeCaptureItem = baseCapture) => render(
  <BakeCaptureTab
    captures={[capture]}
    total={1}
    limit={20}
    offset={0}
    query=""
    app=""
    from=""
    to=""
    draftQuery=""
    draftApp=""
    draftFrom=""
    draftTo=""
    sourceCaptureId={null}
    selectedCaptureId={capture.id}
    selectedCaptureDetail={capture}
    onSelectCapture={noop}
    onPageChange={noop}
    onLimitChange={noop}
    onDraftQueryChange={noop}
    onDraftAppChange={noop}
    onDraftFromChange={noop}
    onDraftToChange={noop}
    onSearch={noop}
    onClearFilters={noop}
    onViewLinkedTimeline={noop}
    onDeleteCapture={noop}
  />,
)

describe('采集记录展示', () => {
  beforeEach(() => {
    noop.mockClear()
    useAppStore.getState().reset()
    useAppStore.setState({
      apiBaseUrl: 'http://localhost:7070',
      debugModeEnabled: false,
    })
  })

  it('普通模式将 AX 与 OCR 合并为文本信息，并隐藏内部片段类型', () => {
    renderCapture()

    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '窗口 / 页面' })).toBeInTheDocument()
    expect(screen.getByLabelText('应用')).toHaveAttribute('list', 'bake-capture-app-options')
    expect(screen.queryByRole('heading', { name: '采集记录' })).not.toBeInTheDocument()
    expect(screen.queryByText('当前显示 1 条，共 1 条')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '项目文档' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '查看采集记录 #42 详情' }))

    expect(screen.getByRole('dialog', { name: '项目文档' })).toBeInTheDocument()
    const textInformation = screen.getByText('文本信息').nextElementSibling
    expect(textInformation).toHaveTextContent('界面读取到的正文')
    expect(textInformation).toHaveTextContent('截图识别到的补充正文')
    expect(screen.getByPlaceholderText('搜索标题、正文或文本信息')).toBeInTheDocument()
    expect(screen.queryByText('AX 文本')).not.toBeInTheDocument()
    expect(screen.queryByText('OCR 文本')).not.toBeInTheDocument()
    expect(screen.queryByText('界面片段')).not.toBeInTheDocument()
    expect(screen.queryByText('截图片段')).not.toBeInTheDocument()
    expect(screen.queryByText('触发信号')).not.toBeInTheDocument()
  })

  it('新近截图在 OCR 回写前显示识别中，不误报为无文本', () => {
    renderCapture({
      ...baseCapture,
      ts: Date.now(),
      axText: null,
      ocrText: null,
      screenshotPath: 'screenshots/pending.jpg',
      summary: '项目文档',
      bestText: '项目文档',
    })

    expect(screen.getByText('正在提炼中')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看采集记录 #42 详情' }))

    expect(screen.getByText('文本识别中，完成后将自动显示…')).toBeInTheDocument()
    expect(screen.queryByText('暂无文本信息')).not.toBeInTheDocument()
  })

  it.each([
    ['app_switch', '应用切换'],
    ['browser_navigation', 'URL 变动'],
    ['mouse_click', '鼠标事件'],
    ['scroll', '滚动事件'],
    ['key_pause', '键盘事件'],
    ['auto', '定时器兜底'],
    ['manual', '手动触发'],
  ])('调试模式将 %s 展示为“%s”', (eventType, label) => {
    useAppStore.setState({ debugModeEnabled: true })
    renderCapture({ ...baseCapture, eventType })

    fireEvent.click(screen.getByRole('button', { name: '查看采集记录 #42 详情' }))

    expect(screen.getByText('触发信号')).toBeInTheDocument()
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByText('界面片段')).toBeInTheDocument()
    expect(screen.getByText('原始模态：AX / UI')).toBeInTheDocument()
    expect(screen.queryByText('原始模态：原始模态：AX / UI')).not.toBeInTheDocument()
  })

  it('仅通过操作列打开详情，并支持 Escape 收起抽屉', () => {
    renderCapture()

    expect(screen.queryByText('文本信息')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看采集记录 #42 详情' }))
    expect(screen.getByRole('dialog', { name: '项目文档' })).toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.queryByRole('dialog', { name: '项目文档' })).not.toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(noop).toHaveBeenLastCalledWith(null)
  })
})
