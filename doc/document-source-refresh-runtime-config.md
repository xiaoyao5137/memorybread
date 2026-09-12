# 文档来源刷新运行配置

配置保存在现有 `user_preferences` 的 `runtime.document_source_refresh` 键中；通过 `PUT /preferences/runtime.document_source_refresh` 更新，请求体的 `value` 为 JSON 字符串。缺失字段使用下表默认值，未知字段、错误类型和超限数值返回 400，写入前完成校验。

| 字段 | 默认 | 允许值及含义 |
| --- | --- | --- |
| enabled | true | false 阻止新的手动和自动文档来源刷新 |
| automatic_enabled | true | false 暂停自动刷新，仍可显式手动刷新 |
| execution_seconds | 60 | 来源采集预算，1–60 秒；扩展通信等待另留返回结果时间，Apple Events 到期主动终止采集脚本 |
| max_steps | 20 | Chrome 扩展遍历步骤预算，1–30；Apple Events 就绪轮询也受此上限限制，不代表完整遍历 |
| poll_seconds | 15 | 后台观察队列轮询间隔，1–300 秒 |
| max_attempts | 3 | 单条观察最多尝试 1–5 次，包含首次 |
| retry_seconds | [30,120] | 1–4 个重试间隔，每项 1–3600 秒；后续重试复用最后一项 |

示例请求体：

```json
{"value":"{\"enabled\":false}"}
```

配置在领取任务前及刷新入口重新读取；暂停不删除观察、正文或历史，也不修改单文档长期策略。入口发现暂停时不启动浏览器；领取后才发现暂停的任务退回 pending，不消耗尝试次数。运行中的任务使用启动时预算，可通过 `POST /api/bake/documents/:id/refresh/cancel` 单独取消；进入提交阶段的任务返回 finishing。

恢复时写回原配置；不要用 `{}` 覆盖用户自定义预算。关闭自动刷新不解除单文档 never 策略，手动刷新沿用显式单次权限和冷却机制。

Apple Events 兼容采集共享取消标记和执行截止时间，等待采集互斥锁期间也检查取消。脚本中断后回收子进程，清理窗口另有最多 5 秒预算；清理不允许继续采集或提交正文。真实 953 验证中，取消后约 0.207 秒结束并回收已观察到的脚本进程；1 秒预算约 1.214 秒返回 SCRAPE_TIMEOUT，正文及来源 head 不变。

当前边界：兼容采集仍不能仅凭就绪轮询证明全文覆盖。扩展 Service Worker 的物理标签取消、规则版本选择、灰度范围和全链路监控仍以总验收清单为准，不能仅凭此运行开关宣称 ROLL-002/ROLL-005 完成。

## 本地来源健康统计

`GET /api/bake/documents/source-health` 默认最近 24 小时；可传 `since_ms` 毫秒时间戳，范围必须在最近 31 天内。接口只读，返回 document-source-health.v1。

- checks：窗口内的来源检查总数、complete/partial/failed 数量、预算或超时数量；timed_samples 是具有合法数字耗时的样本数，mean_execution_ms/max_execution_ms 只使用这些样本。没有样本返回 null；不能把历史缺字段按零补齐。检查耗时范围见共享契约，不含正文应用与派生重建。
- observations：窗口内创建的观察当前 pending/running/blocked/completed 数量，retried_observations 为 attempts>1 的观察数。多条观察可共用一次浏览器任务，因此它不是浏览器请求重试总数。oldest_pending_age_ms 为该观察集合中最早 pending 的累计年龄，包含等待、退避和暂停，不冒充首次排队耗时；没有 pending 时为 null。

检查以 checked_at、观察以 created_at 划定窗口；窗口外仍未处理的老观察不包含在此集合。数值可计算各分类占比，但 completed 仅代表观察检查完成，不代表正文已应用。接口不返回正文、URL、指纹或任意错误消息。每任务排队/重试时长、重复观察计数和版本错配统计仍需另外补齐，不能以此接口代替全部 OBS 验收。

健康统计新增 attempts：按租约计 claimed/running/completed/retry_scheduled/blocked/cancelled/interrupted/paused_before_dispatch，包含 mean_queue_wait_ms、finished_timing_samples、mean_execution_wall_ms、scheduled_retry_ms。它是 worker 调度尝试统计，手动刷新没有 worker 租约，仍计在 checks 中。时钟和排队/退避口径见共享契约；暂停及中断不填造执行耗时。observations 增加 duplicate_enqueue_count，表示本集合自迁移启用后的累计重复入队数，历史次数及队列之前的去重未纳入。按文档保存的原计数可用于单来源诊断。运行部署和真实任务验收通过前，不宣称这些新字段已在当前 Core 生效。

健康统计新增 `version_mismatches`：按观测时间窗口分 component/reason 汇总 occurrences；不是当前失配文档库存数。接入范围与事务/失败语义见共享契约，最新代码仍需部署验收后才视为当前运行能力。


## 来源写入暂停（新增，部署验收中）

`runtime.document_source_refresh` 新增 `source_writes_enabled`，默认 true。false 阻止新的采集任务，同时在快照保存、完整来源正文切换和刷新成功状态写入的 SQLite 事务内再次检查。已经取得采集结果的任务也不能越过关闭后的提交点；开关生效前已提交的历史快照保留，不删除历史。

提交点发现暂停返回 `SOURCE_WRITES_PAUSED`；入口尚未启动采集时仍返回 `SOURCE_REFRESH_PAUSED`。队列保留pending并退还尝试次数，不安排失败退避。提交点暂停保留已经发生的执行耗时，健康统计 `paused_after_dispatch` 与 `paused_before_dispatch` 分开计数。诊断失败原因和队列状态仍允许记录。

