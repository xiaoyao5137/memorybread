# 创作「已自动修正 2 次仍未通过本轮交付验收」根因修复验收

日期：2026-09-07

## 结论先行

会话 `creation-1788777595771-fc9e0eae89c068`（历史 146，指令「请生成GPU成本优化的周报」）在 18:39:55–18:49:27 跑了 571 秒，最终失败并提示「已自动修正 2 次仍未通过本轮交付验收；已保存已生成内容，可重试继续」。

这不是「用户没连浏览器扩展，请连上后自行重试」的外部条件问题，而是四个产品缺陷叠加出的**确定性死锁**：本轮已经刷新成功并通过结构校验的真实数据，被同一来源后续步骤的瞬态刷新失败整体降级并从验收证据视图里抹掉，验收因此把正文里的真实数据判成「虚构数据」；自动修正节点确实重写了正文，但候选稿因输出偏短被整份丢弃，正文一字未变；没有收敛守卫，同一份正文被连续重审 3 次；拒绝原因既没进日志也没进轨迹，用户侧只剩一句「可重试继续」。本文记录四者的证据与修复，并推翻 `creation-operation-parse-failure-rootfix-2026-09-07.md`「边界」一节中对该错误码的判语。

## 故障定位

失败序列全部可在 `~/.memory-bread/memory-bread.db` 的 `creation_history.agent_trace_json`、`creation_operations.checkpoint_json` 与 `~/.memory-bread/logs/ollama.log` 中复核。

- 采集先成功后瞬断。`environment.webpage_scrapes` 四条：1582（GPU使用情况一览）18:43:28、1584（GPU项目用量管理）18:43:43 均为 `status=completed`、`collector=chrome_attach`、`interaction_mode=background_tab`；随后 Token数据 步骤对**同两个来源**再刷新，得到 `1584 failed` / `1582 failed`，`error_code=BROWSER_EXTENSION_FAILED`。扩展不是「从未连上」，而是本轮跑到中途掉线。
- 已校验事实被后续失败吃掉。合并后的终态自相矛盾：两条来源仍留着 `creation_evidence.validation_status=verified`、`creation_evidence.validation.data_usage_status=verified`、`data_usage_status=verified`，却同时被写成 `can_use=False`、`content_excerpt=None`、`structured_data=None`、`provenance=None`，1582 加 `unavailable_reason=refresh_failed`，1584 走快照降级（`freshness_class=stale`、`evidence_status=failed`、带 `stale_fallback`）后被 `_enforce_report_evidence_policy` 二次剥空。
- 验收据此判「虚构」。`environment.delivery_review` 三个 check 全部 `passed=false`、`evidence=""`，理由写作「产出包含大量无法验证的虚构数据（如2026年9月5日的实时报表、具体的项目卡数与ROI数值）」「检索到的资料仅包含2026年1月的历史快照和2026年8月的部分项目状态」，corrections 第一条即「删除所有无法验证的虚构数据」。而撰写与验收共用 `creation/prompt_evidence.py` 的有界视图，只有 `can_use is True` 才暴露摘录：写入正文时数据可见，验收时不可见。同一份报告又在第 3 条里把 102 个项目、1803.59 张卡、39.86 倍 ROI 称作「已确认事实」——结论自相矛盾，来源是视图不对称而非模型乱判。
- 自动修正必然被丢。`environment.rejected_document_mutations` = 两条 `{"agent_id": "delivery_repair", "problems": ["document_content_lost"]}`。基线 4563 字符，守卫要求候选稿 ≥ 0.55×基线（2509 字符），4B 模型在含全文 + 契约 + 逐条 checks 的修复提示词下写不满，候选稿整份丢弃，正文逐字未变。
- 没有收敛守卫。轨迹 18:47:07 `delivery.checked` → 18:48:28 `document.mutation.rejected` → 18:48:44 `delivery.checked` → 18:49:11 `document.mutation.rejected` → 18:49:27 `delivery.checked` → `run.failed`。`ollama.log` 对应 5 次 `POST /api/generate`（56.8s / 1m20s / 15.6s / 27.6s / 15.6s，合计约 196 秒单槽模型时间）；正文 hash 未变，三次验收结论逐字一致。
- 诊断黑洞。轨迹里 108 个事件的 `data` 全是 `{}`，`run.failed` 落盘成「创作 Agent 执行失败（详细错误未写入轨迹）」，真实原因只在内存里；`llm_usage_logs` 在 18:46:10–18:49:37 区间零记录，即 `_stream_complete_agent_output`（撰写、润色、验收、修复共用）此前完全不埋点；`creation.log` 无一行相关 WARNING。
- 语义矛盾的兜底提示。`app._creation_failure_details` 把 `OperationError` 一律收敛为 `retryable=False`，页面却仍固定拼接「可重试继续」。

