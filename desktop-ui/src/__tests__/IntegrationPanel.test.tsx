import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import IntegrationPanel from '../components/IntegrationPanel'
import { useAppStore } from '../store/useAppStore'

const skillMocks = vi.hoisted(() => ({
  importAgentSkillPackage: vi.fn(),
  importAgentSkillZip: vi.fn(),
  listLocalCreationSkills: vi.fn(),
  saveLocalCreationSkill: vi.fn(),
}))

const integrationMocks = vi.hoisted(() => ({
  listIntegrationSkills: vi.fn(),
  getIntegrationSkill: vi.fn(),
  listIntegrationSkillRuns: vi.fn(),
  getIntegrationSkillRun: vi.fn(),
  startIntegrationSkillRun: vi.fn(),
  selectedFilesToIntegrationInput: vi.fn(),
  downloadIntegrationSkillBundle: vi.fn(),
  downloadIntegrationSkillFile: vi.fn(),
  downloadIntegrationArtifact: vi.fn(),
  copyIntegrationArtifact: vi.fn(),
}))

const browserMocks = vi.hoisted(() => ({
  getBrowserIntegrationStatus: vi.fn(),
  prepareBrowserIntegration: vi.fn(),
}))

vi.mock('../utils/creationSkills', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/creationSkills')>()),
  importAgentSkillPackage: skillMocks.importAgentSkillPackage,
  importAgentSkillZip: skillMocks.importAgentSkillZip,
  listLocalCreationSkills: skillMocks.listLocalCreationSkills,
  saveLocalCreationSkill: skillMocks.saveLocalCreationSkill,
}))

vi.mock('../utils/integrationSkills', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/integrationSkills')>()),
  ...integrationMocks,
}))

vi.mock('../utils/browserIntegration', () => ({
  getBrowserIntegrationStatus: browserMocks.getBrowserIntegrationStatus,
  prepareBrowserIntegration: browserMocks.prepareBrowserIntegration,
}))

vi.mock('../components/MemoryBackupSection', () => ({
  default: () => <section aria-label="记忆备份">备份功能</section>,
}))

const catalog = [
  {
    id: 'obsidian', title: 'Obsidian', eyebrow: 'Markdown 知识库', description: '真实导入 Obsidian。',
    capability: 'Vault 预检 · 幂等增量导入', badge: '推荐', direction: 'input', executor: 'markdown_import',
    version: '1.0.0', inputKind: 'folder', accept: '.md,.markdown', supportsPreview: true, fileCount: 4,
  },
  {
    id: 'qdrant', title: 'Qdrant', eyebrow: '向量数据库导出', description: '真实导入 Qdrant。',
    capability: 'Point 映射', badge: 'JSON', direction: 'input', executor: 'record_import',
    version: '1.0.0', inputKind: 'files', accept: '.json,.jsonl', supportsPreview: true, fileCount: 4,
  },
  {
    id: 'workbuddy', title: 'WorkBuddy', eyebrow: '腾讯办公智能体', description: '生成可上传 Skill。',
    capability: 'Skill ZIP · 本机即时召回', badge: '腾讯', direction: 'output', executor: 'workbuddy_skill_export',
    version: '1.0.0', inputKind: 'none', accept: '', supportsPreview: true, fileCount: 5,
  },
  {
    id: 'qianwen-office', title: '千问办公', eyebrow: '中文办公上下文包', description: '生成中文上下文包。',
    capability: '中文材料包', badge: '办公', direction: 'output', executor: 'context_export',
    version: '1.0.0', inputKind: 'query', accept: '', supportsPreview: false, fileCount: 4,
  },
  {
    id: 'codex', title: 'Codex', eyebrow: '编码 Agent', description: '安装 Codex Skill。',
    capability: '真实安装', badge: '已内置', direction: 'output', executor: 'install_agent_skill',
    version: '1.0.0', inputKind: 'none', accept: '', supportsPreview: true, fileCount: 6,
  },
  {
    id: 'claude-code', title: 'Claude Code', eyebrow: '编码 Agent', description: '安装 Claude Skill。',
    capability: '真实安装', badge: '已内置', direction: 'output', executor: 'install_agent_skill',
    version: '1.0.0', inputKind: 'none', accept: '', supportsPreview: true, fileCount: 6,
  },
] as const

