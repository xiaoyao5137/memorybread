<div align="center">
  <img src="./static/logo.png" alt="记忆面包 Logo" width="104" />

# 记忆面包

### 目光所见，皆是未来答案

本地优先的 AI 工作记忆助手：自动沉淀工作现场，在需要时找回背景、回答问题、生成文档并持续复用。

[使用手册](https://my.feishu.cn/wiki/Fqlrw0OwTiy3LOkLPpxcaeCOnBe) · [官网](https://memorybread.cn/) · [问题反馈](https://github.com/xiaoyao5137/MemoryBread/issues)
</div>

## 产品概览

记忆面包把日常浏览、阅读、编辑和创作中有价值的内容，逐步加工为可检索、可理解、可复用的工作记忆。它不是另一套需要手工维护的知识库，而是一条持续运行的工作闭环：

1. **采集工作现场**：在授权范围内记录屏幕内容与应用上下文。
2. **沉淀长期记忆**：将零散记录整理为时间线、知识、文档、操作和数据。
3. **用于咨询与创作**：按当前问题召回相关背景，减少重复解释。
4. **形成自动化**：通过任务、日记和集成 Skill，让记忆继续产生工作成果。

## 产品界面

| 工作现场，随时找回 | 基于真实工作背景创作文档 | 本地运行与隐私保护 |
| --- | --- | --- |
| ![记忆面包时间线记录与检索界面](https://memorybread.cn/show/timeline-show.png) | ![记忆面包创作智能体编写项目周报](https://memorybread.cn/show/creation-show.png) | ![记忆面包敏感内容过滤与应用黑名单设置](https://memorybread.cn/show/privacy-show.png) |

## 核心能力

| 能力 | 你可以做什么 |
| --- | --- |
| 采集 | 按日期、关键词和应用回看工作时间线与原始采集记录。 |
| 记忆 | 浏览自动提炼的文档、知识、操作和数据，并通过记忆图谱查看关联。 |
| 咨询 | 用自然语言追问过去的工作，查看回答引用的本机资料。 |
| 创作 | 基于真实工作背景生成方案、周报、总结等文档，管理 Skill、Tool 与执行过程。 |
| 任务 | 创建定时任务或使用模板，并把成功结果推送到本机配置的消息渠道。 |
| 日记 | 查看和编辑系统生成的日记、周记与月记。 |
| 集成 | 导入外部知识、导出上下文包，连接 Obsidian、Codex、Claude Code 与自定义 Skill。 |
| 备份 | 导出或导入完整用户内容包；登录后可使用客户端加密的云端快照。 |

## 本地优先与隐私边界

- 采集、提炼、检索与创作默认在本机完成；本地模型不消耗云端 Token。
- 敏感内容过滤和应用黑名单在采集入口生效；命中的内容直接丢弃，只保留拦截计数。
- 未登录或断网时，本地核心能力仍可使用。账户、软件更新、任务奖励、可选云端模型和云端备份等能力按需联网。
- 新版备份包含知识、文档、操作、日记、创作记录、任务、设置、本地 Skill（含完整技能包文件）和其他用户文件；原始采集记录、采集截图、日志、模型与运行时缓存不进入备份。
- 云端内容包在客户端加密后上传，服务端不读取快照明文；本机导入和导出不要求登录。消息渠道等本机配置只会随加密内容包迁移，不进行明文同步。

隐私设置请在客户端左侧导航的 **隐私** 页面管理。开始采集前，也建议先检查系统授权和应用黑名单。

## 快速开始

当前官网直装版支持 macOS，并分别提供 Apple Silicon 与 Intel 安装包。

1. 从官网或发布页下载与 Mac 芯片匹配的版本并完成安装。
2. 首次启动时，按系统提示授予屏幕录制和辅助功能权限。
3. 点击 **初始化**，保持网络畅通。必要组件、采集提炼能力、本地记忆库和核心功能会自动安装并质检；可以最小化应用等待完成。
4. 初始化通过后正常工作一段时间，再到 **采集** 查看时间线，或到 **咨询** 输入一个具体线索开始召回。

完整步骤、功能说明和故障排查入口见 [记忆面包使用手册](https://my.feishu.cn/wiki/Fqlrw0OwTiy3LOkLPpxcaeCOnBe)。

## 推荐使用方式

- **找回内容**：说明大致时间、项目、应用或关键词，例如“找回上周评审支付方案时的结论”。
- **生成文档**：先写清交付物、读者和时间范围，再预览参考资料并开始创作。
- **积累记忆**：让采集保持稳定运行，定期检查提炼结果，而不是只保留原始截图。
- **保护隐私**：将聊天、邮箱、相册、密码管理器等敏感应用加入黑名单，并保留必要的敏感过滤规则。
- **定期备份**：在重要版本或换机前导出完整内容包；需要跨设备恢复时可使用同一份客户端加密云端备份。导入完成后软件会自动重启并加载恢复内容。

## 编码 Agent 记忆检索

记忆面包提供只读的本机召回 Skill，让 Codex 和 Claude Code 按当前任务检索相关项目决策、浏览记录和工作背景。只有本轮选中的相关片段会进入对应 Agent 会话。

```bash
# 安装到 Codex
node integrations/install-memory-retrieval-skill.mjs codex

# 安装到 Claude Code
node integrations/install-memory-retrieval-skill.mjs claude-code

# 同时安装
node integrations/install-memory-retrieval-skill.mjs both
```

安装器不会默认覆盖已有目录；确认替换时使用 `--force`，旧版本会先移动到带时间戳的备份目录。完整契约见 [`shared/memory-retrieval`](shared/memory-retrieval)。

## 本地开发

主要技术栈为 Tauri、React、Rust 和 Python。建议准备 Node.js 18+、Rust stable，以及符合项目要求的 Python 环境。

```bash
git clone https://github.com/xiaoyao5137/memorybread.git
cd MemoryBread

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
