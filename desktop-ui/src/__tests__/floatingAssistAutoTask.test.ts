import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY,
  FLOATING_ASSIST_AUTO_TASK_KEY,
  detectFloatingAssistTaskFromOcr,
  readFloatingAssistAutoTaskConfig,
  writeFloatingAssistAutoTaskConfig,
} from '../utils/floatingAssistAutoTask'

const installMemoryLocalStorage = () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      values.set(key, String(value))
    }),
    removeItem: vi.fn((key: string) => {
      values.delete(key)
    }),
    clear: vi.fn(() => {
      values.clear()
    }),
  }
  Object.defineProperty(window, 'localStorage', {
    value: storage,
    configurable: true,
  })
  Object.defineProperty(globalThis, 'localStorage', {
    value: storage,
    configurable: true,
  })
  return storage
}

beforeEach(() => {
  installMemoryLocalStorage()
  window.localStorage.clear()
})

describe('detectFloatingAssistTaskFromOcr', () => {
  it('识别包含任务标记和动作词的 OCR 文本', () => {
    const result = detectFloatingAssistTaskFromOcr(`
      TODO
      - [ ] 修复登录页验证码错误
      截止：明天下午前
    `)

    expect(result.matched).toBe(true)
    expect(result.score).toBeGreaterThanOrEqual(4)
    expect(result.reasons.length).toBeGreaterThan(0)
    expect(result.snippets).toContain('- [ ] 修复登录页验证码错误')
  })

  it('忽略只有导航词的普通界面文本', () => {
    const result = detectFloatingAssistTaskFromOcr('记忆面包 咨询 创作 任务 模型 隐私 监控 配置')

    expect(result.matched).toBe(false)
  })

  it('识别屏幕上的短指令任务', () => {
    const result = detectFloatingAssistTaskFromOcr('用户消息：帮我写周报，突出本周项目风险和下周计划')

    expect(result.matched).toBe(true)
  })

  it('自动识别要求 IM 前台窗口', () => {
    const result = detectFloatingAssistTaskFromOcr('TODO\n- [ ] 修复登录页验证码错误', {
      requireImWindow: true,
      screenshotSource: 'window',
      appName: 'Chrome',
      windowTitle: '项目文档',
    })

    expect(result.matched).toBe(false)
    expect(result.requiresConfirmation).toBe(false)
    expect(result.reasons).toContain('非IM窗口')
  })

  it('自动识别允许 IM 窗口里的明确任务', () => {
    const result = detectFloatingAssistTaskFromOcr('用户消息：帮我写周报，突出本周项目风险和下周计划', {
      requireImWindow: true,
      screenshotSource: 'window',
      appName: '飞书',
      windowTitle: '项目群',
    })

    expect(result.matched).toBe(true)
  })

  it('自动识别忽略只有宽泛礼貌词的残句', () => {
    const result = detectFloatingAssistTaskFromOcr('麻烦.', {
      requireImWindow: true,
      screenshotSource: 'window',
      appName: '飞书',
      windowTitle: '项目群',
    })

    expect(result.matched).toBe(false)
    expect(result.requiresConfirmation).toBe(false)
  })

  it('自动识别拒绝仅在 OCR 正文里出现消息词的非配置应用', () => {
    const result = detectFloatingAssistTaskFromOcr(`
      新对话 搜索 已安排 插件 项目
      优化卡皮巴拉真实闲置动画
      优化IM窗口任务识别
      排查悬浮球咨询触发规则
      消息
    `, {
      requireImWindow: true,
      screenshotSource: 'window',
      appBundleId: 'com.openai.codex',
      appName: 'Codex',
      windowTitle: 'Codex',
    })

    expect(result.matched).toBe(false)
    expect(result.requiresConfirmation).toBe(false)
    expect(result.reasons).toContain('非IM窗口')
  })

  it('自动识别在 IM 窗口里遇到普通待办列表时只进入确认态', () => {
    const result = detectFloatingAssistTaskFromOcr(`
      TODO
      - [ ] 修复登录页验证码错误
      截止：明天下午前
    `, {
      requireImWindow: true,
      screenshotSource: 'window',
      appName: '飞书',
      windowTitle: '项目群',
    })

    expect(result.matched).toBe(false)
    expect(result.requiresConfirmation).toBe(true)
    expect(result.reasons).toContain('任务列表需确认')
  })

  it('自动识别支持配置软件列表和关键词', () => {
    const config = writeFloatingAssistAutoTaskConfig({
      enabled: true,
      appTargets: [{ bundleId: 'com.example.WorkChat', appName: 'WorkChat' }],
      triggerWords: ['请处理'],
    })
    const result = detectFloatingAssistTaskFromOcr('老板：请处理登录页异常', {
      requireImWindow: true,
      screenshotSource: 'window',
      appBundleId: 'com.example.WorkChat',
      appName: 'WorkChat',
      windowTitle: '项目群',
      appTargets: config.appTargets,
      triggerWords: config.triggerWords,
    })

    expect(result.matched).toBe(true)
    expect(result.reasons).toContain('配置触发词')
    expect(readFloatingAssistAutoTaskConfig().enabled).toBe(true)
    expect(window.localStorage.getItem(FLOATING_ASSIST_AUTO_TASK_KEY)).toBe('true')
    expect(window.localStorage.getItem(FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY)).toContain('WorkChat')
  })

  it('自动识别拒绝未配置的软件窗口', () => {
    const result = detectFloatingAssistTaskFromOcr('老板：请处理登录页异常', {
      requireImWindow: true,
      screenshotSource: 'window',
      appBundleId: 'com.apple.mail',
      appName: 'Mail',
      windowTitle: '收件箱',
      appTargets: [{ bundleId: 'com.example.WorkChat', appName: 'WorkChat' }],
      triggerWords: ['请处理'],
    })

    expect(result.matched).toBe(false)
    expect(result.reasons).toContain('非IM窗口')
  })

  it('兼容旧版软件和关键词配置并迁移为结构化配置', () => {
    window.localStorage.setItem(FLOATING_ASSIST_AUTO_TASK_CONFIG_KEY, JSON.stringify({
      enabled: true,
      appMatchers: ['WorkChat'],
      keywords: ['请处理'],
    }))

    const config = readFloatingAssistAutoTaskConfig()

    expect(config.appTargets).toEqual([{ bundleId: '', appName: 'WorkChat' }])
    expect(config.triggerWords).toEqual(['请处理'])
  })

  it('忽略云文档阅读页里的会议议程和普通动作词', () => {
    const result = detectFloatingAssistTaskFromOcr(`
      显示器 1:
      Chrome
      文件 编辑 显示 历史记录 书签 个人资料 标签页 窗口 帮助
      docs.example.com/d/home/sample-document
      记忆面包 MemoryBread
      运营控制台|记忆面包
      可编辑
      所有改动已自动保存
      正文，默认字体，11
      7月7日周二 10:15
      灵机专项双日会
      零、上次todo
      一、核心数据@李鑫@郑媛元
      二、基模进展@石山@鲜嘉麒
      三、组件进展@鲜嘉麒@蔡一超
      零、上次todo
      三、组件进展
      方向
      基模API网关
      Agent能力
      Workflow编排
      智能选品
      现状
      已上线能力：
      自动化生产：服饰类工作流
      灵机小二版：工作流搭建与执行
      1.冷启：生服模型适配电商场景正在落地
      开发中；
      2. 追爆：正在调研生服的追爆机制
      进展
      灵机agent二期，联调中
      灵机小二版：
      对话生成Agent，开发中
    `)

    expect(result.matched).toBe(false)
    expect(result.reasons).toContain('文档页面需确认')
    expect(result.requiresConfirmation).toBe(false)
  })

  it('云文档里的明确待办只进入确认态', () => {
    const result = detectFloatingAssistTaskFromOcr(`
      Chrome
      docs.example.com/d/home/example
      所有改动已自动保存
      TODO
      - [ ] 修复登录页验证码错误
      截止：明天下午前
    `)

    expect(result.matched).toBe(false)
    expect(result.requiresConfirmation).toBe(true)
    expect(result.reasons).toContain('文档页面需确认')
  })
})
