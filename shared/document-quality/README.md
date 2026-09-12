# Document source quality v2

Summary metadata is a separate derived source. Migration 124 adds nullable
`summary_source_snapshot_id` without backfilling guessed provenance. A document
with a source head exposes its summary to retrieval only when the binding equals
the currently verified head. Missing, stale or invalid bindings are suppressed
in candidate queries, semantic inputs and final materialization; the stored
summary is preserved. Evaluated rejections count as `summary_version_mismatch`.
Older schemas continue rejecting unbound summaries but omit that new diagnostic
reason until Core migration, preserving their existing mismatch events. Legacy
documents without a source head retain their prior summary behavior. Publication
of new summary bindings uses `publish_document_source_summary`: the input must
carry the source snapshot ID and document revision actually read before inference.
Publication rechecks exact body, owner, complete/identity evidence, current head,
document revision and automatic/source write gates in one transaction. It binds
the summary and invalidates prior vectors atomically; rejected or delayed results
do not change the document. Migration 125 adds a durable generation worker keyed by document, source and
document revision, with fenced leases and bounded retries. It uses the P2 model
queue and validates exact source quotes before publication. Runtime/model
acceptance and coverage of other derived metadata remain outstanding.

The document collector uses `content_kind=document` on the existing browser job.
Older clients may omit this field and retain report collection. A document result
must expose `structured_data.document_body` with the selected adapter, body block
count, and quality. Body text excludes navigation and editor controls. Missing
body evidence is not a complete document. Explicit `completeness` fields must be
consumed as evidence; absent stable passes or coverage never default to success.

Paginated editors may use `document-body.v3`: two bounded complete traversals
must yield the same ordered positioned blocks, with overlapping adjacent views
and no conflicting block values. Table cells are tracked by row and column;
unmounted cells cannot erase observed values. Every observed textual body block
must be represented. A stable but incomplete subset is still partial. The result
is frozen before restoring scroll position. Other document roots retain v2.

Source URL identifies the merge target, not whether content has already been
incorporated. Only the pair (document identity, source fingerprint) coalesces a
candidate. Failed body validation must not record the fingerprint as applied.

This contract is under implementation. Historical reprocessing, version pointers,
and UI acceptance remain tracked in the source-quality proposal.

## Applied source versions and recovery

Migration 115 creates source heads and body version archives. A complete,
identity-verified, stable snapshot is applied transactionally with an archive of
the previous document. A shorter complete revision is legal. Partial/stale
snapshots cannot replace the current full version. Observed hashes are no longer
registered as applied merely because a snapshot was collected.

## Revisit observations

### Automatic artifact publication control

The existing runtime configuration gains `automatic_document_writes_enabled`
(default true) and `automatic_document_rollout_percent` (integer 0–100, default
100). These govern automatic initial documents and observation-driven updates
at the SQLite transaction boundary. Stable canonical source identity, falling
back to canonical title, determines the percentage bucket using SHA-256.
Manual edits/imports and source associations are separate operations. Paused
automatic publication returns `DOCUMENT_AUTOMATIC_WRITES_PAUSED`; bake defers
without consuming a failure retry. Migration 123 persists the pause bucket in
the retry state before advancing the watermark. Eligible paused candidates are
selected before LIMIT, independently of the watermark and sibling artifacts;
scope changes are checked again when publishing. Queue health uses the same
eligibility, and successful completion clears the pending record.
Source snapshot publication continues to use its existing source-write controls.

### Passive candidate evaluation audit

`document-candidate-quality.v1` records each semantic evaluation stage
(`precheck`, `extraction`, `persistence`) independently of source application.
Events contain local run/timeline/document/capture IDs, quality rule version,
eligibility flags and a fixed reason code. They contain no text, URL or model
explanation. The run/timeline IDs join the existing candidate skip decisions.
Passive input character counts are explicitly input counts, not verified body
counts. Body/block/exclusion/redaction measurements and snapshot ID are null
when the passive collector has not supplied that evidence; coverage stays
`unverified`. An eligible candidate is not evidence of complete collection.
These local runtime events are excluded from asset import and cannot be rebound
to another database's document IDs.

Migration 117 stores observations independently of applied fingerprints. Queue
states are `pending`, `running`, `completed`, and `blocked`. `completed` means a
current complete snapshot was checked; it does not assert that historical capture
text was merged. `checked_snapshot_id` records that source check. Artifact audit
states are `pending_source_refresh`, `source_checked`, and
`source_refresh_blocked`; enqueueing never increments the document-created count.

