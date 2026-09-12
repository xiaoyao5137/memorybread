# 创作「本轮操作解析失败」根因修复验收

日期：2026-09-07

## 故障定位

最新一次创作是历史 144（会话 `creation-1788717768181-974244dd33e7a8`），指令「请生成GPU成本优化的周报」，操作 `operation-c3219b04…` 于 02:02:48 标记 failed，正文为空。页面只显示「本轮操作解析失败，请重试当前指令」。

真实原因不在解析。`llm_usage_logs` 57434（qwen3.5:4b，02:03:11）与 `creation.log` 均记录 `自动文档标题不能复制技能或示例名称`，随后 `agent_loop.py:1337` 把它改写成通用的 `CREATION_OPERATION_INVALID`。排查确认五处缺陷，前三处构成本次报错，后两处在验证过程中被同一批日志暴露。

- 标题别名死锁。`validate_identity` 用「剥离 模板/创作法/文档 等后缀的 `title_key`」比对技能名。已登记技能 `creation-skill-gpu-cost-weekly-report` 标题为「GPU成本优化周报模板」，别名即 `gpu成本优化周报`；用户指令本身就在描述该主题，唯一忠实的标题「GPU成本优化周报」必然撞车。实测该守卫是抛硬币式的：`GPU成本优化周报` 与 `GPU成本优化周报模板` 被拒，`GPU成本优化的周报`、`本周GPU成本优化周报` 侥幸通过。temperature=0 下确定性复现，契约自带的修复重试同样失败，「请重试」永远无效。这与 `creation-skill-domain-title-investigation-2026-09-06.md` 要求的「最终提交前必须完成基于需求的命名校验」不符：实现只做了「不能像技能名」，漏掉了「是否来自需求」这一半。
- 错误被 fallback 掩盖。`route_capabilities` 外层 `except Exception` 把带精确错误码的 `OperationError` 一律降级为 `fallback_routing_decision`，`_apply_routing_decision` 见到 `source=="fallback"` 就抛通用文案；`agent_loop.py` 的品牌模型路径有同样掩盖。真实原因只留在服务端 WARNING 与用量埋点里，用户侧完全丢失并被误导去重试。`OperationError` 继承 `ValueError`，因此它会先消耗一次修复重试再被吞掉。
- 续跑同类死锁。`recover_identity` 用同一套别名比对判定已有正文标题，续跑会把合法标题反复判成泄漏，最终报「恢复文档时未能确定有效标题」。
- 意图核验单点超时被吞。`task_intent` 只有一次 120 秒尝试，`except Exception: return {}` 把超时、传输故障与输出不合契约压成同一个空字典，`route_capabilities` 随即硬失败为「未能核验本轮动作，请重试」。验证重放实测到该路径：Ollama 日志 `02:50:40 | 500 | 1m59s | POST /api/generate` 与 `aborting completion request due to client closing the connection`，即请求排队超过预算被本地取消。同时段 `background_processor` 与 `knowledge.extractor_v2` 正持续跑 P1 采集提炼（单次约 28 秒），单槽模型被占满。
- 技能审查静默降级。`review_skill` 同样只有一次 60 秒尝试，超时即返回 `unreviewed`，自动选中的技能被降级成普通生成，用户看不出「明明选了技能却没走技能」。重放日志实测到 `技能适用性审查不可用 … TimeoutError`，该轮因此走了通用五步流程而非技能自身工作流。

## 修复

- 命名守卫从「名字比对」升级为「基于需求的命名校验」。`skill_governance.py` 抽出 `TITLE_SEPARATORS`、`SKILL_NAMING_SUFFIXES` 与 `_compact_title`，新增 `skill_name_forms`（区分逐字名称与去后缀别名）、`reads_from_instruction`（按原有顺序的子序列判定，只忽略分隔符与大小写）、`copied_skill_name` 与 `generated_title_leaks_skill`。逐字复制技能名或示例标题一律拒绝；仅别名重合时还要看标题能否从用户本轮指令原文读出，读不出才算泄漏。错误文案改为可执行的「请改用用户指令原文中的交付物主题命名」。`ROUTING_RULES` 同步要求 title 必须能从本轮指令原文读出。
- `recover_identity` 只在已有标题逐字沿用技能名时才重新命名，续跑不再进入无法收敛的改名循环；改名提示词补上同一条可读性要求。
- `service.route_capabilities` 与 `agent_loop` 的品牌模型路径都让结构化 `OperationError` 原样上抛：先记用量埋点，再按错误码记 WARNING 并 raise，只有真正无法解析的输出才走 `fallback_routing_decision`。`_apply_routing_decision` 对 fallback 的通用报错保留，真解析失败仍失败关闭。
- `task_intent` 与 `review_skill` 改为本模块其他契约阶段一致的有界重试：预算与次数提为 `INTENT_ATTEMPTS`/`INTENT_TIMEOUT_SECONDS`、`SKILL_REVIEW_ATTEMPTS`/`SKILL_REVIEW_TIMEOUT_SECONDS` 常量，契约判定抽出为 `intent_contract_problem`、`skill_review_problem` 以便把具体原因回灌给下一次尝试，并区分超时、传输故障与契约不符写入 WARNING。失败关闭语义不变：意图预算用尽仍返回 `{}`，审查预算用尽仍返回 `unreviewed`。
- `scripts/evaluate_creation_operations.py` 原先没有包裹 `route_capabilities`，契约改为上抛后整个验收会中断；改为记录 `error`/`error_code` 后继续跑剩余用例。