恢复应还原原配置，不覆盖其他预算字段。开关不改变手工编辑、来源隔离、历史版本恢复或消费质量门禁。当前覆盖来源刷新写入链路；普通提炼写入、规则版本选择和灰度范围的完整回滚控制仍依原方案继续验收，不能将此字段等同于所有新规则写入的总开关。


## 来源刷新灰度与规则版本（新增，回归中）

- quality_rule_version：默认 document-quality.v2，必须是当前运行二进制支持的规则版本。当前仅支持v2；未知/未安装版本返回400，不回退旧规则。采集检查使用同一规则版本常量。
- rollout_document_ids：缺失/null为全部来源文档，[]为全部暂停；正整数ID列表为仅启用这些文档，最多1000项。此范围不包含站点/业务关键词写死分支。
- 后台在领取SQL中筛选范围，不让范围外较早观察阻塞范围内观察；范围外观察不因领取而增加尝试次数。手动刷新入口也受范围约束。
- 快照保存、正文切换和成功状态事务重新读取范围；运行中移出范围等同于提交暂停，正文仍受保护。已有可靠内容的读取和隔离策略不变。

当前灰度范围针对来源刷新链路；首次自动提炼/普通观察合并等入口仍须按总方案补齐统一控制，不能把上述配置宣传为整个文档产品的灰度总开关。

## 普通自动文档写入（新增，回归中）

- `automatic_document_writes_enabled`：默认true；false阻止普通自动建文档及观察合并的提交。
- `automatic_document_rollout_percent`：默认100，允许0–100整数；按规范化来源身份（无URL时规范化标题）的SHA-256稳定分桶，0暂停全部自动文档，增大比例保留已纳入身份。
- 入库与合并事务重新读取配置，迟到模型结果不能绕过暂停。返回固定原因`DOCUMENT_AUTOMATIC_WRITES_PAUSED`，候选进入持久化deferred，不增加失败重试次数。
- 迁移123使用现有重试队列保存暂停分桶，保存成功后才推进水位；后续候选继续。恢复查询在LIMIT之前筛选范围，零失败次数及已存在兄弟产物都不能吞掉待补文档。提交仍再次检查配置，成功后清理待办。该调度修改仍在回归，需实际验收后才视为运行能力。
- 手工编辑/手动沉淀/导入、纯来源关联和隔离读取保持各自契约；来源快照提交仍由`source_writes_enabled`控制。停止两类正文自动发布需同时关闭两个写入字段，并保留其余原配置。
- 最新源码仍需完成运行验收；不得只用存储事务测试宣称全链路灰度回滚已完成。


## 摘要重建任务（迁移125，尚未部署）

summary_execution_seconds默认300，允许1–1200秒，是后台摘要HTTP调用预算；任务租约额外保留30秒。摘要任务复用enabled、automatic_enabled、source_writes_enabled、automatic_document_writes_enabled、文档ID/百分比灰度、max_attempts和retry_seconds；不依赖浏览器连接。runtime.capture_enabled=false同样阻止领取和发布，恢复后保留待处理任务。

当前完整来源head且summary为空的文档会被后台发现，以文档ID、来源快照ID、文档版本为任务身份。持久租约防止重复领取，超时可恢复，次数耗尽blocked；新来源版本有独立任务。用户已有非空摘要不被该worker自动覆盖。发布时重新检查来源/版本/暂停和租约，成功与任务completed、摘要绑定和旧向量失效在同一事务完成。summary_generation_version记录document-summary.v1。document_summary_jobs属于运行时调度状态，快照合并不导入。

模型调用使用现有P2 bake队列，仅输入正文与来源版本；正文超出单次预算明确blocked（SUMMARY_INPUT_BUDGET），当前尚不支持分层长文摘要。返回的引文必须逐字存在于本次正文，但这不等于语义准确性已验收。不得把自动发现/任务完成冒充全文质量或模型内容正确性的证明。


## 摘要失败后的显式重试（迁移126）

POST /api/bake/documents/:id/summary/retry仅重新排队当前有效来源、版本未变化、摘要仍为空且已blocked的任务。返回queued=true表示已记录重试意图，不代表已开始推理或成功生成。没有可重试任务时queued=false；不会重置正在运行的租约或覆盖用户摘要。恢复后仍遵守全部暂停/灰度门禁。

重排队前，同事务向document_summary_retry_events追加旧任务的完整调度记录JSON、原尝试次数、状态、错误和请求时间。审计失败则不重置任务；重复请求幂等。调度记录与重试历史不参与快照合并，以免在另一台设备执行旧任务。

模型选择的依据数量由本次正文块集合限制，重复编号去重；不能依赖固定6条上限，否则可能拒绝实际有效的跨章节摘要。编号必须有效，程序按编号恢复原文，Rust复核逐字出处及引文总长度不超过本次正文。摘要仍限500字；引用原文有效不等于摘要语义已通过真实验收。


## 文档摘要状态展示

文档列表与详情增加summary_status：pending/running/blocked/ready/unverified及paused。ready必须由当前有效正文head和摘要绑定证明；过期详情版本不会获得新版本ready状态。已有非空但未绑定摘要展示未核对。该状态与来源采集状态独立，摘要失败不改变可靠正文完整性。

详情打开后读取最新记录；pending/running时每5秒更新，完成、关闭或进入编辑后停止，迟到请求不能覆盖编辑内容。blocked提供显式重试入口，调用审计重试API后重新读取真实状态；不根据按钮点击直接假设任务已运行或完成。