const obsidianDetail = {
  ...catalog[0],
  files: [
    { path: 'SKILL.md', mediaType: 'text/markdown', sizeBytes: 36, content: '# Obsidian 本地导入\n\n真实执行说明。' },
    { path: 'source/executor.rs', mediaType: 'text/x-rust', sizeBytes: 24, content: 'fn execute_obsidian() {}' },
  ],
}

const succeededRun = {
  id: 'integration-run-1',
  skillId: 'obsidian',
  mode: 'preview',
  status: 'succeeded',
  inputSummary: { fileCount: 1, totalBytes: 12 },
  result: { kind: 'import', mode: 'preview', parsed: 1, created: 0, updated: 0, unchanged: 0, skipped: 0 },
  logs: [
    { ts: 1, level: 'info', message: '本地执行器已启动' },
    { ts: 2, level: 'success', message: '执行完成，结果已经保存在本机' },
  ],
  createdAtMs: 1,
  startedAtMs: 1,
  finishedAtMs: 2,
}

beforeEach(() => {
  useAppStore.getState().reset()
  skillMocks.importAgentSkillPackage.mockReset()
  skillMocks.importAgentSkillZip.mockReset()
  skillMocks.listLocalCreationSkills.mockReset().mockResolvedValue([])
  skillMocks.saveLocalCreationSkill.mockReset()
  integrationMocks.listIntegrationSkills.mockReset().mockResolvedValue(catalog)
  integrationMocks.getIntegrationSkill.mockReset().mockResolvedValue(obsidianDetail)
  integrationMocks.listIntegrationSkillRuns.mockReset().mockResolvedValue([])
  integrationMocks.getIntegrationSkillRun.mockReset().mockResolvedValue(succeededRun)
  integrationMocks.startIntegrationSkillRun.mockReset().mockResolvedValue(succeededRun)
  integrationMocks.selectedFilesToIntegrationInput.mockReset().mockResolvedValue([
    { path: 'Note.md', mediaType: 'text/markdown', contentBase64: 'IyBOb3Rl', sizeBytes: 6 },
  ])
  integrationMocks.downloadIntegrationSkillBundle.mockReset().mockResolvedValue(undefined)
  integrationMocks.downloadIntegrationSkillFile.mockReset().mockResolvedValue(undefined)
  browserMocks.getBrowserIntegrationStatus.mockReset().mockResolvedValue({
    runtime: {
      schema_version: 'memorybread.browser-extension-status.v1',
      connected: false,
      extension_version: null,
      active_job_count: 0,
      queued_job_count: 0,
    },
    install: {
      supported: true,
      extensionId: 'llkmmkikjolkibaiklpkfhjdpbbohlbe',
      extensionDirectory: '/tmp/memorybread-chrome-extension',
      storeUrl: null,
      nativeHostRegistered: false,
      bridgeAvailable: true,
    },
  })
  browserMocks.prepareBrowserIntegration.mockReset().mockResolvedValue({
    supported: true,
    extensionId: 'llkmmkikjolkibaiklpkfhjdpbbohlbe',
    extensionDirectory: '/tmp/memorybread-chrome-extension',
    storeUrl: null,
    nativeHostRegistered: true,
    bridgeAvailable: true,
  })
})

