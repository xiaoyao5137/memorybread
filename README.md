<div align="center">
  <img src="./static/logo.png" alt="记忆面包 Logo" width="104" />

# 记忆面包

### 目光所见，皆是未来答案

记忆面包是一款本地化的工作记忆助手，也可以理解它是款自动化构建的本地知识库。
基于LLM Wiki的设计理念+操作系统事件采集的原理制作而来，旨在解放用户手工维护知识库的困扰，以及文档撰写的成本。


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
| 创作 | 基于工作记忆生成方案、周报、总结等文档，可在创作页原地编辑并保存 Markdown 正文，管理 Skill、Tool 与执行过程。 |
| 任务 | 创建定时任务或使用模板，并把成功结果推送到本机配置的消息渠道。 |
| 日记 | 查看和编辑系统生成的日记、周记与月记。 |
| 集成 | 导入外部知识、导出上下文包，连接 Obsidian、WorkBuddy、Codex、Claude Code 与自定义 Skill。 |
| 备份 | 导出或导入完整用户内容包；以及客户端加密的云端快照。 |

## 本地优先与隐私边界

- 采集、提炼、检索与创作默认在本机完成；本地模型不消耗云端 Token。
- 敏感内容过滤和应用黑名单在采集入口生效；命中的内容直接丢弃，只保留拦截计数。
- 完全本地化，未登录或断网时，全部核心功能仍可使用。
- 云端账户请求会标记来源为桌面客户端；服务端账户行为日志仅保存脱敏账号、来源 IP、操作类型与结果，不保存密码或验证码。

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

语义向量模型默认在 CPU 上运行，避免自动选择 MPS 后阻塞首次预热或与生成模型争用 GPU。需要指定设备时可配置 `MEMORYBREAD_EMBEDDING_DEVICE`；这只影响向量模型。日常状态轮询只检查数据库可访问性和必需表/迁移，数据库完整性扫描仍在初始化与修复质检时执行。

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

## 自定义模型与本地客户端

在模型页的采集分析模型或创作模型中，点击右上角“添加模型”打开配置弹窗；填写后可先“测试连接”，再保存。测试不会保存草稿或切换当前模型。已有用户模型在上方列表中编辑、验证、体验、启用或删除。右上角“刷新”统一更新本机配置与推荐目录。两类用途共用本机配置列表，但分别保存启用项：采集分析用于时间线分析与知识提炼，创作用于创作 Agent、咨询和悬浮咨询。不要求 MemoryBread Credit；供应商费用由用户自己的 API/客户端账户承担。

支持 OpenAI Chat Completions 兼容 API、Anthropic Messages API，以及本地 Codex、Claude Code、Kimi 和用户指定的 MiniMax、GLM 等客户端。客户端必须先安装并登录。添加模型时选择本地客户端及类型，会自动定位可执行文件，并从本机配置或已保存的接入配置中读取模型和调用参数，均可手动覆盖；未发现客户端或显式模型时会提示补充。探测不调用模型，也不安装软件或改写客户端配置。Codex 使用只读沙箱，Claude Code 禁用工具，当前 Kimi Code 使用 `--prompt` 和无工具的临时 Agent 文件。旧版 Kimi CLI 可以在参数中指定 `['--print','--output-format','text']` 的 JSON 数组形式（实际填写时使用双引号）。MiniMax、GLM 和其他客户端按安装版本配置可执行文件和参数，默认以 stdin/stdout 传递文本；参数内的 `{prompt}` 可替换提示词。

自定义模型密钥只存于本机 `~/.memory-bread/user_models.json`（0600、原子更新）；前端和本地偏好只保存模型引用，不回传密钥。CLI 无 shell 执行，在独立临时目录运行，限制 300 秒和 4 MB 输出，取消/超时会清理子进程。自定义 API 与 CLI 当前以完整文本返回，服务接口通过 SSE 包装；目录的 vision/tools 标签表示供应商能力，当前配置体验只发送文本。

运营目录来自 `GET /v1/model-catalog`，支持分页、手动刷新和按服务环境隔离的离线缓存。目录不可用时可继续手动配置并使用已保存模型。目录更新不会覆盖用户自己的配置；需要切换到新版本时重新从目录添加或编辑实际模型 ID。

相关自测：`cd desktop-ui && npm test -- --run src/__tests__/UserModelsPanel.test.tsx src/__tests__/ModelManager.test.tsx src/__tests__/CreationPanelAgentLoop.test.tsx`；`cd ai-sidecar && .venv/bin/python -m pytest tests/test_user_models.py tests/test_creation_cloud.py tests/test_cloud_llm.py -q`。


### 国内平台模型网关

在「模型 → 添加模型 → 调用方式」中选择阿里云百炼、MiniMax、智谱、Kimi 或 DeepSeek 模型网关。平台预设会自动填写模型 ID 并解析官方调用地址；只需填写该平台的 API Key，即可测试连接、保存并启用为采集分析模型。模型 ID 可以修改为账号已开通的其他文本模型。普通开放平台密钥与 Coding/Token Plan 套餐密钥不可混用。

百炼使用模型平台 API Key，不使用 RAM AccessKey。选择密钥对应地域；北京和新加坡可以只填密钥，也可填写业务空间 ID 使用专属网关；美国和日本地域必须填写业务空间 ID。已有密钥编辑时留空保留，切换平台、地域或业务空间需要重新填写。密钥保存在本机权限为 0600 的 `~/.memory-bread/user_models.json`，不进入模型列表响应或远端偏好设置。

网关预设位于 `shared/model-gateways/providers.json`，前后端共用。地址以服务端可信预设为准，忽略客户端传入的网关地址。采集提炼复用 `KnowledgeExtractorV2` 的设备模型调用路径。

接入依据：[百炼](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[MiniMax](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[智谱](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)、[Kimi](https://platform.kimi.com/docs/get-api-key)、[DeepSeek](https://api-docs.deepseek.com/)。