The worker coalesces pending observations per document, leases at most one job
globally, and leaves observations arriving during a job for a later check. An
expired lease may be recovered; stale completions cannot acknowledge another
lease. Three attempts exhaust an observation. Browser disconnection waits without
claiming work. Automatic checks obey `never`, URL/identity gates, and a 30-second
cooldown; old reused snapshots cannot acknowledge new observations. The existing
manual refresh remains a separate explicit one-shot action.

`ai-sidecar/scripts/replay_document_updates.py --db PATH --document-id ID` audits
historical metadata-only skips; `--apply` schedules selected observations through
the durable source-refresh queue (legacy databases use the bake retry lane) and
writes a backup of prior retry/observation records. It does not rewrite
capture timestamps or reset the global watermark.

`ai-sidecar/scripts/restore_document_body_version.py --db PATH --version-id ID`
previews an archived body restore. `--apply` first backs up the current record,
restores content fields, preserves new source associations and history, and clears
complete-source claims. Shared shell filtering still excludes invalid restored
content from retrieval. Both tools default to read-only previews.

For a known reliable historical source, explicitly pass `--source-snapshot-id ID`.
The preview and apply both require the selected snapshot to belong to the document,
match its current source URL and the archived body exactly, have a matching SHA256,
and be complete, identity-verified, untruncated and non-shell. No snapshot is inferred
from a title or a similar body. A successful apply restores the head atomically with
the body and invalidates the replaced vectors. The archived summary binding is
restored only if it already names that exact snapshot and has a generation version;
otherwise the summary remains unverified. The pre-restore backup includes the prior
source head under `_source_head`. Omitting the option retains historical-only restore
behavior. A historical source remains historical in freshness; restoring it does
not claim a new visit to the source URL.

New body archives preserve `summary_source_snapshot_id` and
`summary_generation_version` in the same transaction as source replacement.
Snapshot import remaps an archived summary's source ID only when the imported
snapshot belongs to the archive's document and matches its historical body.
Missing or mismatched bindings are cleared while the archived text is retained.
Legacy archives without binding evidence remain unverified; import never infers
that a matching title or timestamp authorizes a summary.

Summary scheduling reports `summary_schedule/head_invalid` for an enabled,
in-scope document awaiting a summary whose applied source is invalid. Each actual
scheduling assessment contributes one occurrence; this is not a distinct-document
count. Paused and rollout-excluded documents are not assessed or counted. Invalid
sources never acquire a generation lease, and an audit sink failure does not let
them through or prevent another valid document from being scheduled.

### 回访任务展示契约

文档响应新增可选 source_collection：state 为 pending/running/blocked/completed，attempts 为已尝试次数，next_attempt_at_ms 为最早可重试时间，last_error 为稳定原因，updated_at_ms 为状态更新时间。它只表达观察检查任务，不改变正文完整性或来源版本。展示优先级为 running、pending、blocked、completed；同状态取最近更新，防止新成功记录掩盖未处理观察。无记录返回 null，旧客户端忽略新字段。重新获取沿用既有一次性手动入口，成功检查只确认开始前的观察。

### 编辑已校验正文

通用文档编辑若改变正文或规范化来源身份，原子移除当前source head，将覆盖改为unverified，清空旧摘要/结构化内容/提炼提示；原始不可变来源快照仍保留。正文、标题、来源身份或删除状态变更时，旧artifact索引登记与Qdrant删除队列在同一事务失效。只改标题或其他元数据不解除正文来源关联。观察/模型写入继续要求旧版本匹配且无source head，不能借编辑路径覆盖已校验原文。

### 未识别来源的比较

两个canonical identity均为空不能证明同一来源。快照应用在此情况下仅允许完全相同的合法HTTP(S)URL（有host且无内嵌凭据）；同名文档匹配若双方均有URL，在无法识别时也必须严格相同。缺少URL的一方仍可使用既有保守标题兜底。该门禁独立于采集器identity_match声明。

### URL身份v2

身份以document-url-v2:标记版本。协议、路径大小写、尾斜杠、未知查询参数及顺序均保留；URL解析器统一host大小写和默认端口。/d/home/、/s/home/、/k/home/分页编辑器仅忽略section及ro=true/false，并去除章节fragment；其他来源的fragment保留。未知参数from等不得因被猜测为追踪参数而删除。Rust和Python共同执行url-identity-v2-cases.json语义夹具。迁移118在事务内归档旧identity并重算活动记录，重复身份只选择一个索引所有者，其余原记录仍保留。别名用于审计，不用于绕过v2判定；正文、ID及引用不变。