describe('IntegrationPanel', () => {
  it('默认展示真正可执行的输入 Skill 与四类集成 Tab', async () => {
    render(<IntegrationPanel />)

    expect(screen.getByRole('heading', { name: '集成' })).toBeInTheDocument()
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual([
      '输入导入外部记忆',
      '输出导出上下文或安装 Skill',
      '备份与恢复备份本地记忆与恢复',
      '浏览器浏览器后台读取数据',
    ])
    expect(screen.getByRole('heading', { name: '导入记忆Skill' })).toBeInTheDocument()
    expect(screen.queryByText(/每个内置 Skill 都在本机真实执行/)).not.toBeInTheDocument()
    expect(await screen.findByText('Obsidian')).toBeInTheDocument()
    expect(screen.getByText('Qdrant')).toBeInTheDocument()
    expect(screen.getByText('记忆来源')).toBeInTheDocument()
    expect(screen.getByText('记忆导入')).toBeInTheDocument()
    expect(screen.getByText('记忆输出')).toBeInTheDocument()
    expect(screen.getAllByText('执行')).toHaveLength(2)
  })

  it('浏览器 Tab 展示 Chrome 安装状态并可启动安装', async () => {
    render(<IntegrationPanel />)
    fireEvent.click(screen.getByRole('tab', { name: /浏览器/ }))

    expect(await screen.findByRole('heading', { name: '浏览器集成' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看浏览器集成教程（在浏览器中打开）' })).toBeInTheDocument()
    expect(screen.getByText('MemoryBread Chrome 扩展程序')).toBeInTheDocument()
    expect(screen.getByText('未安装')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '在 Chrome 中加载 MemoryBread' })).toBeInTheDocument()
    expect(screen.getByText('加载未打包的扩展程序', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getByText('/tmp/memorybread-chrome-extension')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '刷新状态' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '本地安装' }))

    await waitFor(() => expect(browserMocks.prepareBrowserIntegration).toHaveBeenCalledTimes(1))
    expect(await screen.findByRole('status')).toHaveTextContent('已打开 Chrome 扩展页和 MemoryBread 扩展目录')
  })

  it('手动检查连接成功后显示明确反馈', async () => {
    browserMocks.getBrowserIntegrationStatus
      .mockResolvedValueOnce({
        runtime: {
          schema_version: 'memorybread.browser-extension-status.v1',
          connected: false,
          extension_version: null,
          active_job_count: 0,
          queued_job_count: 0,
        },
        install: {
          supported: true,
          extensionId: 'llkmmkikjolkibaiklpkfhjdpbbohlbe',
          extensionDirectory: '/tmp/memorybread-chrome-extension',
          storeUrl: null,
          nativeHostRegistered: true,
          bridgeAvailable: true,
        },
      })
      .mockResolvedValueOnce({
        runtime: {
          schema_version: 'memorybread.browser-extension-status.v1',
          connected: true,
          extension_version: '0.1.1',
          active_job_count: 0,
          queued_job_count: 0,
        },
        install: {
          supported: true,
          extensionId: 'llkmmkikjolkibaiklpkfhjdpbbohlbe',
          extensionDirectory: '/tmp/memorybread-chrome-extension',
          storeUrl: null,
          nativeHostRegistered: true,
          bridgeAvailable: true,
        },
      })

    render(<IntegrationPanel />)
    fireEvent.click(screen.getByRole('tab', { name: /浏览器/ }))
    const checkButton = await screen.findByRole('button', { name: '我已安装，检查连接' })
    fireEvent.click(checkButton)

    expect(await screen.findByRole('status')).toHaveTextContent('浏览器扩展已连接成功')
    expect(screen.getByText('已连接')).toBeInTheDocument()
    expect(screen.getByText('0.1.1')).toBeInTheDocument()
  })

  it('手动检查未连接时给出可操作提示', async () => {
    browserMocks.getBrowserIntegrationStatus.mockResolvedValue({
      runtime: {
        schema_version: 'memorybread.browser-extension-status.v1',
        connected: false,
        extension_version: null,
        active_job_count: 0,
        queued_job_count: 0,
      },
      install: {
        supported: true,
        extensionId: 'llkmmkikjolkibaiklpkfhjdpbbohlbe',
        extensionDirectory: '/tmp/memorybread-chrome-extension',
        storeUrl: null,
        nativeHostRegistered: true,
        bridgeAvailable: true,
      },
    })

    render(<IntegrationPanel />)
    fireEvent.click(screen.getByRole('tab', { name: /浏览器/ }))
    fireEvent.click(await screen.findByRole('button', { name: '我已安装，检查连接' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('尚未检测到浏览器扩展连接')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('配置商店地址时使用 Chrome 商店安装入口', async () => {
    browserMocks.getBrowserIntegrationStatus.mockResolvedValue({
      runtime: {
        schema_version: 'memorybread.browser-extension-status.v1',
        connected: false,
        extension_version: null,
        active_job_count: 0,
        queued_job_count: 0,
      },
      install: {
        supported: true,
        extensionId: 'llkmmkikjolkibaiklpkfhjdpbbohlbe',
        extensionDirectory: null,
        storeUrl: 'https://chromewebstore.google.com/detail/memorybread/example',
        nativeHostRegistered: false,
        bridgeAvailable: true,
      },
    })

    render(<IntegrationPanel />)
    fireEvent.click(screen.getByRole('tab', { name: /浏览器/ }))

    expect(await screen.findByRole('button', { name: '前往 Chrome 商店' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '在 Chrome 中加载 MemoryBread' })).not.toBeInTheDocument()
  })

  it('输出 Tab 展示上下文包与可安装编码 Agent Skill', async () => {
    render(<IntegrationPanel />)
    await screen.findByText('Obsidian')
    fireEvent.click(screen.getByRole('tab', { name: /输出/ }))

    expect(screen.getByText('WorkBuddy')).toBeInTheDocument()
    expect(screen.getByText('千问办公')).toBeInTheDocument()
    expect(screen.getByText('Codex')).toBeInTheDocument()
    expect(screen.getByText('Claude Code')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '输出记忆到外部工具' })).toBeInTheDocument()
  })

  it('备份与恢复 Tab 承载原记忆备份组件', async () => {
    render(<IntegrationPanel />)
    await screen.findByText('Obsidian')
    fireEvent.click(screen.getByRole('tab', { name: /备份与恢复/ }))

    expect(screen.getByRole('heading', { name: '备份与恢复' })).toBeInTheDocument()
    expect(screen.getByLabelText('记忆备份')).toBeInTheDocument()
    expect(screen.queryByText(/管理本地记忆包/)).not.toBeInTheDocument()
  })

  it('可以查看内置 Skill 描述与真实执行源码', async () => {
    render(<IntegrationPanel />)
    const title = await screen.findByText('Obsidian')
    const card = title.closest('article') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /文件/ }))

    expect(await screen.findByText('# Obsidian 本地导入', { exact: false })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /source\/executor.rs/ }))
    expect(screen.getByText('fn execute_obsidian() {}')).toBeInTheDocument()
  })

  it('选择 Vault 后可启动预检并查看结果与日志', async () => {
    render(<IntegrationPanel />)
    const title = await screen.findByText('Obsidian')
    fireEvent.click(within(title.closest('article') as HTMLElement).getByRole('button', { name: '执行' }))
    await screen.findByText('选择本地仓库文件夹')
    const file = new File(['# Note'], 'Note.md', { type: 'text/markdown' })
    fireEvent.change(screen.getByLabelText('选择 Skill 输入文件'), { target: { files: [file] } })
    await screen.findByText('已读取 1 个文件')
    fireEvent.click(screen.getByRole('button', { name: '预检' }))

    await waitFor(() => {
      expect(integrationMocks.startIntegrationSkillRun).toHaveBeenCalledWith(
        'http://127.0.0.1:7070',
        'obsidian',
        expect.objectContaining({ mode: 'preview', files: expect.arrayContaining([expect.objectContaining({ path: 'Note.md' })]) }),
      )
    })
    expect(await screen.findByText('执行完成，结果已经保存在本机')).toBeInTheDocument()
    expect(screen.getByText('预检结果')).toBeInTheDocument()
  })

  it('在清空文件输入前快照 WebView 的动态 FileList', async () => {
    render(<IntegrationPanel />)
    const title = await screen.findByText('Obsidian')
    fireEvent.click(within(title.closest('article') as HTMLElement).getByRole('button', { name: '执行' }))
    await screen.findByText('选择本地仓库文件夹')

    const input = screen.getByLabelText('选择 Skill 输入文件') as HTMLInputElement
    const file = new File(['# Note'], 'Note.md', { type: 'text/markdown' })
    let cleared = false
    const liveFiles = {
      0: file,
      get length() { return cleared ? 0 : 1 },
      item: (index: number) => index === 0 && !cleared ? file : null,
    } as unknown as FileList
    Object.defineProperty(input, 'files', { configurable: true, get: () => liveFiles })
    Object.defineProperty(input, 'value', {
      configurable: true,
      get: () => '',
      set: () => { cleared = true },
    })

    fireEvent.change(input)

    await waitFor(() => expect(integrationMocks.selectedFilesToIntegrationInput).toHaveBeenCalled())
    expect(integrationMocks.selectedFilesToIntegrationInput.mock.calls[0][0]).toEqual([file])
  })

  it('上传自定义输入 Skill 后按集成方向保存并展示', async () => {
    const imported = {
      clientSkillKey: 'imported-my-input-skill', sourceKind: 'imported', sourceId: 'my-input-skill',
      title: 'my-input-skill', summary: '导入团队自己的数据。', categoryId: null,
      skillDescription: { purpose: '', documentTypes: [], problems: [], domains: [], deliverables: [] },
      executionSteps: [], commonTitles: [], titleStyle: '', textStyle: '', diagramStyle: '', writingGuidelines: [],
      sectionHeadings: { commonTitles: '', titleStyle: '', textStyle: '', diagramStyle: '', writingGuidelines: '' },
      fieldExamples: { commonTitles: [], titleStyle: [], textStyle: [], diagramStyle: [], writingGuidelines: [] },
      exampleDocument: '', status: 'saved', installed: true, published: false,
      packageFiles: [{ path: 'SKILL.md', mediaType: 'text/markdown', contentBase64: 'LS0t', sizeBytes: 3 }],
    }
    const saved = { ...imported, id: 12, categoryId: 'integration-input', createdAt: 1, updatedAt: 1 }
    skillMocks.importAgentSkillPackage.mockResolvedValue(imported)
    skillMocks.saveLocalCreationSkill.mockResolvedValue(saved)

    render(<IntegrationPanel />)
    await screen.findByText('Obsidian')
    const file = new File(['---\nname: my-input-skill\ndescription: test\n---'], 'SKILL.md', { type: 'text/markdown' })
    fireEvent.change(screen.getByLabelText('选择包含 SKILL.md 的技能文件夹'), { target: { files: [file] } })

    await waitFor(() => expect(skillMocks.saveLocalCreationSkill).toHaveBeenCalledWith(
      'http://127.0.0.1:7070', expect.objectContaining({ categoryId: 'integration-input' }),
    ))
    expect(await screen.findByText('导入团队自己的数据。')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('my-input-skill 已保存为自定义输入 Skill')
  })

  it('执行结果中的记忆标题可进入对应时间线明细', async () => {
    integrationMocks.listIntegrationSkillRuns.mockResolvedValue([{
      ...succeededRun,
      mode: 'execute',
      result: {
        ...succeededRun.result,
        mode: 'execute',
        created: 1,
        records: [{ id: 42, title: 'Imported decision', path: 'Decisions/Import.md', outcome: 'created' }],
      },
    }])

    render(<IntegrationPanel />)
    const title = await screen.findByText('Obsidian')
    fireEvent.click(within(title.closest('article') as HTMLElement).getByRole('button', { name: '执行' }))
    const memoryLink = await screen.findByRole('button', { name: /Imported decision/ })
    fireEvent.click(memoryLink)

    expect(useAppStore.getState()).toMatchObject({
      windowMode: 'knowledge',
      repositoryTab: 'memory',
      repositoryMemoryFocusId: '42',
      selectedMemoryId: '42',
    })
  })
})