## 验证

- 治理、交付契约、操作与技能匹配回归：174 passed。
- `-k "creation or skill or routing or intent"` 范围回归：569 passed，792 deselected。
- 全量回归：1365 passed，4 skipped，1 failed。唯一失败始终落在 `tests/test_inference_queue.py`，且两轮全量失败的是该文件里不同的用例（先后为 `test_battery_single_slot_p0_preempts_background_in_another_queue`、`test_power_aware_slots_cap_background_across_process_queues_and_reserve_p0`）；单独运行该文件 12 passed。该文件用真实 `time.sleep` 与墙钟断言（如 `assert time.monotonic() - started < 0.5`），对负载敏感，且 `inference_queue.py` 与其测试文件在 git 中均无改动，与本次修复无关。
- 修改文件通过 Python 3.9 语法与类型联合检查；`git diff --check` 无空白问题。
- 真实模型重放失败那轮的操作解释（qwen3.5:4b + 数据库真实技能目录）：PASS，`source=model`、`operation.kind=execute_skill`、`skill_ids=["creation-skill-gpu-cost-weekly-report"]`、`document_identity={"title":"GPU成本优化周报","source":"generated"}`，即此前被拒的标题现在通过治理。
- 真实 `review_skill` 连续两次：18–20 秒返回 `status=reviewed`、`applicable=True`、`metadata_consistent=True`、`conflicts=[]`，确认重放中的超时只是排队，重试后技能会被正常准入。
- 端到端重放（技能路径）：通过 `skill.admission` 与 `document.identity`，按技能自身工作流依次执行「检索外部资料」「本周大模型性能成本优化周会会议纪要」「AIGC进度总结」「GPU算力数据」「Token数据」。
- 端到端重放（同指令，真实负载下 634 秒）：通过 `skill.admission`、`document.identity`、`inputs.assessed`，产出标题 `# GPU成本优化周报`、正文 2569 字符，交付验收逐项核对并自动修正 2 次。
- 创作服务于 03:06:53 由启动脚本看门狗自动重启，晚于源码修改时间 03:06:33，`/health` 返回 ok，运行中进程已加载全部修复。

## 边界

- 上述端到端重放最终停在 `CREATION_DELIVERY_INCOMPLETE`（已自动修正 2 次仍未通过本轮交付验收），这不是本次缺陷。数据源 1582、1584 是 `access_mode=browser_session` 的内网 GPU 报表，`/api/browser-integration/status` 返回 `connected:false`，报表刷新以 `BROWSER_EXTENSION_FAILED` 失败并降级到存量快照；验收报告三轮一致指出缺少月度花费、实例单价与历史基线，正文也诚实声明了该缺口。按设计不应放松该闸门，需连上浏览器扩展后在原会话重试。与修复前的区别是：用户现在拿到的是带具体原因的真实错误码，而不是被误导去重试一个确定性失败。**【2026-09-07 更正】本条对 `CREATION_DELIVERY_INCOMPLETE` 的判语已被推翻**：后续会话 `…fc9e0eae89c068`（历史 146）的 `webpage_scrapes` 显示同两个报表 1582、1584 先以 `chrome_attach` 刷新成功、再在后续步骤瞬断失败，已校验事实被降级后从验收证据视图消失，真实数据被验收判为「虚构数据」，修复稿又因输出偏短被整份丢弃——该失败是产品缺陷组成的确定性死锁，不该由用户重试兜底。根因与修复见 `creation-delivery-incomplete-rootfix-2026-09-07.md`。
- 创作服务是独立进程（8001），不经过主 sidecar 的 `InferenceQueue`；`LANE_P0_CREATION` 虽已定义，但跨进程无法仲裁，单槽 Ollama 仍会被 P1 采集提炼抢占。本次只做有界重试与原因可见，跨进程优先级仲裁留作后续架构议题。
- 历史 144 与原始需求保持不变，可在原会话重试。未打包 DMG，未提交工作区其他已有修改。