普通文档向量写入同样要求文档存在、未删除及版本时间匹配；生产批处理额外传原正文SHA256，避免同毫秒正文变动。source head的有无必须匹配生成时的来源绑定，Qdrant前后均验证，拒绝的孤儿点进入删除队列。来源快照原文在详情按plain text保留换行，不能把字面原文再解释成Markdown结构。

### 资产快照的历史引用

原始 capture 已按保留策略删除时，导出副本为仍被外键或已声明 JSON ID 数组引用的 capture 保存脱敏占位记录：时间为 0，event_type 为 snapshot_ref_missing:<原始 ID>，所有正文/屏幕字段为空，并标记敏感及已脱敏。占位符不是新增采集证据，不能冒充发生时间或恢复已删除正文。再次导出保留该原始引用标记，避免多个缺失引用因内容相同而合并。源库不因导出而补写占位记录。

导入时，主键同时为所属记录外键的表必须使用映射后的所属 ID，不能为冲突重新分配独立 ID；已有本地所属状态优先保留。源 ID 到目标 ID 的映射使用重映射前的源键，供后续引用使用。真实完整资产恢复仍需单独验收，不能由小型夹具代替。

### 来源检查时间

来源成功/部分覆盖和已记录失败检查都附带 started_at_ms、finished_at_ms 和 execution_ms。前两项是墙钟时间，execution_ms 使用单调时钟，覆盖检查开始至构建审计证据的实际耗时；不包含随后正文应用、摘要或索引重建，也不包含观察队列等待。旧审计没有这些字段时视为未采样，不按 0 参与耗时统计。观察排队、重试和全任务耗时需独立记录，不能用这个字段冒充端到端耗时。

### 扩展取消执行槽

扩展 0.2.4 收到 cancelled_job_ids 时先关闭对应标签，再使挂起的任务等待返回 SOURCE_REFRESH_CANCELLED 并释放执行槽。取消后的迟到结果只由原等待处理，不再次发布；迟到进度不发送。连接断开后，旧执行的完成不能向新连接发布结果或释放新任务的执行槽。此协议不保证操作系统一定接受标签关闭，真实关闭结果必须另行验证；Core 拒绝取消后的结果继续有效。运行验收需核对实际心跳 extension_version，源码 manifest 版本不能代替已加载版本。

资产导入明确排除的运行日志不具有可迁移身份。若业务表到该日志表的外键声明为 ON DELETE SET NULL，导入副本将该引用置空，不能按数字 ID 误连目标库已有日志；原库日志及引用均不修改。未声明可置空的关系仍按约束失败处理，不擅自删除业务记录。

### 调度尝试统计

迁移 120 新增仅供本地运维的 bake_document_refresh_attempt_metrics，每个 worker 租约一行，以 lease_id 唯一关联本次领取。observation_count 记录本批合并的观察数；不能用该数乘执行次数。queue_wait_ms 是最早合格观察从可领取时间（max(created_at,next_attempt_at)）到实际领取的间隔，包含调度暂停造成的等待；scheduled_retry_ms 是本次结束安排的退避时长，不等于后来真实等待。execution_wall_ms 是领取至结束的墙钟间隔，负差截为零，和采集器单调时钟耗时独立。中断的真实结束时间未知、暂停时尚未派发，二者耗时均为空，不参与执行耗时平均。

领取、结束和取消时统计与观察状态在同一事务更新。取消/中断后的迟到 finish 不得改写终态。attempt_no 表达本批观察的最大尝试序号，不表示每条观察已发生同样次数。此表不包含正文、URL、指纹或任意错误字符串，也不作为可移植业务资产导入。

观察 duplicate_count 从迁移启用后开始累计相同 document_id+fingerprint 的重复 enqueue；重复调用仍返回 false，不改变原状态/来源/创建时间。历史重复次数未知，不回填。健康接口 observations 中的 duplicate_enqueue_count 是窗口内创建的观察集合的累计计数，不是该时间窗口内发生的全部重复访问量，也不包含在进入队列前已去重的访问。

`observations.duplicate_enqueues_by_document` 按 document_id 分组同一创建窗口内观察的累计重复数，仅返回计数大于零的文档。窗口结束后的重复入队仍可能增加这个集合的累计值，不能解读为历史时点快照。

`prequeue_coalescing` 单独统计 updated_at_ms 位于窗口内、当前状态为 skipped/document_url_already_queued 的候选审计行；同一候选重复 upsert 不增加行数。sources 以规范化来源身份的 SHA256 分组，旧记录缺少身份时以 null 表示未知，不推断来源。total 是候选数，不是刷新执行数。此阶段与队列重复数可能覆盖同一次访问，不得相加当作独立访问总数。接口不返回原始 URL、指纹或正文。

