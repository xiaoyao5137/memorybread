# 创作正文重复与风险标注膨胀：设计及验收

## 问题证据

只读核对本机历史 131：正文 13,686 字符，风险标注 54 次，同一 GMV 段落完整重复 53 次；正文在第三章中途停止。最终字体润色补丁扩展了重复内容并删除后续章节，旧质检只报表达和加粗软问题，仍交付成功。原文哈希保存在验收 JSON 中，未修改原文。

## 方案与实现

1. 收窄证据披露规则。移除各节点中“风险披露最高优先级、必须补齐参考数据”的强制要求。仅采用支持当前任务的事实，普通历史背景写明真实周期，同来源的必要限定合并一次，不把未指定的任务解释成本周汇报。
2. 结构化风险按正文实际使用的指标和值输出，过滤无关检索结果；相同风险去重，程序管理的说明重复执行仍得到相同正文。数值 0 保留。多来源表头及正常短标签不会被当作正文循环。
3. 生成候选先验收再提交。Writer、Skill、云端回传共用正文检查；局部模型补丁在应用前也检查重复。流式输出出现实质性重复时停止消费，开头空白不误中断，结束阶段仍拒绝空正文。
4. 自动润色保留二级章节顺序与主体内容。重复、丢章或大幅截短的候选不覆盖上一有效版本，执行记录发出 document.mutation.rejected。新生成异常正文以 CREATION_DOCUMENT_INVALID 拒绝；最后交付前再次检查，不能只靠非空判断成功。
5. 本地及兼容云端流检查长度停止原因。length/max_tokens 返回 CREATION_DOCUMENT_TRUNCATED，不把因输出预算耗尽产生的片段视为完整结果。

契约见 [creation-integrity](../shared/creation-integrity/README.md)。实现位于 creation/document_integrity.py、agent_loop.py 和 service.py；没有修改数据库历史或增加针对特定业务、指标和会话的产品分支。

## 自测

- 创作、脑暴、Skill、云端协议、局部编辑、队列及定时创作回归：425 passed。命令：在 ai-sidecar 执行 `.venv/bin/python -m pytest tests/test_creation*.py tests/test_scheduled_task_creation_executor.py -q --disable-warnings --tb=short`。
- 新增覆盖：长段循环、无换行循环、格式变化、正常短标签与代码、异常润色原子拒绝、初稿/Skill 拒绝、局部补丁拒绝、风险去重与幂等、无关指标省略、多个来源和数值 0、三种长度停止协议、空终止块。
- 受影响六个 Python 文件在实际 Python 3.9.25 上完成编译、AST 语法和联合注解检查；git diff --check 通过。
- 原故障正文只读回放：检测为 repeated_content；实际候选提交入口返回 CREATION_DOCUMENT_INVALID，状态未被改写。
- 真实本地模型生成 590 字符、润色 530 字符，三个二级章节保留，两次风险标注均为 0。使用合成商家视频工具案例，不读取或重写原始业务文档。[结构化结果](evaluations/creation-integrity-local-2026-09-05.json)

- 当前服务 HTTP 验收：POST /creation/agent/run，经真实本地模型返回 run.completed，338 字符正文，风险标注 0，失败事件 0。[结构化结果](evaluations/creation-integrity-http-2026-09-05.json)
- 开发服务已自动加载晚于源码修改的进程。一次在源码变更期间发生的自动重载中断未计为通过；源码稳定后的上述请求完整结束。

## 交付边界

重复检测是确定性的内容完整性保护，不等于可以自动证明所有事实或语义都正确。正常证据与范围检查继续生效。历史 131 保留原状；可在修复后的版本重新生成。未打包 DMG。
