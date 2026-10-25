import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getActiveCreationModelPayload } from '../hooks/useApi'
import UserModelsPanel from '../components/UserModelsPanel'
import { normalizeCreationModels, useAppStore } from '../store/useAppStore'
import { getEffectiveCreationModelId } from '../utils/modelSelection'
const profile = { id: 'user-test', name: 'My Kimi', protocol: 'openai', model: 'new-version', base_url: 'https://api.example.com/v1', command: '', client: '', has_api_key: true }
let profiles: typeof profile[]
let saved: Record<string, unknown> | undefined
beforeEach(() => {
  localStorage.clear(); useAppStore.getState().reset(); profiles = []; saved = undefined
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/detect/')) { const client = url.split('/').pop(); return Response.json({ command: client === 'codex' ? '/detected/codex' : '/detected/claude', model: `${client}-detected-model`, args: ['--print'], message: '已探测到本地客户端，配置可自行修改。' }) }
    if (url.includes('/model-catalog')) return Response.json({ data: { schema_version: 1, models: [{ ...profile, id: 'template', capabilities: ['text','reasoning'] }], next_offset: null } })
    if (url.endsWith('/creation/user-models') && init?.method === 'POST') { saved = JSON.parse(String(init.body)); profiles = [profile]; return Response.json(profile) }
    if (url.endsWith('/creation/user-models')) return Response.json({ models: profiles })
    if (init?.method === 'DELETE') { profiles = []; return Response.json({ status: 'ok' }) }
    if (url.includes('/preferences/')) return Response.json({})
    throw new Error('Unexpected request: ' + url)
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('opens experience in a modal, streams replies, and removes the verify action', async () => {
  profiles = [profile]
  const original = vi.mocked(fetch).getMockImplementation()!
  let submitted: Record<string, unknown> | undefined
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith('/creation/chat')) {
      submitted = JSON.parse(String(init?.body))
      return new Response('data: {"content":"你好"}\n\ndata: {"content":"，测试成功"}\n\n')
    }
    return original(url, init)
  })
  render(<UserModelsPanel scope="analysis" />)
  const trigger = await screen.findByRole('button', { name: '体验 My Kimi' })
  expect(screen.queryByRole('button', { name: '验证 My Kimi' })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('体验消息')).not.toBeInTheDocument()
  trigger.focus()
  fireEvent.click(trigger)
  const dialog = screen.getByRole('dialog', { name: '体验模型 · My Kimi' })
  expect(dialog).toContainElement(screen.getByLabelText('体验消息'))
  expect(screen.getByLabelText('体验消息')).toHaveFocus()
  fireEvent.change(screen.getByLabelText('体验消息'), { target: { value: '帮我测试' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  await screen.findByText('你好，测试成功')
  expect(submitted).toMatchObject({ model: 'user-test', messages: [{ role: 'user', content: '帮我测试' }] })
  await waitFor(() => expect(screen.getByRole('button', { name: '关闭' })).not.toBeDisabled())
  fireEvent.keyDown(dialog, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
})

it('keeps experience errors inside the modal and allows retry', async () => {
  profiles = [profile]
  const original = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => String(url).endsWith('/creation/chat')
    ? new Response('data: {"error":"客户端未登录"}\n\n') : original(url, init))
  render(<UserModelsPanel scope="analysis" />)
  fireEvent.click(await screen.findByRole('button', { name: '体验 My Kimi' }))
  const dialog = screen.getByRole('dialog')
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  expect(dialog).toContainElement(await screen.findByRole('alert'))
  expect(screen.getByRole('alert')).toHaveTextContent('客户端未登录')
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled())
  fireEvent.click(screen.getByRole('button', { name: '关闭模型体验' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
it('restores user model selection across normalization without credit', () => {
  const models = normalizeCreationModels([{ id: 'user-test', name: 'Own model', apiKey: 'device-profile', enabled: true }])
  expect(getEffectiveCreationModelId(models, false)).toBe('user-test')
  expect(models.filter(m => m.enabled)).toHaveLength(1)
  expect(getActiveCreationModelPayload(models)).toEqual({ creation_model: 'user-test', creation_api_key: 'device-profile' })
  useAppStore.getState().setCreationModelConfigs(models)
  useAppStore.getState().setCreationModelConfig('mbcd-std-v1', { enabled: true })
  expect(getEffectiveCreationModelId(useAppStore.getState().creationModelConfigs, false)).toBe('mbcd-std-v1')
})
it('adds a catalog model, keeps secrets out of preferences, selects and deletes it', async () => {
  render(<UserModelsPanel />)
  fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
  fireEvent.click(await screen.findByRole('button', { name: '运营模型目录' }))
  fireEvent.click(screen.getByRole('option', { name: /My Kimi/ }))
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'my-private-key' } })
  fireEvent.click(screen.getByText('保存模型'))
  await screen.findByRole('button', { name: '编辑 My Kimi' })
  expect(saved?.api_key).toBe('my-private-key')
  expect(JSON.stringify(useAppStore.getState().creationModelConfigs)).not.toContain('my-private-key')
  fireEvent.click(screen.getByRole('button', { name: '启用 My Kimi' }))
  await waitFor(() => expect(getEffectiveCreationModelId(useAppStore.getState().creationModelConfigs, false)).toBe('user-test'))
  await waitFor(() => expect(screen.getByRole('button', { name: '删除 My Kimi' })).not.toBeDisabled())
  fireEvent.click(screen.getByRole('button', { name: '删除 My Kimi' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '删除 My Kimi' })).not.toBeInTheDocument())
  expect(getEffectiveCreationModelId(useAppStore.getState().creationModelConfigs, false)).toBe('mbcd-std-v1')
})
it('retains cached templates and manual configuration when catalog is offline', async () => {
  vi.mocked(fetch).mockImplementation(async (url) => {
    if (String(url).includes('/model-catalog')) throw new TypeError('offline')
    return Response.json({ models: [] })
  })
  render(<UserModelsPanel />)
  expect(screen.queryByText(/推荐模型暂时无法更新/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('添加模型'))
  expect(screen.getByLabelText('实际模型 ID')).toBeInTheDocument()
})
it('selects collection analysis independently of creation without duplicating local model controls', async () => {
  profiles = [profile]
  useAppStore.getState().setCreationModelConfigs([{ id: 'mbcd-std-v1', apiKey: '', enabled: true }])
  const original = vi.mocked(fetch).getMockImplementation()!
  const selected: Array<string | null> = []
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith('/analysis-selection')) {
      const { model_id } = JSON.parse(String(init?.body)); selected.push(model_id)
      return Response.json({ model_id })
    }
    return original(url, init)
  })
  const changed = vi.fn()
  render(<UserModelsPanel scope="analysis" onAnalysisChange={changed} />)
  fireEvent.click(await screen.findByRole('button', { name: '启用 My Kimi' }))
  await waitFor(() => expect(selected).toEqual(['user-test']))
  expect(getEffectiveCreationModelId(useAppStore.getState().creationModelConfigs, false)).toBe('mbcd-std-v1')
  expect(changed).toHaveBeenLastCalledWith('My Kimi')
  expect(screen.queryByRole('radio', { name: '使用本地 MBEM v1.0' })).not.toBeInTheDocument()
})