资产恢复中的 current head 是经校验的投影，不要求复制历史失配指针。历史快照及正文归档仍保存；新导入文档原来声称 fresh_complete、但没有通过校验的 head 时，降为 historical_only/unverified，原因 SOURCE_HEAD_UNVERIFIED。只更新本次新插入文档，不修改目标库已有文档。源数据库保持原样。所属键同时为主键的记录按所属键区分，不得因其他状态字段相同而合并不同所属记录。


### 来源错配事件（migration 121）

`document_source_mismatch_events` 只记录 document_id、组件枚举、原因枚举、期望/观测来源快照ID（无效类型为NULL）、occurrences、observed_at。每次消费调用将相同事件合计后，在原事务结束后独立提交；occurrences是拒绝/降级判定次数，不是去重文档数或浏览器任务数。重复查询重复发现同一问题会再次计数。

当前生产接入点为 RAG 文档向量物化、持久文档向量写入和创作关键词/语义候选读取。创作仅统计本次实际候选中有原始head却无有效来源投影的行；其他文档不因库内失配而被额外扫描计数。诊断写入失败不能使被拒绝的来源通过，并记录只含次数的写入失败日志；旧库无表时不自动建表，不声称历史错误已被统计。

健康接口 version_mismatches 按 observed_at 时间窗口聚合 component/reason/occurrences，不返回文档ID或快照ID。此表为本地运行诊断，不导入/导出为用户资产；回滚/导入旧资产不重放历史错配次数。尚未接入的背景调度及其他门禁不可被该字段隐式代表。

后续接入 vector_schedule：每轮按 limit 有界检查正文/结构长度符合候选条件、且索引缺失/过期/模型变化的失配来源；source_versions_only 的检查也计为一次独立判定。拒绝候选不占有效调度名额；该计数不是库内所有失配文档数量。

提交暂停契约：SOURCE_WRITES_PAUSED表示采集后来源写入被当前配置拒绝，不可确认新观察已应用；SOURCE_REFRESH_PAUSED表示入口暂停。两者均保留pending且不消耗失败预算，后者无执行耗时，前者保留实际耗时。快照/正文/成功状态提交必须在同一SQLite事务内读取source_writes_enabled。

### Long-document summary generation

The `document-summary.v1` response contract also supports hierarchical generation.
Every nonblank source block enters an input batch; payload estimates use the bake
safety factor and reserve prompt overhead. Each batch produces a bounded summary
and validated local block IDs. Reduction levels receive every preceding summary,
retain source-ID provenance, and resolve final evidence quotes from the original
body rather than quoting generated intermediate text. No head/tail truncation is
allowed. Invalid evidence, cancellation, failed batches, or nonconvergent budget
reduction prevent publication of the whole result. Calls remain under the P2 queue
execution deadline; this does not promise arbitrarily long documents complete in
one attempt. Intermediate summaries are memory-only and raw trace capture stays
disabled. The existing source/revision/lease publication checks remain mandatory.

### Explicit replacement of unbound summaries

Background jobs leave non-null summaries untouched, since legacy text may have
been written by the user. For a verified current source with an unbound summary,
`summary_status.can_regenerate` enables an explicit rebuild action. POST
`/api/bake/documents/:id/summary/regenerate` requires `expected_updated_at` from
the displayed document. Stale requests, mismatched bodies and already-bound
summaries make no change. In one transaction, migration-127 summary history
archives the old summary/provenance/revision, clears the active summary, advances
the revision, invalidates vectors and supersedes older jobs. Archive failure rolls
back the entire operation. The usual worker then processes the new revision under
existing pause/gray/lease checks. History is content, remains in complete backups,
uses UUID identity for repeated import, and cascades with hard document deletion;
its JSON provenance is historical evidence, never an active source pointer.

When a verified source replaces a document body, template-derived
`style_phrases`, `replacement_rules`, and `applicable_tasks` are invalidated in the
same transaction as summary/sections/tags/prompt context. Their previous values
remain in the complete document record archived by the source switch. Source
capture links and user history are preserved; partial or rejected candidates do
not invalidate the currently applied metadata.

Document ingestion diagnostics must not interpolate source URLs, page titles,
model-controlled review labels, or raw response decode errors. Association,
coalescing and merge events retain internal timeline/document IDs and fixed
reasons. Bake task failure persistence uses the typed API error category (and
upstream status/code) rather than arbitrary error messages. Response decode
logging records the decode/category flag only. This contract does not sanitize
historical logs retroactively.

