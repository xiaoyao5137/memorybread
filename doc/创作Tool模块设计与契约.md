# 创作 Tool 模块设计与契约

## 目标

创作模块通过独立的“工具”Tab 管理 Agent 在创作过程中的可调用能力。Tool 与 Skill 分工如下：

- Tool 执行检索、代码生图等动作，把结果写回本轮创作环境。
- Skill 约束文档结构、行文、标题和图示风格。
- Agent 根据用户意图和已开启 Tool 动态生成执行计划，不因 Tool 已开启就无条件调用。

## 内置 Tool

| Tool ID | 名称 | 安装策略 | 调用条件 | 数据边界 |
| --- | --- | --- | --- | --- |
| `internet_search` | 互联网检索 Tool | 默认安装、始终开启 | 任务涉及最新信息、政策、标准、行业/市场调研等 | 只使用公开网页摘要与 URL，不记录 prompt |
| `memory_search` | 记忆搜索 Tool | 默认安装、始终开启 | 任务需要本地工作证据 | 本地执行，原始记忆不因 Tool 调用自动上传 |
| `data_search` | 数据检索 Tool | 默认安装、始终开启 | 日报、周报、项目总结和数据分析类文档 | 只读取本地数据资产，返回采集时间、时效和来源证据 |
| `webpage_scrape` | 网页爬取 Tool | 默认安装、始终开启 | 数据检索返回 `refresh_required=true` 的可刷新报表后 | 优先复用已登录的受支持浏览器会话，不复制或保存 Cookie；公开网页可直接 HTTP 降级 |
| `plantuml_diagram` | PlantUML 画图 Tool | 用户选择安装和开启 | 任务明确要求架构图、流程图、时序图等 | 输出可编辑的 PlantUML 代码 |
| `github_search` | GitHub 检索 Tool | 用户选择安装和开启 | 任务涉及 GitHub、公开仓库、开源或技术选型 | 只检索公开仓库，不读取或存储 GitHub Token |

## 请求契约

桌面 UI 向 `POST /api/creation/agent/run` 发送：

```json
{
  "enabled_tools": [
    "internet_search",
    "memory_search",
    "data_search",
    "webpage_scrape",
    "plantuml_diagram"
  ]
}
```

完整 JSON Schema 位于 `shared/creation-tools/creation-tools.schema.json`。

兼容规则：

1. `enabled_tools` 为新增字段，旧客户端不传时由 Core Engine 和 Sidecar 补齐四个必备 Tool。
2. 即使客户端传空数组或遗漏必备 Tool，服务端仍补齐 `internet_search`、`memory_search`、`data_search` 与 `webpage_scrape`。
3. 旧字段 `enable_rag`、`enable_web_search` 暂时保留；新客户端以 `enabled_tools` 为准。
4. Tool ID 在 Core Engine 转发时只做去重，不删除未知 ID；旧 Sidecar 忽略不认识的 ID，允许后续灰度扩展。

## 执行与可观察性

Agent 计划中的每次 Tool 调用都产生 `creation.agent.v1` 事件：

- `tool.started`：Tool 开始执行。
- `tool.completed`：Tool 完成，`data.result_count` 或 `data.diagram_type` 描述结果规模。
- `tool.failed`：Tool 暂时不可用，`data.error_code` 返回稳定错误码；Agent 使用已有上下文继续创作。
- `harness.decision`：Harness 根据上一步反馈重新规划。数据反馈分支返回触发 Tool、计数、原因码和实际追加能力；质量反馈分支返回质检轮次、问题代码、动态激活的 Skill 和实际追加的 Tool/Agent。

执行结果写回环境：

- 记忆搜索：`references`
- 互联网检索：`web_results`
- GitHub 检索：`github_results`
- PlantUML：`plantuml_diagram`
- 数据检索：`data_results`，包含时效、`refresh_required` 和 `can_use`
- 网页刷新：`webpage_scrapes`，正文只留在本轮内部环境，事件仅返回数量和状态

数据型文档没有稳定流水线：初始证据探针可包含 `data_search`；报表 URL 与其他来源统一参加 Top-K，不预留名额。Top-K 内的报表先通过后台标签静默读取 DOM/AX，`focus_policy=never`；页面未打开或目标指标不足时，在硬门禁下以 `allow_foreground_refresh=true`、`focus_policy=allow_once` 对该来源降级一次专用浏览器刷新。“保留证据截图”只控制是否在同一会话生成通用浏览器长截图，不控制即时取数。程序化校验通过的数据可以进入分析，截图 OCR 仅作补充核验；启用截图时才把证据卡插到实际使用该数据的段落或表格下方。数据、文档、知识、操作和互联网结果不因模块类型获得额外权重；刷新或校验失败不终止创作，但未验证的报表值不得当作当前事实。详细数据契约见 `shared/data-memory/`，完整设计与验收见《数据记忆与实时报表采集产品技术规范》。

初稿质量问题使用同一 Harness 决策事件：匹配的已应用 Skill 通过 `activated_skills[]` 和 `skill.completed` 动态激活；章节细节需要指标支持时可以先追加 `data_search`，再根据 Tool 反馈追加 `data_analysis_agent`；图示问题在 PlantUML 已启用时先追加 `plantuml_diagram`。专项 Agent 不直接绕过 Harness 调用 Tool 或 Skill，避免形成不可审计的嵌套循环。

文档撰写 Agent 只消费必要摘要和公开链接，不把 Tool 密钥、供应商信息或本地记忆正文写入日志。

## 页面行为

- “工具”页沿用“技能”页的扁平页头、状态标签、白底卡片和底部按钮组，不展示额外介绍框或调用链。
- 必备 Tool 展示“官方工具”和“始终开启”，安装、开启状态不可修改。
- 可选 Tool 的“安装”和“开启”是两个独立按钮；安装后默认关闭，由用户明确开启，也可以关闭或卸载。
- 配置持久化在本机 `localStorage` 的 `memory-bread_creation_tools_v1`，不上传云端。
- 桌面端使用与技能页一致的自适应卡片网格，窄屏切换为单列；所有可操作控件提供键盘焦点状态。
