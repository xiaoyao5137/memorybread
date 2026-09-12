# 创作输出长度耗尽恢复

## 根因

本机历史 132 的失败栈定位到 `run_specialist_agent`：行业调研节点固定使用 1600 token，模型达到长度上限后 `require_complete_generation` 正确拒绝不完整结果，但上层没有预算恢复能力，导致后续 writer 根本没有执行。此前的短文验收未覆盖原始长方案指令。

## 修复

- 分析、JSON、严格 Skill 与正文生成共用完整候选缓冲层。只有传输完整结束后才向调用方提交；截断候选整份丢弃，不把多次输出拼接为正文或 JSON。
- 本地长度耗尽时按四倍扩展预算，最多 16384 token；例如分析节点为 1600 → 6400 → 16384，正文为 8192 → 16384。达到上限仍明确失败，不放松截断检查。
- 正文节点显式使用现有关闭思考参数，避免思考挤占正文预算。
- 其他业务错误及外部模型错误不进入本地预算重试。原有传输故障、权限错误处理继续生效。
- 完整候选缓冲意味着生成过程中先显示节点执行状态，完整候选通过后再提供正文；不展示随后可能被丢弃的正文片段。

## 自动验证

`ai-sidecar/.venv/bin/python -m pytest tests/test_creation*.py tests/test_scheduled_task_creation_executor.py -q --disable-warnings --tb=short`：432 passed。

新增覆盖分析、JSON、Skill、正文四种入口，失败候选丢弃、原始指令保持、关闭思考、预算增长、最终上限及非目标错误不重试。修改的 Python 文件在实际 Python 3.9.25 上完成编译与语法/联合注解检查。未打包 DMG。

## 真实运行

第一轮通过本机 Core → Sidecar → 本地模型执行原始长方案指令，实际触发 1600 → 6400 预算恢复，生成 3209 字符正文，`run.completed` 且 `failed_steps=[]`。另通过原失败操作 checkpoint 恢复入口复测技能与已有检索产物，结果见后续验收补记。

原始业务指令、完整模型输出与运行事件保留在本机验收文件夹，不写入通用产品实现或测试 fixtures。生成成功仅验证执行与正文完整性，不代表对文档每项商业假设或引用做了事实核验。

恢复验收补记：原历史 132、原失败 operation 和 checkpoint 真实续跑成功，`run-3f3e820a-a16e-41e0-8ea2-21bdb1ba1d29` 返回 `run.completed`。Core 原子提交 revision 2，正文 3500 字符，包含摘要、背景目标、现状约束、方案设计、取舍风险、实施验收和结论七章，`failed_steps=[]`。GET 历史接口确认 lifecycle_status=completed，存储正文与完成事件正文逐字一致。正文 SHA-256：`d5554f1e1e47d87a79900487ead5377be52d98915d9976a3a6e96d7aaa8ad660`。本机导出位于 `~/Documents/MemoryBread创作验收/快手灵机非L0商家增长方案.md`，验收结果 JSON 同目录保存。
