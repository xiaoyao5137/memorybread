# Creation operations v1

The current instruction controls the operation. Session goals, existing documents,
prior outputs and available skills provide context; their presence never schedules
research, writing or review. Natural-language instructions are interpreted by the
model. Structured selection actions keep their direct adapter.

## Decision contract

`routing.schema.json` describes the decision. Runtime validators restrict capability
IDs to enabled tools and available skills. Local decoding receives a JSON Schema;
the union contract and document selectors are also validated by application code.
Loopback model requests bypass environment HTTP proxies. The decoder receives a
compact schema without large bounded repetitions, avoiding llama.cpp grammar
complexity failures. The full contract budgets are enforced by application code. `tools` and `agents` are selected
resources, not mandatory stages. Every decision includes one `operation`:

| Kind | Input and execution |
| --- | --- |
| `patch` | Nonempty `patches`: deterministic delete, replace, insert or move. No resource calls. |
| `transform` | Nonempty `targets`: generate bounded patches, optionally using selected resources. No full-document writer/reviewer. |
| `generate` | Generate the requested document using only selected resources. |
| `respond` | Nonempty `response`: answer or clarify without changing the document or calling resources. |
| `answer` | Generate an answer from selected resources without changing the document. |
| `resume` | `operation_id` from Core-owned `pending_operations`; restore saved work. No new resource plan. |
| `undo` | `operation_id` from Core-owned `undo_candidates`; conditional inverse commit. |
| `execute_skill` | Nonempty available `skill_ids`; run their declared workflows. |

`constraint_skill_ids` optionally loads available rules without rerunning a workflow.
Capabilities declare their purpose. Dependencies order only capabilities already
selected; they do not introduce research or other absent stages. Actual execution
plans are emitted with decision source and reason.

An invalid decision fails closed. Local interpretation can retry once with the
contract error; this performs no tools and never substitutes keyword-based routing.
External model results pass the same decision validation before execution.

Intent evidence describes independently requested outcomes, not every verb or
sentence. A general request and its later restatement or target restriction for
the same deliverable form one task with a continuous original-source quote. For
example, drafting an updated version of the current document is an edit even if
an earlier sentence calls it a draft. An existing document alone does not turn a
request for a separate new deliverable into an edit. Distinct create/edit outcomes
and writing/control conflicts still fail closed; retry feedback identifies their
request indices and actions without logging source text. Saved instructions use
the same classifier, so recovery does not require rewriting conversation history.
After a scope conflict, a retry can only return a single writing outcome if its
original-source span still covers every disputed writing request and one bounded,
independent review confirms the same outcome and action. That review sees the
original instruction and disputed excerpts, not the proposed merged answer; its
evidence must also cover every disputed request. Missing evidence, a distinct or
unclear target relationship, malformed output, timeout, or a writing/control merge
fails closed. Protocol retries alone cannot authorize dropping an earlier outcome.
All spans used for this cross-request coverage proof must resolve uniquely.
The current-turn intent binding is versioned as `creation.current-turn-intent.v2`;
older cached classifications are rechecked when their routing node is resumed.

## Patches and concurrency

A patch is `{action,target}` for delete; replace adds `content`; insert adds
`content,position`; move adds `destination,position`. Unknown or missing fields are
rejected. `position` is `before` or `after`. All selectors bind to the immutable base:

- Heading node ID, or `{id: node_id}`. ATX/setext headings exclude fenced code;
  their ranges contain descendants through the next same/higher heading.
- `{text: exact_original_text, occurrence?: one_based_index}`. Repeated text
  without a unique occurrence is ambiguous.

A direct patch may only introduce replacement text explicitly supplied in the current
instruction (unchanged surrounding text is excluded from this check). Generated
wording belongs to `transform`, with the same bounds and patch validators.

Apply 1–64 patches atomically. Overlaps, stale selectors and transform edits outside
`targets` fail. Unaffected text is preserved exactly, including formatting.
Cross-references and tables of contents need separately selected patches; there is
no hidden global renumbering or document rewrite. Deleting the whole document is a
valid empty result.

Core owns the canonical document and revision. Conversation and inline-edit paths
share `creation_history::matches_document_base`, checked again inside each commit
transaction. Inline editing retains its existing explicit-action protocol and
conditional commit/undo adapter; it does not add a language-routing stage.

## Durable execution

Desktop sends stable `instruction_id` for retries. Core derives `operation_id`,
persists the original instruction, base revision/document, checkpoint, event sequence
and result, and provides `operation_context` to Sidecar. `resume_operation_id` resumes
an existing operation in that session. Core does not trust model-supplied checkpoints.

`operation.checkpoint` is saved before each node and after completion. An external
`model.request` uses `request_id`; the reply must include the matching
`model_request_id`. Core restores its checkpoint rather than the browser's copy.
A new attempt resumes at the saved cursor with completed outputs preserved.

If a downstream node fails but a document result exists, Core commits it as `partial`
and preserves the checkpoint before the first failed node. A continuation completes
that operation; the original document remains available for undo. If interrupted
before the first checkpoint, the persisted original instruction is interpreted.
Legacy histories without operation rows expose unanswered user turns as candidates;
multiple candidates require interpretation/clarification, never a guessed workflow.

Completion and document commit share a transaction. Duplicate completion returns the
stored result without a second revision. A changed base is never overwritten. Late
browser progress snapshots cannot replace canonical content or downgrade a terminal
state. The final browser save acknowledges `committed_operation_id` and merges chat
metadata, rather than committing the document again. A per-session lease prevents
concurrent execution phases. Current creation resource tools are reads; future
external side effects need an explicit idempotency contract before replay is allowed.

Checkpoint events are stored separately from the visible trace. Token deltas and
model prompt bodies are omitted from that trace. Runtime credentials are not fields
of the serialized LoopState.

## Errors

`CREATION_OPERATION_INVALID`, `CREATION_CAPABILITY_UNAVAILABLE`,
`CREATION_TARGET_MISSING`, `CREATION_TARGET_AMBIGUOUS`, `CREATION_PATCH_OVERLAP`,
`CREATION_PATCH_OUT_OF_SCOPE`, `CREATION_BASE_CHANGED`, `CREATION_RESUME_MISSING`,
`CREATION_MODEL_RESULT_STALE`. Existing selection-edit error codes remain compatible.

失败保存后的恢复基线：兼容客户端在 `run.failed` 后把已生成候选保存为下一文档版本。恢复入口仅当操作与文档均为 failed、版本恰好增加一次、正文与 Core 持久 checkpoint 的 `current_document` 逐字相等且非空时同步操作基线；原始撤销正文和断点保持不变。其他正文、版本或生命周期变化继续返回版本冲突，不覆盖用户后续编辑。

进度快照仅可写入对应 `progress_epoch`。缺省 epoch 只兼容尚未启动新 run 的旧记录（epoch 0），不能穿透已有 run 的隔离。完成、失败或取消后的迟到进度不得更改终态与正文；相同 epoch、相同终态仍可补齐会话、执行轨迹与耗时。重新运行必须由 `start_progress` 递增 epoch；正文保存、手动修改继续使用正式文档保存/操作接口，不通过进度快照替换已保存正文。
