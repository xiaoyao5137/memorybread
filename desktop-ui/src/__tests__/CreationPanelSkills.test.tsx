import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CreationPanel from '../components/CreationPanel'
import { useAppStore } from '../store/useAppStore'

const rawSkill = {
  id: 2,
  client_skill_key: 'skill-cross-team-tech-meeting',
  cloud_skill_id: null,
  source_kind: 'bake_document',
  source_id: '171',
  title: '跨部门技术沟通会文档',
  summary: '适合架构师、软件工程师和产品经理在跨部门技术沟通、阶段复盘与规划场景中使用。',
  category_id: '01900000-0000-7000-8000-000000011401',
  common_titles: ['跨部门技术沟通会', '技术协作复盘会'],
  title_style: '用会议目标概括标题，不出现具体部门名称。',
  text_style: '结论先行，明确技术取舍和行动项。',
  diagram_style: '用简洁流程图说明跨团队依赖。',
  structure_pattern: ['会议目标', '进展与指标', '技术取舍', '行动项'],
  writing_guidelines: ['每项行动明确负责人和期限。'],
  status: 'saved',
  installed: false,
  published: false,
  created_at: 1_720_000_000_000,
  updated_at: 1_720_000_000_000,
}

describe('技能安装与使用', () => {
  beforeEach(() => {
    useAppStore.getState().reset()
    useAppStore.getState().setApiBaseUrl('http://localhost:7070')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('安装后可通过 @ 选择，并把完整 Skill 配方注入生成请求', async () => {
    let installed = false
    let generationPayload: any = null
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([{ ...rawSkill, installed }])
      }
      if (url.pathname === '/api/creation/skills/2' && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body || '{}'))
        installed = Boolean(body.installed)
        return Response.json({ ...rawSkill, ...body, installed, updated_at: rawSkill.updated_at + 1 })
      }
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/references') {
        return Response.json({ requirement: {}, references: [] })
      }
      if (url.pathname === '/api/creation/generate') {
        generationPayload = JSON.parse(String(init?.body || '{}'))
        return new Response('data: {"content":"# 已生成文档"}\n\ndata: {"done":true}\n\n', {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 88 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '技能 (1)' }))
    fireEvent.click(screen.getByRole('button', { name: '安装' }))
    await screen.findByRole('button', { name: '卸载' })

    fireEvent.click(screen.getByRole('button', { name: '创作' }))
    expect(screen.getByRole('region', { name: '生成内容' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '创作对话' })).toBeInTheDocument()
    const textarea = screen.getByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(textarea, { target: { value: '@' } })

    const picker = await screen.findByRole('listbox', { name: '选择技能' })
    expect(within(picker).getByText('选择已安装的技能')).toBeInTheDocument()
    fireEvent.click(within(picker).getByRole('option', { name: /跨部门技术沟通会文档/ }))

    expect(textarea).toHaveValue('@跨部门技术沟通会文档 ')
    const promptField = textarea.closest('.mention-highlight-field')
    expect(promptField?.querySelector('.mention-highlight-field__mention')).toHaveTextContent('@跨部门技术沟通会文档')
    const matched = screen.getByLabelText('本次使用的技能')
    expect(within(matched).getByText('@ 已选择')).toBeInTheDocument()

    // 选中技能后继续输入正文，选择器不应重新弹出
    fireEvent.change(textarea, { target: { value: '@跨部门技术沟通会文档 是' } })
    expect(screen.queryByRole('listbox', { name: '选择技能' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))
    await waitFor(() => expect(generationPayload).not.toBeNull())

    expect(generationPayload.user_prompt).toContain('已安装并匹配的技能')
    expect(generationPayload.user_prompt).toContain('S#1 跨部门技术沟通会文档（用户明确选择）')
    expect(generationPayload.user_prompt).toContain(`适用场景与目标：${rawSkill.summary}`)
    expect(generationPayload.user_prompt).toContain('互联网 / 电商零售')
    expect(generationPayload.user_prompt).toContain('Agent Skills 目录内容')
    expect(generationPayload.user_prompt).toContain('references/memorybread-creation.json')
    expect(generationPayload.user_prompt).toContain('子标题优先使用“对象或章节角色＋技术方案动作”的短名词结构')
    expect(generationPayload.user_prompt).toContain('源记录没有保留图示证据，默认不生成图片')
    expect(generationPayload.user_prompt).not.toContain(rawSkill.title_style)
    expect(generationPayload.user_prompt).not.toContain(rawSkill.diagram_style)
    expect(generationPayload.user_prompt).not.toContain('[技能文件：references/example.md]')
    expect(generationPayload.user_prompt).toContain('execution_steps 是唯一的执行流程和一级章节白名单')
  })

  it('输入内容不再自动推荐技能，未 @ 时生成请求不注入技能', async () => {
    let generationPayload: any = null
    const installedSkill = { ...rawSkill, installed: true }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([installedSkill])
      }
      if (url.pathname === '/api/creation/history' && (!init?.method || init.method === 'GET')) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      if (url.pathname === '/api/creation/references') {
        return Response.json({ requirement: {}, references: [] })
      }
      if (url.pathname === '/api/creation/generate') {
        generationPayload = JSON.parse(String(init?.body || '{}'))
        return new Response('data: {"content":"# 已生成文档"}\n\ndata: {"done":true}\n\n', {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (url.pathname === '/api/creation/history' && init?.method === 'POST') {
        return Response.json({ id: 89 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    const textarea = await screen.findByPlaceholderText(/输入 @ 可选择已安装的技能/)
    // 输入内容与技能标题高度重合，但没有显式 @，不应出现任何自动推荐。
    fireEvent.change(textarea, { target: { value: '请生成跨部门技术沟通会文档' } })

    expect(screen.queryByLabelText('本次使用的技能')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开始创作' }))
    await waitFor(() => expect(generationPayload).not.toBeNull())
    expect(generationPayload.user_prompt).not.toContain('已安装并匹配的技能')
    expect(generationPayload.user_prompt).not.toContain(`S#1 ${rawSkill.title}`)
    expect(generationPayload.user_prompt).not.toContain(rawSkill.summary)
  })

  it('打开 @ 技能选择框后支持键盘选择，并可点击外部关闭', async () => {
    const secondSkill = {
      ...rawSkill,
      id: 3,
      client_skill_key: 'skill-weekly-report',
      title: '项目周报',
      summary: '整理项目进展、风险和下周计划。',
      installed: true,
    }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') {
        return Response.json([{ ...rawSkill, installed: true }, secondSkill])
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    const textarea = await screen.findByPlaceholderText(/输入 @ 可选择已安装的技能/)
    fireEvent.change(textarea, { target: { value: '@' } })
    let picker = await screen.findByRole('listbox', { name: '选择技能' })
    const options = within(picker).getAllByRole('option')
    expect(options[0]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(textarea, { key: 'ArrowUp' })
    expect(options[1]).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(textarea, { key: 'ArrowDown' })
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(textarea, { key: 'ArrowDown' })
    expect(options[1]).toHaveAttribute('aria-selected', 'true')
    expect(textarea).toHaveAttribute('aria-activedescendant', options[1].id)
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(textarea).toHaveValue('@项目周报 ')
    expect(screen.queryByRole('listbox', { name: '选择技能' })).not.toBeInTheDocument()

    fireEvent.change(textarea, { target: { value: '@' } })
    picker = await screen.findByRole('listbox', { name: '选择技能' })
    fireEvent.mouseDown(screen.getByRole('main'))
    expect(picker).not.toBeInTheDocument()
    expect(textarea).toHaveValue('@')
  })

  it('从 Skill 列表把当前版本发布到创作市场', async () => {
    const user = {
      id: '018f0000-0000-7000-8000-000000000008',
      username: '发布测试用户',
      status: 'active',
      roles: ['user'],
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      created_at: new Date().toISOString(),
    }
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_publish_token',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
      user,
    })
    let marketPayload: any = null
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([rawSkill])
      }
      if (url.pathname === '/api/creation/skills/2' && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body || '{}'))
        return Response.json({ ...rawSkill, ...body, updated_at: rawSkill.updated_at + 1 })
      }
      if (url.pathname === '/v1/creation-skills' && init?.method === 'POST') {
        marketPayload = JSON.parse(String(init.body || '{}'))
        return Response.json({ data: { id: 'cloud-skill-2', published: true } }, { status: 201 })
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '技能 (1)' }))
    fireEvent.click(screen.getByRole('button', { name: '发布' }))

    await screen.findByText('已发布')
    expect(marketPayload).toMatchObject({
      client_skill_key: rawSkill.client_skill_key,
      category_id: rawSkill.category_id,
      published: true,
    })
    expect(marketPayload).not.toHaveProperty('source_id')
    expect(marketPayload).not.toHaveProperty('source_kind')
  })

  it('已发布 Skill 显示取消发布并同步本地状态', async () => {
    const user = {
      id: '018f0000-0000-7000-8000-000000000008',
      username: '发布测试用户',
      status: 'active',
      roles: ['user'],
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      created_at: new Date().toISOString(),
    }
    useAppStore.getState().setAuthSession({
      access_token: 'mbs_publish_token',
      expires_at: new Date(Date.now() + 86_400_000).toISOString(),
      user,
    })
    let marketPayload: any = null
    const publishedSkill = { ...rawSkill, cloud_skill_id: 'cloud-skill-2', published: true }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([publishedSkill])
      }
      if (url.pathname === '/api/creation/skills/2' && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body || '{}'))
        return Response.json({ ...publishedSkill, ...body, updated_at: rawSkill.updated_at + 1 })
      }
      if (url.pathname === '/v1/creation-skills/cloud-skill-2' && init?.method === 'PUT') {
        marketPayload = JSON.parse(String(init.body || '{}'))
        return Response.json({ data: { id: 'cloud-skill-2', published: false } })
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '技能 (1)' }))
    fireEvent.click(screen.getByRole('button', { name: '取消发布' }))

    await screen.findByRole('button', { name: '发布' })
    expect(screen.queryByText('已保存')).not.toBeInTheDocument()
    expect(marketPayload).toMatchObject({ published: false })
  })

  it('在客户端搜索市场、查看详情、下载文件并安装技能', async () => {
    const nativeUrl = URL
    const createObjectUrl = vi.fn(() => 'blob:skill-file')
    const revokeObjectUrl = vi.fn()
    class DownloadUrl extends nativeUrl {
      static createObjectURL = createObjectUrl
      static revokeObjectURL = revokeObjectUrl
    }
    vi.stubGlobal('URL', DownloadUrl)
    const downloadClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const marketSkill = {
      id: '01900000-0000-7000-8000-000000000021',
      title: '通用架构评审文档',
      summary: '适合架构师组织方案评审并沉淀关键取舍。',
      category_id: rawSkill.category_id,
      category_path: [
        { id: '1', key: 'internet', name: '互联网', level: 1 },
        { id: rawSkill.category_id, key: 'architecture', name: '技术架构设计文档', level: 4 },
      ],
      content: {
        common_titles: rawSkill.common_titles,
        title_style: rawSkill.title_style,
        text_style: rawSkill.text_style,
        diagram_style: rawSkill.diagram_style,
        structure_pattern: rawSkill.structure_pattern,
        writing_guidelines: rawSkill.writing_guidelines,
        section_headings: {
          common_titles: '标题设计风格',
          title_style: '标题设计风格',
          text_style: '行文设计思路',
          diagram_style: '图片生成方式',
          structure_pattern: '章节组织骨架',
          writing_guidelines: '话术表达风格',
        },
        field_examples: {
          common_titles: ['通用架构评审方案'],
          title_style: ['架构评审方案：明确范围与约束'],
          text_style: ['先说明约束，再给出方案与验证方式。'],
          diagram_style: ['用分层图标注边界和依赖。'],
          structure_pattern: ['背景与目标 → 总体方案 → 风险与验证'],
          writing_guidelines: ['每个结论都补充验证方式。'],
        },
        example_document: '# 通用架构评审方案\n\n## 摘要\n\n本示例说明如何组织一次通用架构评审。\n\n## 背景与目标\n\n明确范围与约束。\n\n## 总体方案\n\n说明系统边界和关键取舍。\n\n## 风险与验证\n\n给出风险及验证方式。\n\n## 结论\n\n形成可复用的评审结论。',
      },
      author: { id: 'author-1', nickname: '面包师小麦' },
      is_official: true,
      package_name: 'architecture-review',
      package_file_count: 1,
      package_size_bytes: 140,
      package_sha256: 'a'.repeat(64),
      published: true,
      published_at: '2026-07-23T08:00:00Z',
      updated_at: '2026-07-23T08:00:00Z',
    }
    let marketQuery = ''
    let marketCategoryId = ''
    let localSkills: any[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json(localSkills)
      }
      if (url.pathname === '/api/creation/skills' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body || '{}'))
        localSkills = [{
          id: 7,
          ...body,
          created_at: rawSkill.created_at,
          updated_at: rawSkill.updated_at,
        }]
        return Response.json(localSkills[0], { status: 201 })
      }
      if (url.pathname === '/api/creation/skills/7' && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body || '{}'))
        localSkills = [{
          id: 7,
          ...body,
          created_at: rawSkill.created_at,
          updated_at: rawSkill.updated_at,
        }]
        return Response.json(localSkills[0])
      }
      if (url.pathname === '/v1/creation-skill-categories') {
        return Response.json({
          data: [
            { id: rawSkill.category_id, key: 'architecture', name: '技术架构设计文档', level: 4, parent_id: 'role-1', sort_order: 10 },
            { id: 'industry-1', key: 'internet', name: '互联网', level: 1, parent_id: null, sort_order: 10 },
            { id: 'role-1', key: 'architect', name: '架构师', level: 3, parent_id: 'segment-1', sort_order: 10 },
            { id: 'segment-1', key: 'ecommerce', name: '电商零售', level: 2, parent_id: 'industry-1', sort_order: 10 },
          ],
        })
      }
      if (url.pathname === '/v1/creation-skills') {
        marketQuery = url.searchParams.get('q') || ''
        marketCategoryId = url.searchParams.get('category_id') || ''
        return Response.json({
          data: {
            items: marketQuery && marketQuery !== '架构' ? [] : [marketSkill],
            total: 1,
            limit: 18,
            offset: 0,
          },
        })
      }
      if (url.pathname === `/v1/creation-skills/${marketSkill.id}`) {
        const markdown = '---\nname: architecture-review\ndescription: Review architecture decisions and produce an actionable document.\n---\n\n# Workflow\n'
        return Response.json({
          data: {
            ...marketSkill,
            package_files: [{
              path: 'SKILL.md',
              media_type: 'text/markdown',
              content_base64: btoa(markdown),
              size_bytes: new TextEncoder().encode(markdown).byteLength,
            }],
          },
        })
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '技能' }))
    fireEvent.click(screen.getByRole('tab', { name: '技能市场' }))
    await screen.findByText('通用架构评审文档')

    fireEvent.change(screen.getByLabelText('搜索市场技能'), { target: { value: '架构' } })
    const categoryCombobox = screen.getByRole('combobox', { name: '技能类目' })
    fireEvent.focus(categoryCombobox)
    fireEvent.change(categoryCombobox, { target: { value: '互电零' } })
    fireEvent.click(screen.getByRole('option', { name: /^电商零售/ }))
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))
    await waitFor(() => {
      expect(marketQuery).toBe('架构')
      expect(marketCategoryId).toBe('segment-1')
    })

    const marketCard = screen.getByText('通用架构评审文档').closest('article')!
    fireEvent.click(within(marketCard).getByRole('button', { name: '查看详情' }))
    const detail = await screen.findByRole('dialog', { name: '通用架构评审文档' })
    expect(within(detail).getByText('面包师小麦')).toBeInTheDocument()
    expect(await within(detail).findByTitle('SKILL.md')).toBeInTheDocument()
    fireEvent.click(within(detail).getByRole('button', { name: '下载 SKILL.md' }))
    expect(createObjectUrl).toHaveBeenCalledOnce()
    expect(downloadClick).toHaveBeenCalledOnce()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:skill-file')
    fireEvent.click(within(detail).getByRole('button', { name: '安装技能' }))

    await waitFor(() => expect(localSkills[0]).toMatchObject({
      source_kind: 'market',
      source_id: marketSkill.id,
      installed: true,
      published: false,
    }))
    expect(within(detail).getByText('已安装')).toBeInTheDocument()
    fireEvent.click(within(detail).getByRole('button', { name: '卸载技能' }))
    await waitFor(() => expect(localSkills[0]).toMatchObject({ installed: false }))
  })

  it('技能库头部提供手工新建技能入口', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([rawSkill])
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /技能/ }))
    fireEvent.click(screen.getByRole('button', { name: '新建技能' }))

    const editor = await screen.findByRole('dialog', { name: '新建技能' })
    expect(within(editor).getByText(/手工新建从空白开始/)).toBeInTheDocument()
  })

  it('用一个上传入口同时提供文件夹和 ZIP 选择', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([rawSkill])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /技能/ }))
    const folderInput = screen.getByLabelText('选择 Skill 源文件夹') as HTMLInputElement
    const zipInput = screen.getByLabelText('选择 Skill 源文件 ZIP') as HTMLInputElement
    const folderClick = vi.spyOn(folderInput, 'click')
    const zipClick = vi.spyOn(zipInput, 'click')

    expect(folderInput).toHaveAttribute('webkitdirectory')
    expect(zipInput).toHaveAttribute('accept', '.zip,application/zip')

    fireEvent.click(screen.getByRole('button', { name: '上传' }))
    const folderOption = screen.getByRole('menuitem', { name: /选择文件夹/ })
    const zipOption = screen.getByRole('menuitem', { name: /选择 ZIP 文件/ })
    expect(folderOption).toBeInTheDocument()
    expect(zipOption).toBeInTheDocument()
    fireEvent.click(folderOption)
    expect(folderClick).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByRole('button', { name: '上传' }))
    fireEvent.click(screen.getByRole('menuitem', { name: /选择 ZIP 文件/ }))
    expect(zipClick).toHaveBeenCalledOnce()
  })

  it('源文件按钮打开独立文件浏览器并提供单文件与完整下载', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills') return Response.json([rawSkill])
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /技能/ }))
    const card = screen.getByText(rawSkill.title).closest('article')!
    fireEvent.click(within(card).getByRole('button', { name: '源文件' }))

    const sourceDialog = await screen.findByRole('dialog', { name: rawSkill.title })
    expect(within(sourceDialog).getByText('Skill 源文件')).toBeInTheDocument()
    expect(within(sourceDialog).getByTitle('SKILL.md')).toBeInTheDocument()
    expect(within(sourceDialog).getByRole('button', { name: '下载文件' })).toBeInTheDocument()
    expect(within(sourceDialog).getByRole('button', { name: '下载全部源文件 (.zip)' })).toBeInTheDocument()
    expect(sourceDialog).toHaveTextContent('name:')
    expect(sourceDialog).not.toHaveTextContent('标题写法')
  })

  it('我的技能卡片省略无意义的已保存标签，并把安装状态放在标题同行', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([{ ...rawSkill, source_kind: 'manual', source_id: 'manual-2' }])
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /技能/ }))
    const card = screen.getByText(rawSkill.title).closest('article')!
    expect(within(card).queryByText('手工新建')).not.toBeInTheDocument()
    expect(within(card).queryByText('已保存')).not.toBeInTheDocument()
    const titleRow = within(card).getByText(rawSkill.title).closest('.creation-skill-library__title-row')
    expect(titleRow).not.toBeNull()
    expect(within(titleRow as HTMLElement).getByText('未安装')).toBeInTheDocument()
  })

  it('删除技能前要求二次确认', async () => {
    let deleteCount = 0
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/creation/skills' && (!init?.method || init.method === 'GET')) {
        return Response.json([rawSkill])
      }
      if (url.pathname === '/api/creation/skills/2' && init?.method === 'DELETE') {
        deleteCount += 1
        return new Response(null, { status: 204 })
      }
      if (url.pathname === '/api/creation/history') {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0 })
      }
      return new Response('{}', { status: 404 })
    }))

    render(<CreationPanel />)

    fireEvent.click(await screen.findByRole('button', { name: '技能 (1)' }))
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    const firstDialog = screen.getByRole('alertdialog', { name: '确认删除技能？' })
    expect(deleteCount).toBe(0)
    fireEvent.click(within(firstDialog).getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    const secondDialog = screen.getByRole('alertdialog', { name: '确认删除技能？' })
    fireEvent.click(within(secondDialog).getByRole('button', { name: '确认删除' }))

    await waitFor(() => expect(deleteCount).toBe(1))
    expect(await screen.findByText('还没有技能')).toBeInTheDocument()
    // 头部与空状态区域各有一个新建技能入口
    expect(screen.getAllByRole('button', { name: '新建技能' }).length).toBeGreaterThanOrEqual(2)
  })
})