## 修复

按契约优先顺序落地，不放松任何验收闸门（`integrity_problems` 与 `require_complete_generation` 的判定条件均未放宽，`document_content_lost` 守卫原样保留）。

- 证据自洽（`creation/service.py`）。新增 `_has_retained_verified_capture`，只承认即时刷新成功通道写入的组合（`can_use` + `fresh` + `evidence_status=verified` + `creation_evidence.validation_status=verified` + 非空摘录），历史工作记忆与派生值不满足。报表刷新失败分支先命中该条件时**保留本轮已校验采集**，只标 `refresh_required=True` 与 `refresh_limited.reason=later_refresh_failed_verified_capture_retained` 并记 INFO；确无快照可留时，把矛盾标记改名保留可诊断性（`creation_evidence`→`stale_creation_evidence`、`data_usage_status`→`stale_data_usage_status`）并清空摘录与结构化数据，杜绝「同一记录既说可用又说不可用」。
- 策略不再摧毁已接受快照（`creation/agent_loop._enforce_report_evidence_policy`）。合并阶段已显式接受的 `stale_fallback` 条目补 `data_usage_status=snapshot_only` 后原样放行；确需剥离事实时补 `unavailable_reason=evidence_not_verified`、迁移 `data_usage_status`，并在真丢掉可用事实上记 WARNING。
- 让修正真正落地（`creation/document_integrity.py` + `agent_loop`）。新增纯函数 `_heading_entries`（跳过围栏代码块内的伪标题）、`level2_sections` 与 `merge_rewritten_sections`：按基线章节顺序把重写稿逐章合并回来，候选缺失或明显偏短（尾章 < 基线同名章 60%）的章节沿用基线，只按通用结构合并、不识别任何业务字段。`_salvage_truncated_polish` 仅在**问题集恰为 `document_content_lost`** 的 polisher 步骤上启用，拯救后必须复算同一守卫，通过才提交并发 `document.mutation.salvaged`，否则维持原拒绝行为；`repeated_content`、`section_structure_changed` 等一律不救。
- 空转即刻收敛（`agent_loop` delivery_check + `_repair_delivery`）。`delivery_last_revise` 记录本轮 revise 的正文 hash 与 corrections，同一 hash 再次 revise 视为确定性空转，立即按验收给出的具体缺口收尾，不再烧完预算；`_repair_delivery` 正文未变时跳过重复重审并置 `delivery_repair_stalled`。blocked、预算耗尽、post-loop 三处统一改用 `delivery_incomplete_message`。
- 失败必须给出可执行原因（`creation/delivery_contract.py`）。新增 `delivery_gap`（corrections → 未通过 check 的 reason → 契约里 `state=missing` 的 need，逐级取第一个安全短句）与 `delivery_incomplete_message`；`MAX_DELIVERY_GAP_CHARS=96` 与 `_UNSAFE_GAP_PATTERN` 保证文案能穿过客户端敏感词过滤——宁可退回通用提示，也不能让用户拿到的整段原因被吞成兜底。实测用 146 的真实报告回放：`自动修正 2 次后仍未通过验收：将周报内容重构为基于现有资料（2026年1月历史快照、2026年8月周会纪要、通用行业基准）的复盘与规划报告。已保留当前正文，可补充该资料或指出待改段落，我会重新修正并验收`，102 字符、无花括号、不命中过滤。
- 可观测性补齐。`_stream_complete_agent_output` 增加 `record_output_usage`（成功与失败各记一次，失败带 `budget=`/`attempts=`，用 `getattr` 容忍只实现传输函数的轻量测试替身）；`_delivery_repair_brief` 只把 `status`、`corrections`、未通过 check 的 `{id, reason}` 交给修复节点，去掉逐行正文复述的 `checks[].evidence`（修复节点本来就能看到完整文档），并在提示词里明确要求「输出完整全文，不得只输出部分章节」；修复提示词与拯救路径都补 WARNING/INFO。
- 客户端不再吞原因、不再乱喊重试。`desktop-ui/src/utils/userFacingError.ts` 新增 `toCreationFailureMessage`（仅对 `errorCode` 形如 `CREATION_*` 的本地创作失败直显原因，其余仍走通用过滤；底层 fetch 失败不外泄）与 `recoverableCreationHint`（仅 `retryable === true` 才加「，可重试继续」）；`CreationPanel.tsx` 两处 `setError` 切换到该组合，`toStoredAgentEvent` 保留 `document.mutation.rejected/salvaged` 的 `{problems, candidate_length, merged_length}`、`delivery.checked/rechecked` 的有界 `delivery_review{status, corrections, failed_checks}`，`run.failed` 不再固定写成「详细错误未写入轨迹」。

