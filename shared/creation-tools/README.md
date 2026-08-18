# 创作 Tool 契约

`creation-tools.schema.json` 定义创作模块传给 Core Engine 与 AI Sidecar 的稳定 Tool ID 和 Tool 事件形态。

## 兼容规则

- `enabled_tools` 是新增的可选请求字段；旧客户端不传时，Core Engine 与 Sidecar 都补齐 `internet_search`、`memory_search`、`data_search`、`webpage_scrape`。
- `internet_search`、`memory_search`、`data_search`、`webpage_scrape` 是必备 Tool，客户端不得卸载或关闭，服务端收到空数组时也必须补齐。
- `memory_search` 默认最多召回 10 条，可在创作“工具”页配置为 1～30 条；`data_search` 默认最多召回 30 条，可配置为 1～50 条。旧客户端不传 `max_references` / `data_search_limit` 时由 Core Engine 与 Sidecar 补齐默认值，越界值在服务端收敛到契约范围。
- 日报、周报、项目总结和分析任务可把 `data_search` 作为与记忆、文档、知识等来源平权的证据探针；报表 URL 统一参加 Top-K。Harness 对 Top-K 报表调用 `webpage_scrape` 时先做不抢焦点的 DOM/AX 静默取数；页面未打开或静默结构不足时，可在焦点硬门禁下为每个来源降级一次专用浏览器会话。关闭截图时只在建立隐藏窗口时短暂切换一次，恢复原应用后在隐藏窗口完成页面交互和长 DOM 采集。Skill 的“保留证据截图”只控制图片资产，不控制即时取数。只有程序化校验通过的数据才进入 `data_analysis_agent`，不预置供应商专用流程。
- 初稿质检通过 `$defs.quality_issue` 返回问题代码、严重度、目标 Agent、可观察证据和前置能力。`harness.decision` 兼容数据反馈与质量反馈两个分支；质量分支可通过 `activated_skills[]` 动态激活匹配的已应用 Skill，并追加数据分析、PlantUML、Mermaid、文档重写、五类专项润色和再次质检。
- 首次创作必须把 `chapter_design_agent` 放在首个 Writer 前；专项润色不预置成固定流水线，只在质量问题命中时运行，最多三轮。
- 可选 Tool 当前包括 `plantuml_diagram`、`mermaid_diagram`、`github_search`。未安装或未开启时不得进入 Agent 计划。
- 未识别的 Tool ID 保留在转发契约中，但旧 Sidecar 不调用，以便新客户端与旧服务兼容。
- `enable_rag`、`enable_web_search` 暂时保留供旧客户端兼容；新客户端以 `enabled_tools` 为准。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `TOOL_NOT_INSTALLED` | 请求调用尚未安装的可选 Tool |
| `TOOL_DISABLED` | Tool 已安装但当前关闭 |
| `TOOL_UNAVAILABLE` | Tool 的本地或外部依赖暂时不可用 |
| `TOOL_EXECUTION_FAILED` | Tool 已启动但执行失败 |

数据 Tool 的细化错误码、时效规则和浏览器会话隐私边界见 [`../data-memory`](../data-memory)。

Tool 失败通过 `creation.agent.v1` 的 `tool.failed` 事件返回，不得在日志或事件中写入用户原始 prompt、密钥或本地记忆正文。

质量决策通过同协议的 `harness.decision` 返回 `quality_cycle`、`issue_count`、`issue_codes`、`activated_skills[]` 和实际 `scheduled[]`。旧客户端忽略这些新增字段，数据反馈事件的原字段保持不变。
