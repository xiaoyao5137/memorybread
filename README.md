<div align="center">
  <img src="./static/logo.png" alt="记忆面包 Logo" width="104" />

# 记忆面包

### 目光所见，皆是未来答案

记忆面包是一款本地化的工作记忆助手，你也可以理解它是款自动化构建的本地知识库，基于LLM Wiki的设计理念+操作系统事件采集的原理制作而来。


[使用手册](https://my.feishu.cn/wiki/Fqlrw0OwTiy3LOkLPpxcaeCOnBe) · [官方网站](https://memorybread.cn/) · [问题反馈](https://github.com/xiaoyao5137/MemoryBread/issues)
</div>

## 产品概览

记忆面包会在你授权的范围内自动记录工作现场，把零散的浏览、阅读、编辑和创作过程整理成可检索、可理解、可复用的长期记忆。

1. **采集工作现场**：在授权范围内记录屏幕内容与应用上下文。
2. **沉淀长期记忆**：将零散记录整理为时间线、知识、文档、操作和数据。
3. **用于咨询与创作**：按当前问题召回相关背景，减少记忆梳理和数据找回的人工成本。
4. **形成自动化**：通过任务、日记和集成 Skill，完成自动化输出。

## 产品界面

| 工作现场，随时找回 | 基于真实工作背景创作文档 | 本地运行与隐私保护 |
| --- | --- | --- |
| ![记忆面包时间线记录与检索界面](https://memorybread.cn/show/timeline-show.png) | ![记忆面包创作智能体编写项目周报](https://memorybread.cn/show/creation-show.png) | ![记忆面包敏感内容过滤与应用黑名单设置](https://memorybread.cn/show/privacy-show.png) |

## 产品特色

1. **零维护成本**：无需手工维护记忆库，完成首次设置后，记忆的采集与提炼会完全自动运行。
2. **自动化**：支持定制Skill与定制任务，基于个人记忆，自动输出高质量内容文档。
3. **完全免费**：采集、提炼、检索与创作默认使用本地模型完成，0模型使用成本，软件亦永久免费且开源。
4. **注重隐私**：敏感规则命中的内容会直接丢弃，应用黑名单可以让指定应用完全跳过采集。

## 核心能力

| 能力 | 你可以做什么 |
| --- | --- |
| 采集 | 按日期、关键词和应用回看工作时间线与原始采集记录。 |
| 记忆 | 浏览自动提炼的文档、知识、操作和数据，并通过记忆图谱查看关联。 |
| 咨询 | 用自然语言追问过去的工作，查看回答引用的本机资料。 |
| 创作 | 基于工作记忆生成方案、周报、总结等文档，管理 Skill、Tool 与执行过程。 |
| 任务 | 创建定时任务或使用模板，并把成功结果推送到本机配置的消息渠道。 |
| 日记 | 查看和编辑系统生成的日记、周记与月记。 |
| 集成 | 导入外部知识、导出上下文包，连接 Obsidian、WorkBuddy、Codex、Claude Code 与自定义 Skill。 |
| 备份 | 导出或导入完整用户内容包；以及客户端加密的云端快照。 |

## 本地优先与隐私边界

- 采集、提炼、检索与创作默认在本机完成；本地模型不消耗云端 Token。
- 敏感内容过滤和应用黑名单在采集入口生效；命中的内容直接丢弃，只保留拦截计数。
- 完全本地化，未登录或断网时，全部核心功能仍可使用。

隐私设置请在客户端左侧导航的 **隐私** 页面管理。开始采集前，也建议先检查系统授权和应用黑名单。

## 编码 Agent 记忆检索

记忆面包提供只读的本机召回 Skill，让 WorkBuddy、Codex 和 Claude Code 按当前任务检索相关项目决策、浏览记录和工作背景。WorkBuddy 使用集成页生成的 ZIP，通过“专家·技能·连接器 > 技能 > 上传技能”安装。

```bash
# 安装到 Codex
node integrations/install-memory-retrieval-skill.mjs codex

# 安装到 Claude Code
node integrations/install-memory-retrieval-skill.mjs claude-code

# 同时安装
node integrations/install-memory-retrieval-skill.mjs both
```

## 本地开发

主要技术栈为 Tauri、React、Rust 和 Python。建议准备 Node.js 18+、Rust stable，以及符合项目要求的 Python 环境。

```bash
git clone https://github.com/xiaoyao5137/memorybread.git
cd memorybread

# 启动完整开发环境
./start.sh

# 常用维护命令
./start.sh status
./start.sh logs
./start.sh restart
./stop.sh
```

桌面前端可在 `desktop-ui` 中独立执行测试和构建：

```bash
cd desktop-ui
npm install
npm test
npm run build
```

客户端内运行或打包的 Python 代码以 Python 3.9 语法为兼容基线。修改 Python 后，必须先完成 Python 3.9 兼容性检查，再执行 DMG 打包。跨模块开发、发布与安全约束请先阅读 [`AGENTS.md`](AGENTS.md) 和相关设计文档。

## 项目结构

| 目录 | 说明 |
| --- | --- |
| `desktop-ui` | Tauri 桌面应用与 React 界面 |
| `core-engine` | 本地核心服务、数据与业务接口 |
| `ai-sidecar` | 本地模型、提炼、检索与创作能力 |
| `shared` | 跨模块协议、数据结构和公共契约 |
| `integrations` | Codex、Claude Code 与内置集成 Skill |
| `scripts` | 构建、版本与发布脚本 |
| `doc` | 产品、技术、测试与发布文档 |

## 反馈
提交问题时请附上客户端版本、Mac 芯片类型、复现步骤和经过脱敏的错误信息。不要在公开 Issue 中上传真实工作内容、截图、验证码、密钥或记忆包。
反馈地址：xianjiaqi5137@gmail.com