Python bake inference never persists model response content to diagnostics,
including callers supplying the legacy `capture_trace=True` keyword. Raw output
remains in memory solely for parsing and bounded recovery; returned diagnostic
previews are null. Its usage tracker retains token/latency/status metrics while
omitting response previews and replacing exception text with `INFERENCE_FAILED`.
The dedicated bake error log admits only known event labels, numeric counters,
validated internal caller IDs and known completion reasons. API failure logging
omits tracebacks/request metadata and records exception class only. Other tracker
callers retain their existing behavior unless they explicitly select the same
content-free policy; this does not purge historical logs.

Migration 128 adds `summary_write` to the common version-mismatch diagnostics.
Finishing an owned summary job rejected against a changed source records one
`summary_version_mismatch` event with expected/observed snapshot IDs. It checks
body equality, completeness, identity and revision, not just head equality.
Unchanged-source lease expiry remains retryable and emits no source mismatch;
repeated finishes of a released lease emit no duplicate event. Audit failure
cannot permit stale publication or keep stale input retrying. Events contain
IDs, fixed codes and counts only; health aggregates include this component.

Source-check diagnostics use `body_character_count` for the Unicode scalar count
of the persisted redacted snapshot, including whitespace. It is independent of
collector-reported character counts and byte offsets. A failed collection has
unknown body/block/exclusion/redaction/coverage measurements, represented as
explicit nulls rather than zero. `substantive_block_count` counts aligned body candidates accepted by the existing
shell rejection rule, identified as `substantive_block_rule=aligned-non-shell.v1`.
It describes structurally selected, non-shell text, not semantic correctness.
Each reference exposes `body_candidate`; table cells and short headings are not
rejected merely for length. Missing blocks, any unmatched block, or the block
limit being exceeded make the aggregate null. The count covers supplied aligned
blocks only, never unseen pages; coverage remains a separate gate.

### Restore receipts and guarded undo

The maintenance restore CLI writes the before-image and a
`document-restore-receipt.v1` file with mode 0600 before committing. The receipt
contains backup SHA256, canonical before/after document-plus-head hashes, the
resolved database path, and `reverse_argv`. It contains no body text; its adjacent
backup retains the existing private before-image. `prepared` records intent,
not proof of commit. Only a successful CLI result reports `applied=true`.
Failure to write the receipt rolls back SQLite changes.

Preview a reverse operation with `--db PATH --undo-manifest RECEIPT`; add
`--apply` to execute. Version and undo inputs are mutually exclusive. Undo
requires the same database, intact backup, and an unchanged current document
and head. It restores only the existing body-field allowlist, preserves source
links/history, and applies normal snapshot owner/body/hash/identity/coverage
checks before restoring a former head. Invalid old heads are rejected, never
silently blessed. Indexes are invalidated and rebuilt, not revived from stale
points. Undo creates its own receipt and before-image; repeating the old undo
after state changes is rejected. Historical receipts are not retroactively
invented for earlier repairs.

Rejected source reads retain measurements already obtained before rejection:
source character count, filtered body character count, removed characters and
redaction fraction. This applies to shell/empty, terminal-page and identity
rejections after privacy filtering. Transport failures without a body retain
unknown measurements. Neither case creates a verified snapshot or infers block
coverage from counts; reason and failed coverage remain authoritative.

Cancellation after body assessment records a source check with no snapshot ID,
`coverage: cancelled`, `reason: SOURCE_REFRESH_CANCELLED`, and the measured
coverage in `assessed_coverage`. Existing typed, redacted quality measurements
are retained; raw body text is not stored in the check. This check changes no
document body, summary, source head, refresh status or freshness timestamp.
Cancelled assessments count toward total checks but not complete/failed checks.
Cancellation before any body assessment does not invent quality measurements.

When a normalized source fingerprint reuses a snapshot, snapshot-bound audit
hashes, body character counts and block byte offsets are rebuilt against that
snapshot's retained text. Incoming character count and filtering/timing metrics
remain observations of the new read. Whitespace-equivalent input must never
attach offsets into its transient representation to an older snapshot ID.

Refresh status and last-read coverage/count/truncation fields describe the new
read, even when fingerprint deduplication returns an older complete snapshot.
A partial revisit leaves that complete snapshot and its bound summary available,
but records `fresh_partial` and partial last-read coverage. Historical snapshot
quality is never used to upgrade the latest observation's coverage.

The browser claim timeout measures idle extension time, not time spent waiting
behind another claimed task. Busy queue time still consumes the request's total
deadline. Once the extension is free, a task that remains unclaimed fails with
the existing unresponsive code; cancellation and total-timeout cleanup retain
their existing contracts. This requires only a Core update, not new extension
permissions or a larger page execution budget.
