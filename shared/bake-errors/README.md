# Bake Sidecar 错误契约

`/bake/extract` 与 `/bake/merge_document` 的非 2xx 响应必须符合
[`bake-error.schema.json`](bake-error.schema.json)，Core 同时校验 `code`、
`scope`、`retryable`，不允许只凭 HTTP 状态判断是否暂停整条流水线。

| 范围 | 错误码 | Core 行为 |
| --- | --- | --- |
| `service` | `INFERENCE_PREEMPTED`、`INFERENCE_QUEUE_BUSY`、`MODEL_RATE_LIMITED`、`MODEL_UNAVAILABLE` | 不消耗候选重试；等待整服务恢复 |
| `candidate`，可重试 | `INFERENCE_TIMEOUT`、`BAKE_OUTPUT_TRUNCATED`、`BAKE_OUTPUT_INVALID`、`BAKE_MODEL_RESPONSE_INVALID`、`BAKE_MODEL_UPSTREAM_ERROR`、`BAKE_INTERNAL_ERROR` | 记录候选失败；最多 3 次后进入终态并推进 watermark |
| `candidate`，不可重试 | `BAKE_MODEL_REQUEST_INVALID`、`BAKE_REQUEST_INVALID` | 当前候选立即进入终态并推进 watermark |

未知、缺字段或三元组冲突的 5xx 统一映射为
`BAKE_UNCLASSIFIED_UPSTREAM_ERROR`，按候选有界重试。Core 自己检测到的成功响应
损坏和产物结构错误分别使用 `BAKE_SIDECAR_RESPONSE_INVALID` 与
`BAKE_ARTIFACT_PAYLOAD_INVALID`，同样不得永久卡住队首。

烘焙排队与执行分别计时：最多等待执行槽 90 秒，取得槽后普通输入有 180 秒、
长输入有 300 秒执行预算。排队到期返回 `503 INFERENCE_QUEUE_BUSY`，只撤下尚未
开始的任务；执行超时才使用候选级 `INFERENCE_TIMEOUT`。Core 对该排队繁忙响应
保留当前 run 和候选，以有界退避重新等待槽，维持跨进程就绪需求；不重跑已经
开始的推理、不增加候选失败次数。总等待受 run 的 30 分钟总预算约束，重新发起
请求前必须留足单次完整预算，预算不足时延期至下一轮。
Core HTTP 总预算为 410 秒，覆盖两个预算与响应收尾；调度并发不得超过实时推理容量。

`BAKE_DOCUMENT_MERGE_PENDING`：正文合并校验失败、来源尚未应用。candidate 可重试，遵守现有 3 次上限；已有来源关联或其他产物不得抑制该重试。
