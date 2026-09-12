# 创作分类器最终复核

本次验收范围是成功目标分类、来源契约和聚合行为。正式生产默认入口的 20 条真实本地模型用例全部通过，每条 1 次模型调用；没有把不同提示词版本的通过结果合并计算。此报告不替代后续完整检索、正文生成及 UI 验收。

原始请求要求读取材料后写一小段小结，但旧单目标分类器把失败回退或前置问答当成交付，造成正文为空。调整字段顺序及扩展说明曾出现编辑或混合目标回归，相关失败文件保留。最终方案让模型逐项区分 task、constraint、failure_fallback，校验最多 16 项原文引用和 role/action 一致性，再由代码选择写作目标。约束和失败回退只能使用 none，普通确认不能覆盖实质问答。

外部仍取得 action、primary_goal、deliverable、content_request 字符串；请求数组不成为执行计划。create 与既有编辑目标、写作与恢复/撤销等无法由单一动作覆盖的组合，会在既有两次上限内失败关闭，不静默丢弃请求。

正式提示词 SHA-256：`6277ae108837421d8e543e584ee9b2ec31303fde0eda246f5c1b9e166370fa9a`。

| 用例 | 预期 | 实际 | 模型调用 |
| --- | --- | --- | --- |
| memory-original | create | create | 1 |
| memory-question | answer | answer | 1 |
| summary-no-fallback | create | create | 1 |
| summary-fallback-first | create | create | 1 |
| short-notice | create | create | 1 |
| short-question | answer | answer | 1 |
| en-summary | create | create | 1 |
| en-question | answer | answer | 1 |
| failure-report | create | create | 1 |
| failure-question | answer | answer | 1 |
| edit-from-material | edit | edit | 1 |
| literal-patch | patch | patch | 1 |
| negated-writing | answer | answer | 1 |
| quoted-writing | answer | answer | 1 |
| mixed-question-writing | create | create | 1 |
| implicit-resume | resume | resume | 1 |
| undo | undo | undo | 1 |
| implicit-edit | edit | edit | 1 |
| polite-edit | edit | edit | 1 |
| polite-answer | answer | answer | 1 |

人工复核确认：原始请求与混合请求都保留实际成文任务；只读、否定写入、引用讨论均未提供写入任务；短文、纯复述、失败说明、现有正文改写、逐字替换、省略及委婉请求按预期分类。任务角色的细粒度解释仍有限，例如 summary-no-fallback 中取材步骤被标为 constraint，因此它不承担检索规划或工具执行收据职责。完整原始指令仍交给独立需求和工具规划。

相关确定性回归：test_creation_skill_governance.py、test_creation_delivery_contract.py、test_creation_operations.py 合计 271 passed（11.56 秒）。三个改动文件均以 Python 3.9.25 解析、编译和测试；没有添加无法核验的旧结构降级。扩大到创作和脑暴的最终统一回归由主任务在两边改动稳定后执行。

证据：

- 完整真实 fixture、schema、提示词、原始模型响应及最终分类：`intent-request-roles-final-real.json`。
- 确定性测试：`intent-request-roles-final-regression.log`。
- 修改前完整检索失败：`memory-path-before-content-request.json`。
- 中途语义失败：`intent-short-writing-real-candidate.json`、`intent-content-request-real-production.json`、`intent-final-real-production.json`、`intent-source-first-real-four.json`。
- 最终基线差异：`intent-request-roles-final.diff`。

此次改动文件：ai-sidecar/creation/skill_governance.py、ai-sidecar/tests/test_creation_skill_governance.py、ai-sidecar/scripts/evaluate_creation_intent.py。
