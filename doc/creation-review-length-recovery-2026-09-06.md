# 创作验收长度耗尽修复

最新用户记录 140（session 后缀 4c8f0faf8daf8）保存了 4103 字符正文，生命周期为 failed。持久操作的 cursor=5，下一步为 delivery_validation / delivery_check；writer 已完成，三类必要检索回执均为 completed。

本机 creation.log 的异常栈从 agent_loop._execute_step 进入 review_creation_delivery → review_delivery → _stream_direct_completion → require_complete_generation，错误码 CREATION_DOCUMENT_TRUNCATED。实际失败的是验收 JSON，不是已保存正文。

进一步用真实断点复现并核对 Ollama 日志：原失败发生于 13:10:58，输入 88638 token，超过模型 32768 上下文上限，被截到 32767，几乎没有剩余输出空间。验收直接序列化完整检索环境，其中 data_results 超过 15 万字符，带入大量原始结构/历史记录；写作使用的有界事实视图没有被验收复用。单纯扩展输出预算的复测仍立即截断，不能修复此根因。

同时，交付验收固定使用 2800 token，绕过已有完整候选和长度恢复机制；前端保存正文后又用通用中断提示覆盖了具体原因。

修复：

- 提取写作既有证据视图为共享模块，保持作者处理行为；验收复用来源/可用性/事实/时间/引用约束，保持与作者相同的来源覆盖和预算，排除原始 HTML 和历史调试载荷，明确披露摘录和来源条数。原完整环境和断点不改写。
- 输入条件检查和交付验收复用 `_stream_complete_agent_output`，长度耗尽后整份重新生成候选，四倍增长预算且最高 16384 token，不拼接残缺 JSON，也不跳过验收。
- 验收 ID 解码只允许当前契约 ID，提示要求每项只检查一次并简明说明。
- 预算恢复耗尽时转换为对应检查阶段的错误；验收报告不完整不再冒充正文截断。网络等非目标错误原样保留。
- 前端保存产物后保留具体失败原因，生命周期仍为 failed，不能把未验收正文标记完成。

验证：534 项创作后端回归通过，45 项相关前端回归通过；TypeScript 检查、git diff --check 和实际 Python 3.9.25 编译/注解兼容检查通过。新增测试覆盖两个检查节点的长度恢复、有效 JSON 遇 length 仍须丢弃、恢复上限、非目标错误传播，以及前端保全正文/失败状态/具体原因。

真实断点及复测事件保存在本机 `~/.memory-bread/verification/creation-140/`，不将用户原始业务内容写入仓库测试夹具。通过受管启动函数重新加载了 Creation Service；未打包 DMG。

最终验收输入实测降为 22614 token（来源视图 41339 字符），保持与作者同样的来源覆盖。原文三个交付条件均得到有效通过结论，未触发长度恢复。模型通过是本次运行结果，不是对任意商业事实的绝对正确性保证。

恢复原记录时额外确认了旧客户端失败保存的版本漂移：history 已为 revision 2，操作基线仍为 revision 1 的空文档，而 checkpoint 正文与已保存正文逐字相同。Core 恢复入口现在仅对“failed + 恰好下一版本 + 正文等于持久断点”同步基线，原始撤销文档不变；用户修改、跨版本或其他生命周期仍保留冲突保护。59 项 Core 创作单测及 3 项接口集成测试通过，新增测试覆盖断点重用、再次恢复幂等、完成落库、撤销原文与拒绝后续编辑。

原记录恢复已完成：通过真实 Core → Sidecar → 本地模型恢复接口得到 `run.completed`，run 为 `run-862d337a-cfef-4c27-a4dd-28b2d868304b`。数据库确认 history 140 为 completed、revision 2，操作也为 completed，验收为 pass。落盘正文与完成事件逐字一致，4103 字符原文完全保留，未重复写作或检索。正文 SHA-256 为 `1732ecb7c3cf177903aedef84c5bacd9b0abae32147b5b28392ed9f5d8d40a30`。恢复前记录和操作均已保存本机备份。
