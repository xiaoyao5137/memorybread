# 通用创作操作执行：实现与验收

日期：2026-09-05。对应[排查记录](creation-operation-routing-investigation-2026-09-05.md)。本次已修改客户端、Core 和 Sidecar 源码，未改写用户原会话正文，未打包或安装 DMG。

## 已改变的行为

创作入口先解释当前指令，再确定操作与必要资料。原始需求、历史正文、文档类型和已安装 Skill 只提供上下文。移除了默认记忆检索、默认全文撰写/审校，以及解析失败后按“方案”等主题关键词拼接工作流的逻辑。没有针对 c8c30648、特定章节标题或“继续未完成的改造”建立产品分支。

- 可定位删除、移动、明确文本替换和插入由确定性补丁执行。新增文字须能回溯到本轮明确提供的替换内容；需要生成新措辞时使用局部改写。节点包含子章节；重名目标、失效选区、重叠补丁拒绝执行；范围外正文原样保留，允许删除到空文档。
- 局部润色使用有选区边界的生成补丁；全文生成、只回答问题和执行 Skill 是独立操作。只有已选择的资料缺口才调用检索，已选择的能力按声明依赖排序，不补入缺席能力。
- “继续”绑定 Core 保存的未完成操作，恢复游标和产物。尚无 checkpoint 时恢复原指令；旧会话的未回复指令可成为候选；多个候选可消歧。
- 操作、文档修订和完成事件原子提交。重复请求重放结果，过期版本不覆盖，模型回传绑定 request_id，支持条件撤销。部分成功结果保存后仍保留首个失败节点，可继续完成。
- 桌面直接消费恢复请求和已提交操作，不在每条修改指令前先匹配并执行 Skill。候选决策与实际计划可追踪；延迟到达的进度快照不能覆盖提交结果。

自然语言仍需要模型理解，不能保证所有措辞都零模型调用。确定性操作执行不需要检索或全文生成；无效决策至多纠正一次，仍无效则明确失败，不运行旧兜底流程。语义理解仍受所选模型质量影响。

## 实现位置

- `shared/creation-operations/`：操作、补丁、恢复及错误契约和 JSON Schema。
- `ai-sidecar/creation/operations.py`：通用 Markdown 节点、精确选区和原子补丁。
- `ai-sidecar/creation/{service,tools,agent_loop,app}.py`：操作解释、能力描述/校验、按需执行和 checkpoint。
- `core-engine/src/storage/migrations/111_creation_operations.sql`、`src/storage/repo/creation_operation.rs`：持久化操作与事务提交。
- `core-engine/src/api/handlers/creation.rs`：恢复、模型请求绑定、提交确认及进度保护。通用对话与现有选区编辑共享 `creation_history::matches_document_base`；明确选区动作保留直接适配器。
- `desktop-ui/src/components/CreationPanel.tsx`：稳定指令 ID、恢复和结果确认、实际操作反馈。

## 自动化验收

| 范围 | 命令（各子目录执行） | 结果 |
| --- | --- | --- |
| Sidecar 创作、脑暴、Skill、选区编辑、定时创作 | `.venv/bin/python -m pytest tests/test_creation*.py tests/test_scheduled_task_creation_executor.py -q` | 407 passed |
| Core 创作仓储与处理器 | `cargo test creation --lib --test api_tests --test creation_model_boundary_tests` | 56 + 15 + 1 passed |
| Core HTTP 恢复与提交 | `cargo test --test creation_operation_tests` | 1 passed |
| 桌面 Agent、脑暴、新会话、轨迹隔离、布局 | `npm test -- src/__tests__/CreationPanelAgentLoop.test.tsx src/__tests__/CreationPanelBrainstorm.test.tsx src/__tests__/CreationPanelNewSession.test.tsx src/__tests__/CreationTraceRunIsolation.test.tsx src/__tests__/CreationPanelLayout.test.ts` | 75 passed |
| Python 3.9 兼容 | 实际 3.9.25 解释器编译、AST 语法和联合注解检查 | 通过 |
| 桌面生产构建 | `npm run build` | 版本校验、TypeScript、Vite 通过 |

新增测试覆盖任意中文/英文标题、嵌套标题、代码围栏、setext、重复文本、移动/插入/替换、越界和重叠、空结果、否定回应、恢复不重复补丁、失败后有界纠正、无效契约关闭执行。Core 使用临时 SQLite 和真实 HTTP 服务链路验证恢复、旧/缺失模型请求 ID、幂等重放、提交确认、延迟快照及撤销；模型端由可断点控制的测试 Sidecar 提供。

## 真实本地模型验收

[路由结果](evaluations/creation-operations-local-2026-09-05.json)由 `scripts/evaluate_creation_operations.py` 产生，直连本机 Ollama，使用 qwen3.5:4b、合成正文及带有“市场研究/架构设计”干扰的初稿目标。

8 个用例全部通过：中英文删除、精确替换、否定指令、续作、局部精简、删除并检索补充、仅检索回答。前六类没有无关检索/专业 Agent；两类新事实请求正确选择互联网检索。确定性补丁同时校验最终正文。该组仅验证真实模型决策，不实际访问外部资料。

[局部改写实际执行结果](evaluations/creation-transform-local-2026-09-05.json)由 `scripts/evaluate_creation_transform.py` 另行运行真实路由和真实改写模型：冗余段落变短、时间与议题保留、前后正文完全保留，检索/方案设计/全文写作/审校次数均为零。

## 交付边界

本次验证的是当前源码、桌面生产构建、真实本地模型和隔离数据库链路。未将源码构建等同于已安装应用验收，未重启用户正在运行的会话，也未修改原始会话数据。数据库迁移将在运行新 Core 时应用。

构建仍有现存的 Rust unused/dead-code、React act 测试提示和 Vite 大分块提示，均未阻断上述检查。开发中发现两类结构化输出风险：复杂补丁集合的上限在解码器中展开，触发 `failed to parse grammar` 后约束失效；环境代理也影响本地请求连接。现将解码 Schema 压缩，补丁数量/长度限制仍在代码执行；结构化抽取关闭创作式重复惩罚以保留原文和标识；回环地址使用 `trust_env=False` 直连，远程地址保留既有代理行为，并新增 IPv4/IPv6/远程地址回归。代理路径上的临时模型记录不作为本机模型验收证据。非法 JSON、工具/操作冲突、目标与原文不符均须先通过契约与定位校验；一次纠正后仍无效就停止执行。