## 验证

- sidecar 全量：`pytest -q` → **1393 passed，4 skipped**（104 秒）。
- 范围回归：`-k "creation or delivery or integrity or evidence"` → 635 passed；`tests/test_creation_document_integrity.py + tests/test_creation_delivery_contract.py` → 102 passed。
- 新增/改造用例：`test_creation_document_integrity.py` +4（截断修复按章节拯救成功、`repeated_content` 仍整份拒绝、无重叠章节返回空串、围栏内伪标题不算章节）；`test_creation_agent_loop.py` +6（同轮已校验采集被保留、无救降级记录自洽、策略放行快照、策略降级留可诊断标记、成功与截断两条用量埋点）+ 改造 `test_delivery_check_allows_multiple_repair_cycles_before_giving_up`（两次校验之间真正改正文，`service.review_count == 3`）+ 新增 `test_delivery_check_stops_at_once_when_repair_changed_nothing`（断言原因里含具体缺口、`"重试" not in str(error)`、`review_count == 2`、`delivery_repair_count` 停在 1）；`test_creation_delivery_contract.py` 按新文案契约更新 1 处断言。
- desktop-ui：`vitest run` → 724 例中 **720 passed**；`CreationPanelAgentLoop.test.tsx` 38 passed（含新「确定性验收失败不再喊『可重试继续』」与「服务端标为可重试的失败仍提示重试继续」两用例），`userFacingError.test.ts` 8 passed（新增 CREATION_ 错误码直显、非创作错误码仍过滤、网络错误不外泄 + 长文案有界截断、retryable 三分支）。唯一失败的 `SettingsDebugMode.test.tsx` 4 例是工作区里另一处未提交改动（`Settings.tsx` 新增 `<PermissionPreparation />` 卡片多打一次请求，断言 `spy 2 次` 变 3 次）导致，与本次修改无关。`npx tsc --noEmit` 无输出。
- Python 3.9 兼容：AST 扫描 `creation/` 全包 `PEP604 unions: []`、`match statements: []`，venv 实测 3.9.25。

## 边界

- 本文推翻 `creation-operation-parse-failure-rootfix-2026-09-07.md`「边界」第 1 条对本错误码的判语。该条把 `CREATION_DELIVERY_INCOMPLETE` 判为「不是本次缺陷……需连上浏览器扩展后在原会话重试」；146 的 `webpage_scrapes` 证明同一轮里两个报表先以 `chrome_attach` 刷新成功、再在后续步骤瞬断失败，因此「让用户自行重试」不成立：瞬断仍会发生，而重试要能收敛，前提是已校验事实不被后续失败抹掉、修正能真正写回正文、空转不再重复烧预算。历史 144 那轮「扩展始终未连」的事实本身不改，但据此推出的「用户侧自行兜底」结论作废。
- 修复不承诺任何情况下都验收通过。扩展确实全程不可用、且库内无可用快照时，报表事实仍进不了正文，此时交付可能因缺资料而 revise；差别在于现在会在第一次无进展时即刻收尾，并把「缺哪一份资料」直接说给用户，同时该来源会被 `_has_retained_verified_capture`/快照通道尽可能保留。
- 原会话 146 无需用户手工编辑正文：重跑同一指令即可，报表刷新若成功则直接成稿；若中途再瞬断，本轮已校验采集会被保留、快照会被验收看见，修复稿也能按章节落地。未打包 DMG，未提交工作区其他已有修改。