it('keeps service errors actionable while removing hints and the duplicate refresh button', async () => {
  vi.mocked(fetch).mockImplementation(async () => Response.json({ detail: 'Not Found' }, { status: 404 }))
  const { rerender } = render(<UserModelsPanel scope="analysis" refreshVersion={0} />)
  await screen.findByText('本机模型服务缺少此功能，请更新或重启应用后重试。')
  expect(screen.queryByText(/推荐模型服务尚未上线/)).not.toBeInTheDocument()
  expect(screen.queryByText('尚未配置自定义模型。')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '刷新模型列表' })).not.toBeInTheDocument()
  vi.mocked(fetch).mockClear()
  rerender(<UserModelsPanel scope="analysis" refreshVersion={1} />)
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
})

it('uses the shared styled selectors for protocol and client without submitting the form', async () => {
  const { container } = render(<UserModelsPanel />)
  fireEvent.click(screen.getByText('添加模型'))
  fireEvent.click(screen.getByRole('button', { name: '调用方式' }))
  fireEvent.click(screen.getByRole('option', { name: /本地客户端/ }))
  await waitFor(() => expect(screen.getByLabelText('可执行文件')).toHaveValue('/detected/codex'))
  fireEvent.click(screen.getByRole('button', { name: '客户端' }))
  fireEvent.click(screen.getByRole('option', { name: /Claude Code/ }))
  await waitFor(() => expect(screen.getByLabelText('可执行文件')).toHaveValue('/detected/claude'))
  expect(screen.getByLabelText(/实际模型 ID/)).toHaveValue('claude-code-detected-model')
  expect(screen.getByLabelText('调用参数（JSON 数组）')).toHaveValue('["--print"]')
  expect(screen.getByRole('button', { name: '保存模型' })).toBeInTheDocument()
  expect(container.querySelector('select')).toBeNull()
  expect(saved).toBeUndefined()
})
it('renders configured models in the upper selection region', async () => {
  profiles = [profile]
  const host = document.createElement('div')
  document.body.append(host)
  const { unmount } = render(<UserModelsPanel scope="analysis" selectionHost={host} />)
  const model = await screen.findByRole('button', { name: '启用 My Kimi' })
  expect(host).toContainElement(model)
  expect(screen.getByRole('region', { name: '自定义模型' })).not.toContainElement(model)
  unmount(); host.remove()
})

