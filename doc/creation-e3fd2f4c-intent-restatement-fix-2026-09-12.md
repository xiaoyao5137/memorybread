# 再次脑暴后成文动作核验失败修复

日期：2026-09-12；范围：MemoryBread 本机开发版本。

## 根因与修复

会话尾号 `e3fd2f4c` 对应历史记录 `159`。失败发生在 `task_intent`，尚未进入资料核验。按钮发送的原指令包含“先生成一版……在当前文档基础上更新一版文档”，模型连续两次把同一份文档的概括与具体限定拆成 `create` 和 `edit`；单动作聚合器因此拒绝执行。原指令真实模型重放复现了相同结果，不是网络重试或资料缺失问题。

前端现在根据是否已有正文，只发送一次明确的新建或更新要求。仍使用已保存的简报快照；当前勾选但未提交的选项不会进入已确认决定。

后端按独立交付物识别任务，允许同一交付物的概括与后续限定形成一项连续原文证据。失败反馈只包含请求下标和动作枚举，不记录原文。真实负向测试还发现：单纯要求模型修正冲突，会诱使它把明确的两个不同产物压成一个动作。因此增加了独立目标关系复核：

- 发生过冲突后，候选目标必须覆盖先前每项写作请求的原文位置；丢弃目标、降级为约束或混合控制动作不能通过。
- 独立复核只接收原始指令和冲突片段，不接收候选合并结果。必须同时确认同一交付物、动作一致、证据连续且覆盖所有冲突片段。
- 任务、候选和复核证据的定位都必须唯一；不同或不明确的目标、缺失证据、错误字段、超时均失败关闭。复核最多一次，不能通过反复重试把拒绝变成同意。
- 意图缓存绑定升级到 `creation.current-turn-intent.v2`，旧分类在恢复路由时重新核验。原文、会话、正文和简报不需要迁移或改写。

资料检查、事实来源检查、文档修改范围及最终交付验收均保留。

## 验证范围

原失败指令修复后真实模型一次返回 `edit`。原失败断点使用完整原始指令、正文、简报、对话和技能目录，隔离重放至 `inputs.assessed`，确认选择 `transform` 并保持正文不变；没有替用户提交脑暴答案或改写原方案。

独立合成会话使用运行中的 `POST /creation/agent/run`，已有正文的时间为周三下午、地点为一楼会议室，已确认新回答将地点改为二楼会议室。验收要求只更新地点、保留时间，并实际通过资料核验和最终交付。该调用不经过 Core 的历史写入接口。

机器可读证据位于 [验收目录](evaluations/creation-intent-restatement-2026-09-12/)：真实分类与边界测试、HTTP 事件与产物、原失败重放摘要、数据保留摘要和后端回归日志。真实用户内容与原始诊断材料仅留在本机临时诊断目录；仓库中原案例证据只包含脱敏摘要。

前端回归在 `desktop-ui` 中执行：

```bash
npm test -- src/__tests__/CreationPanelBrainstorm.test.tsx src/__tests__/CreationPanelBrainstormDraft.test.tsx src/__tests__/CreationPanelAgentLoop.test.tsx src/__tests__/CreationPanelHistory.test.tsx src/__tests__/CreationPanelSessionIsolation.test.tsx src/__tests__/CreationPanelTermination.test.tsx src/__tests__/CreationBrainstormBranch.test.tsx src/__tests__/CreationBrainstormSiblingQuestions.test.tsx
npm run build
```

后端回归在 `ai-sidecar` 中执行：

```bash
PYTHONPATH=.:../shared/ipc-protocol/python .venv/bin/python -m pytest -q tests/test_creation*.py tests/test_brainstorm*.py
```

## 回归结果

- 后端创作、脑暴相关回归：1713 passed，1 skipped。
- 前端八个相关测试文件：135 passed；版本检查、TypeScript、Vite 构建通过。
- 新增与调整的恢复、独立复核、歧义证据和缓存回归均通过；六个相关 Python 文件通过 Python 3.9 语法、类型注解联合限制及实际 3.9 编译检查。
- 真实分类边界：9/9 通过，包含原旧模板、首次与再次成文、中英文重述、不同产物拒绝、参考旧正文另写、逐字补丁、引用中的写作指令及先问后写。首次负向验收发现的错误合并记录另存为 before-guard 对照，没有计作通过。
- 最终版本的原失败断点：5 次真实模型调用，204.39 秒，经过技能适用性检查并到达资料核验，操作为 transform；原正文未变。技能审查首次输出超长时按既有预算重试后排除了不适用技能，没有跳过该检查。
- 原历史、四条操作记录及脑暴状态均与诊断前快照完全一致，摘要对照通过。原会话仍保留用户失败现场，可在页面再次执行。
- 最终运行服务 HTTP 复验：40.54 秒，`run.completed`，`delivery_review.status=pass`；时间保持周三下午，地点仅更新为二楼会议室。
- 运行服务 PID `15794`，`/health` 正常；最终汇总和源码摘要见 [results.json](evaluations/creation-intent-restatement-2026-09-12/results.json)。