it('tests an unsaved dialog configuration without saving it and reports the result', async () => {
  const original = vi.mocked(fetch).getMockImplementation()!
  let tested: Record<string, unknown> | undefined
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith('/test-connection')) {
      tested = JSON.parse(String(init?.body)); return Response.json({ status: 'ok', message: '连接成功' })
    }
    return original(url, init)
  })
  render(<UserModelsPanel />)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
  const dialog = screen.getByRole('dialog', { name: '添加模型' })
  fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'My endpoint' } })
  fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://api.example.com/v1' } })
  fireEvent.change(screen.getByLabelText('实际模型 ID'), { target: { value: 'new-model' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret' } })
  fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
  await screen.findByText('连接成功')
  expect(tested?.model).toBe('new-model')
  expect(tested?.api_key).toBe('secret')
  expect(saved).toBeUndefined()
  expect(dialog).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('does not overwrite edits made while automatic detection is pending', async () => {
  const original = vi.mocked(fetch).getMockImplementation()!
  let resolveDetection!: (value: Response) => void
  vi.mocked(fetch).mockImplementation((url, init) => String(url).includes('/detect/')
    ? new Promise<Response>(resolve => { resolveDetection = resolve }) : original(url, init))
  render(<UserModelsPanel />)
  fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
  fireEvent.click(screen.getByRole('button', { name: '调用方式' }))
  fireEvent.click(screen.getByRole('option', { name: /本地客户端/ }))
  await screen.findByText('正在探测本地客户端…')
  fireEvent.change(screen.getByLabelText('可执行文件'), { target: { value: '/my/custom/codex' } })
  fireEvent.change(screen.getByLabelText(/实际模型 ID/), { target: { value: 'my-model' } })
  fireEvent.change(screen.getByLabelText('调用参数（JSON 数组）'), { target: { value: '["my-arg"]' } })
  resolveDetection(Response.json({ command: '/auto/codex', model: 'auto-model', args: ['exec'], message: '探测完成' }))
  await screen.findByText('探测完成')
  expect(screen.getByLabelText('可执行文件')).toHaveValue('/my/custom/codex')
  expect(screen.getByLabelText(/实际模型 ID/)).toHaveValue('my-model')
  expect(screen.getByLabelText('调用参数（JSON 数组）')).toHaveValue('["my-arg"]')
})

it('configures a platform gateway without asking for an API URL', async () => {
  render(<UserModelsPanel scope="analysis" />)
  fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
  fireEvent.click(screen.getByRole('button', { name: '调用方式' }))
  fireEvent.click(screen.getByRole('option', { name: /阿里云百炼模型网关/ }))
  expect(screen.queryByLabelText('API 地址')).not.toBeInTheDocument()
  expect(screen.getByLabelText('实际模型 ID')).toHaveValue('qwen-plus')
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'private-aliyun-key' } })
  fireEvent.click(screen.getByRole('button', { name: '调用方式' }))
  fireEvent.click(screen.getByRole('option', { name: /DeepSeek模型网关/ }))
  expect(screen.getByLabelText('API Key')).toHaveValue('')
  expect(screen.getByLabelText('实际模型 ID')).toHaveValue('deepseek-flash')
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'private-deepseek-key' } })
  fireEvent.click(screen.getByText('保存模型'))
  await waitFor(() => expect(saved).toMatchObject({ protocol: 'gateway', provider: 'deepseek', api_key: 'private-deepseek-key' }))
})
