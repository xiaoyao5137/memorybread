# 文档正文质量修复验收清单

状态：验收中，尚未最终交付。范围以 document-source-quality-proposal-2026-09-11.md 为准；逐轮变更、运行与备份证据见 document-source-quality-implementation-2026-09-12.md。本表不以 953 个案成功替代通用要求。

以下表格保留早期验收快照，当前收尾边界见 [交付核对](document-source-quality-delivery-review-2026-09-12.md)及本文后续逐轮证据。

| 要求 | 当时证据与结论 |
| --- | --- |
| FR-001 正文块与界面分离 | 语义块及定位块采集已实现；953 最近成功检查 3：v3、36 块、末尾已覆盖。扩展 41 项通过。 |
| FR-002 实质性与覆盖分离 | 共享 shell 夹具、partial/complete、两次定位遍历；分页预览不得回退成 complete。953 检查 3 独立保留覆盖证据。 |
| FR-003 凭据过滤边界 | Rust 字段分隔/边界回归通过，真实 953 脱敏数为 0；普通商家账号文本保留。 |
| FR-004 拒绝外壳入库 | 确定性恢复及共享质量门已实现；真实旧 953 隔离恢复后仍判 shell。 |
| FR-005 原子版本更新 | source head/body versions、partial 保护、较短完整版本与并发校验已实现；版本专项通过。 |
| FR-006 有界补全 | 指纹观察队列、租约、三次重试、never 策略、冷却、旧任务不得确认新观察；专项通过，五条真实漏更新已重放。 |
| FR-007 详情状态与重取 | r15 真实 UI 已复核 953 正文、末尾引用及取消后保留原文的可读说明；前端详情 20 项和类型检查通过。成功重取后的最终状态仍待收尾。 |
| FR-008 统一消费范围 | 创作片段范围、UTF-8 哈希、缩短时重算、partial 不支持全文；咨询读事务保证版本一致。联合 402 项通过；r12 咨询与创作均返回 953/61。 |
| FR-009 保留历史与恢复 | 隔离恢复验证历史、收藏、关联及其他文档不变；真实全库导出/导入已通过，/tmp/mb-document-head-projection-real-import.log，保留有效head及历史记录。 |
| FR-010 派生版本绑定 | 正文、摘要、向量失效与 CAS；创作绑定快照及正文哈希，跨步骤更新不受旧高分阻挡。真实 953 三条索引时间戳与 head 相同。 |
| AC-001 旧采集重放 | 953 历史五条 skipped 观察重放，原审计保留；旧外壳不能通过质量门。 |
| AC-002 短文/表格/代码/隐藏提示 | 扩展与共享质量夹具通过；完整新版本无需字数增长。 |
| AC-003 隐私负向回归 | 凭据与普通账号文本、无换行/字段边界、Unicode 重叠区间计数回归通过；本轮全量 Rust 617 passed、1 ignored。 |
| AC-004 延迟/虚拟页/身份 | 虚拟分页、跳块、冲突、缺尾、预览与 URL 身份测试通过；真实 v3 两遍一致。 |
| AC-005 新旧版本及重复任务 | 版本、队列、CAS 与幂等专项通过；不以片段无依据拼接制造全文。 |
| AC-006 UI 成功/失败及保护 | 前端已有成功/失败回归及真实正文显示；最终运行时复核待完成。 |
| AC-007 空壳向量/别名/partial | 共享 shell、URL v2、旧向量过滤、partial 片段范围已回归；需在最终报告明确引用范围与历史背景限制。 |
| AC-008 隔离恢复 | /tmp/mb-document-real-restore-acceptance.json：dry-run 不变、shell 隔离、索引失效、可靠版恢复、历史及其他文档不变，已通过。 |
| AC-009 摘要/向量并发 | 源正文切换清旧派生字段；向量写前/事务内版本校验；咨询 WAL 并发测试、跨步骤引用更新测试通过。 |
| AC-010 953 真实端到端 | 成功来源检查3/head61/1139字符；r20咨询含全文、创作第1且hash匹配。r19 UI已复核最新超时及正文末尾。扩展0.2.4成功重取/取消仍待验收。 |
| NFR-001 Python 3.9/共享夹具 | 已改 Python AST 3.9 检查及共享身份/质量夹具通过。 |
| NFR-002 诊断隐私 | 新检查只持久化白名单统计与脱敏正文范围/哈希；原始块文本与属性不入检查记录。 |
| NFR-003 预算与取消 | 扩展预算/队列配置已接通并经专项 26 项验证；r13 实际 running 任务取消后正文/版本不变，r15 总开关阻止手动刷新已验证。r16 Apple Events 真实取消及 1 秒预算均通过，脚本回收、正文不变；扩展物理标签即时关闭仍待验收。 |
| NFR-004 增量兼容 | 可选字段、缺失状态不默认完整、无有效快照 ID 不升级 fresh；旧库回归通过。 |
| OBS-001 评估审计 | 新增正文根排除项数（明确 final_body_root 范围）、实际脱敏字符数/比例，专项通过；失败检查保留拒绝原因。r23普通候选precheck/extraction/persistence审计及健康汇总已加载，640项全量和最终92项专项通过；r24实际16条三阶段事件的任务/时间线关联、白名单字段、未知覆盖和同窗口健康汇总均一致；仍需核对全部入口覆盖。 |
| OBS-002 队列指标 | r20健康接口包含检查、观察及租约尝试聚合，真实预算耗时已验证；前置去重覆盖和真实worker尝试验收仍需补齐。r22实际采集后暂停检查记录33413ms；该证据不替代worker租约尝试验收。 |
| OBS-003 版本错配 | r20已接入RAG、创作、向量写入/调度事件；文档16真实错配2次与健康汇总相符，文档未返回。摘要/其余版本门禁的完整覆盖仍需核对。 |
| ROLL-001 契约及影子评估 | 契约及隐私修复已落地；旧外壳明确可识别，不恢复已脱敏删除文字。 |
| ROLL-002 门禁及灰度 | 入库、更新、消费门禁已有实现；r22来源刷新灰度排除/空范围实际暂停、未知规则版本拒绝已通过；普通提炼入口完整范围仍待补齐。 |
| ROLL-003 结构采集/状态 | 已实测真实浏览器产品路径与队列/UI；最终 UI 复核待完成。 |
| ROLL-004 历史治理 | 重放工具默认 dry-run，写前备份；953 原 ID 恢复、历史及关联保留；未批量修改其他文档。 |
| ROLL-005 回滚 | 隔离恢复和真实全库导入已通过，停止新刷新入口开关已实测；r22真实采集中途暂停及原子失败审计已通过：SOURCE_WRITES_PAUSED、耗时33413ms、953正文/head/版本数不变且偏好恢复。普通提炼/规则灰度的完整写入回滚边界仍未完成。 |

以下为第十五轮起的历史进度记录（旧任务终态以之后补充为准）：

本轮核心证据：`/tmp/mb-document-real-restore-acceptance.json`、`/tmp/mb-document-r12-creation-live.json`、`/tmp/mb-document-r12-rag-live.json`、`/tmp/mb-document-r13-cancel-running-live.json`、`/tmp/mb-document-r15-runtime-config-live.json`。全量 Rust 已通过：`/tmp/mb-document-config-full-rust-tests.log`。实际导入当前运行句柄 16330/PID 53773，日志 `/tmp/mb-document-source-isolated-import-retry.log`；原导入因夹具误打包数据库中止，修正后的首轮 DiskFull，详见实施记录第十五轮。不得将运行中计为通过。

第十六轮补充：真实完整导入先后终态失败于 integration_import_items 和 operation_replay_queue 外键。已补全导出副本中缺失 capture 引用占位，并修复主键兼作所属外键的冲突分配及原始 ID 映射。专项 9 项通过（外部数据用例未设置环境变量时跳过实际操作）；新的真实副本测试句柄 66535/PID 58024，日志 /tmp/mb-document-owner-key-real-import.log，仍待结果。旧句柄 16330 和 68895 已失败结束，不再等待。最近全量 Rust 为 622 passed/1 ignored，早于本次所属主键改动；错误码专项 1 项及前端 20 项另外通过。953 可靠 head 61 继续保留，最近一次预算验收留下 SCRAPE_TIMEOUT，不声称最新刷新成功。

第十七轮补充：所属主键改动后的 Rust 全量 624 passed/1 ignored 已通过；新增检查耗时后专项 12 项通过，时间字段尚未部署至运行 Core。前端失败返回后详情重新读取已修复，44 项及类型检查通过；真实 UI 取消后不重开详情即显示当前取消原因，正文/head 61 保持不变，证据 /tmp/mb-document-r17-failed-refresh-ui-live.json。检查 9 为扩展超时、10 为取消，不能称最新来源刷新成功。扩展物理采集标签仍存在，NFR-003 该项未通过。完整导入仍为原句柄 66535/PID 58024，未计通过。

第十八轮补充：扩展 0.2.4 修复取消挂起等待和断线迟到执行释放新槽问题，全套 46 项、读取真实 manifest 后 worker 5 项通过。实际心跳更新及物理标签关闭仍未验收，NFR-003 不升级为完成。完整导入仍读取原句柄 66535，不以过 60 秒提示推断失败。

第十九轮补充：真实副本发现 180 条队列到已排除 bake_runs 的引用。导入器现按外键 SET NULL 语义处理，防悬空及误绑本地同号日志；专项 10 项通过。原真实导入 66535 尚运行旧代码，新修复的真实全库验收仍待执行。

第二十轮补充：旧真实导入 66535 因已证实遗漏日志引用修复而主动中止，日志保留；新任务 23036/PID 83818 使用修复代码，/tmp/mb-document-excluded-runtime-real-import.log，仍未终态。健康统计 API 和聚合已实现，文档相关 82 项回归通过；尚未部署运行验证。OBS-002 得到覆盖/失败/耗时样本和观察 cohort 聚合，但每任务等待/重试/重复数仍未完成。

第二十一轮补充：OBS-002 增加按租约的领取/等待/结束/安排退避及重复 enqueue 统计，84 项相关回归通过。中断及暂停不填造执行耗时，重复观察不放大任务次数。全量 Rust 正在 /tmp/mb-document-attempt-metrics-full-tests.log 执行。统计代码未部署；队列前去重事件尚未计数，不宣称全入口统计验收完成。原真实导入 23036 继续运行，未计通过。

第二十二轮补充：扩展管理页面受 Browser Use URL policy 阻止，已请求用户手动重新加载并确认 0.2.4，未收到完成答复。发布构建 78461、完整回归 17197、真实导入 23036 均继续原任务；运行部署未完成。只清理了已确认归属旧已中止测试的临时副本和导出，原始备份保留。

第二十二轮回归收尾：完整 Rust 630 passed、1 ignored、0 failed，384.34 秒，/tmp/mb-document-attempt-metrics-full-tests.log；原句柄 17197 已读取退出 0。发布构建 78461 和真实导入 23036 尚未终态，不能计为通过。

第二十三轮补充：新 Core 14801 已加载统计/迁移 120；真实 1 秒兼容预算返回 SCRAPE_TIMEOUT，检查耗时1512ms被健康接口准确聚合，953正文/head61不变且配置恢复。真实导入23036已终态，失败是原备份2条head中1条失配而验收错误要求全部恢复。契约验收改为只恢复有效head；另外新导入失配文档的fresh声明现会降级，本地已有文档不动。快照12项通过，新真实导入57500正在 /tmp/mb-document-head-projection-real-import.log 执行。最后导入改动未部署，不能宣称最终完成。


第二十四轮补充：真实外部副本全库导出/导入 57500 已通过，565.85 秒，日志 /tmp/mb-document-head-projection-real-import.log；不是未配置环境变量的空跑。有效来源 head、历史检查/归档、资产数量及脱敏占位均已验收。Python 当前来源共享校验七文件 528 passed，日志 /tmp/mb-document-source-projection-python-final-tests.log，Python 3.9 通过；对应 Rust 当前有效 head 查询及节流复用保护正在专项验证。最新代码未部署，扩展物理取消、最终 UI、OBS 全入口及规则灰度/回滚仍未完成。

第二十四轮回归收尾：document_ 相关 Rust 85 passed、0 failed，58.48 秒，日志 /tmp/mb-document-current-evidence-regression.log，句柄 20170 已退出 0。包含当前来源有效性新回归。全量 Rust 已启动为句柄 35745，日志 /tmp/mb-document-current-evidence-full-rust.log；尚未计通过，后续继续读取同一任务。

第二十五轮补充：全量 Rust 633 passed/1 ignored 已通过；RAG 新增向量来源 ID 与精确版本时间校验，相关 Python 149 passed。r19 运行加载中，真实浏览器扩展仍为 0.2.3，物理取消及最终端到端验收尚未完成。OBS 和规则灰度/回滚缺口仍保留，不缩减原方案范围。

第二十五轮运行收尾：r19 Core及Python实际加载；相同查询的咨询/创作引用都绑定953/head61，正文/哈希匹配，创作第1。UI重复搜索读旧数据已修复并真实复核，当前显示检查11预算耗尽且保留正文末尾；扩展0.2.4新采集与取消仍待验收。报告 /tmp/mb-document-r19-reference-evidence.json。

第二十五轮前端回归终态：BakeSearchFlow + BakeDetailDisplay 共45 passed，/tmp/mb-document-repeat-search-tests.log；tsc --noEmit 退出0，/tmp/mb-document-repeat-search-types.log。当前没有未结束的本轮测试或运行启动任务。原方案OBS全入口、规则灰度/写入回滚及扩展物理取消仍未完成，目标保持实施中。

第二十六轮补充：OBS003增加来源错配持久计数，接入RAG、向量写入和创作候选，Rust来源13项通过；最终Python消费回归及全量Rust执行中，新迁移未部署。统计范围明确不覆盖尚未接入入口，不能据此将OBS003整项标为完成。

第二十六轮回归终态：全量Rust634 passed/1 ignored，178.55秒，/tmp/mb-document-source-audit-full-rust.log，句柄76135退出0；Python消费及后台320 passed/2条既有Qdrant警告，/tmp/mb-document-source-audit-consumers-final.log，句柄67323退出0。r20已启动加载迁移121及审计，日志/tmp/mb-document-r20-runtime-start.log，尚待实际进程/迁移/健康统计核验。

第二十七轮：r20迁移121和新Python实际加载；真实创作候选发现文档16/head70失配，记录2次事件，健康接口同窗口汇总2次，16未返回引用，16/953正文和head不变。证据/tmp/mb-document-r20-mismatch-live.json。仅证明已接入路径的真实统计，不将OBS全范围标完成。

第二十七轮正常引用复核完成：r20的953咨询包含完整1139字正文并标head61，创作Top10第1、head61、正文hash匹配。/tmp/mb-document-r20-reference-evidence.json，句柄94521退出0。运行统计新增未破坏正常来源引用；本轮没有运行中的测试或部署任务。

第二十八轮：新增来源写入提交点开关及采集后暂停状态，源码回归中、未部署。UI专项21项通过；Rust首轮测试夹具缺偏好updated_at导致1失败已修正，最终结果待读取/tmp/mb-document-write-pause-final-rust.log。不将来源刷新开关当成完整ROLL002/005验收。

第二十八轮测试跟进：6516终态87passed/1failed，仍为编译时已读取的旧夹具缺updated_at错误，并非修正后代码结果。已结束原任务，启动/tmp/mb-document-write-pause-corrected-rust.log验证当前夹具；类型检查65130已退出0。当前写入开关未部署，不计交付完成。

第二十九轮：修正夹具后的文档88项通过；全量Rust78389和r21构建运行中。提交暂停入口实际验证脚本已准备但未执行。灰度/普通提炼写入回滚及剩余OBS范围仍未完成。

第二十九轮运行收尾：r21 Core48958已实际加载source_writes_enabled，手动刷新被拦截且953全状态不变，原偏好恢复；/tmp/mb-document-r21-pause-entry-live.json。事务中途暂停有单元回归，实际采集中途提交拒绝仍待验收。全量Rust78389退出0。

第二十九轮全量结果：Rust636 passed、1 ignored、0 failed，355.18秒，/tmp/mb-document-write-pause-full-rust.log。本轮测试、启动和入口验证均已终态，无需继续等待旧句柄。

第三十轮：真实浏览器先running/reading后关闭写入，最终SOURCE_WRITES_PAUSED，953正文/head/版本数量不变且偏好恢复；/tmp/mb-document-r21-pause-running-live.json。新增来源刷新灰度范围/规则版本校验回归中，未部署。普通提炼入口及暂停source_check审计仍为明确缺口。

第三十轮回归收尾：文档相关89 passed、0 failed，/tmp/mb-document-rollout-rust.log，句柄5681退出0；新全量Rust正在/tmp/mb-document-rollout-full-rust.log。浏览器当前active_job_count=0，中途暂停任务已结束，原偏好已恢复。新灰度配置仍未部署。

第三十一轮：补齐提交暂停source_check，并将失败状态/检查记录改为原子事务；插入失败回滚和暂停耗时回归运行中。该分支源码已补，但普通提炼评估审计等OBS001完整覆盖仍需核对，尚未部署。

第三十二轮：r22 Core77296已加载灰度及失败审计事务；真实范围排除/空范围/非法规则验证通过，原偏好恢复，953不变。637项全量及90项后续专项均终态通过；最新全量73976与实际中途暂停89867正在运行，尚不计通过。

第三十二轮测试终态：最新全量 Rust 638 passed、1 ignored、0 failed，174.25 秒，/tmp/mb-document-failure-audit-full-rust.log，73976退出0。首轮89867实际在running后发生NAVIGATION_TIMEOUT，故不计提交暂停通过；检查13准确记录规则v2和34110ms，正文/head/版本数量不变、偏好恢复、active_job_count=0。报告/tmp/mb-document-r22-scope-pause-live.json。全量测试结束后对该导航瞬态错误进行一次有界重试，句柄10110，独立保留/tmp/mb-document-r22-scope-pause-retry-live.json，不覆盖首轮证据。

第三十二轮运行终态：重试10110退出0，实际running后暂停得到SOURCE_WRITES_PAUSED；检查记录snapshot为空、规则document-quality.v2、耗时33413ms，953正文/head/快照和归档数量未变，原偏好精确恢复。/tmp/mb-document-r22-scope-pause-retry-live.json。此项为提交暂停及失败审计验收，不是扩展0.2.4物理取消；当前实际扩展仍0.2.3。本轮测试/启动/验证已全部终态，剩余原方案范围继续实施。

第三十三轮：新增迁移122及普通候选precheck/extraction/persistence三阶段类型化审计，缺失覆盖证据明确未知，健康接口增加分阶段计数，运行表不导入。首轮91项通过；后续全量93080和最终run ID透传专项仍在测试，未部署，不标OBS001全覆盖完成。

第三十三轮专项终态：最终任务ID透传及新增行为92 passed、0 failed，58.84秒，/tmp/mb-document-candidate-audit-final-rust.log，72870退出0。全量93080尚运行；r23正在/tmp/mb-document-r23-runtime-start.log加载迁移122和新审计代码，尚不计实际运行验收通过。

第三十三轮全量终态：新增审计、健康汇总及拒绝行为全量640 passed、1 ignored、0 failed，303.06秒，/tmp/mb-document-candidate-audit-full-rust.log，93080退出0。之后仅任务ID透传的最终源码专项92项通过。r23仍在发布构建，当前暂无运行中的回归测试。

第三十三轮部署终态：r23 release构建1m23s成功，Core99066加载迁移122，健康candidate_evaluations与同窗口数据库一致；当前0事件，只算迁移/接口冒烟通过，不算真实非零候选审计验收。953仍为head61且正文hash不变。/tmp/mb-document-r23-candidate-audit-live.json。本轮构建和测试均已终态。

第三十四轮：补齐普通自动建文档/观察合并提交点的暂停与稳定比例灰度，暂停走deferred保留水位且不消耗失败重试，API固定409原因；当前专项28808/26089、全量48205执行中，未部署。普通灰度候选造成批次延后这一调度边界仍需改进及验收，未缩减原范围。

第三十四轮专项终态：文档94 passed、0 failed、78.61秒（/tmp/mb-document-automatic-write-rust.log，28808退出0）；补充自动写入3 passed、0 failed、5.00秒（/tmp/mb-document-automatic-write-final-rust.log，26089退出0），覆盖bundle暂停deferred而非false-negative。最新全量48205仍在/tmp/mb-document-automatic-write-full-rust.log执行，含最后的API响应回归；新代码尚未部署。

第三十五轮终态：全量644 passed、1 ignored、0 failed，414.39秒，/tmp/mb-document-automatic-write-full-rust.log，48205退出0。r24 Core19699构建3m07s并加载普通自动写入控制；真实无效配置400且偏好不变，有效0比例/关闭写入接受，953全状态不变、原偏好恢复（/tmp/mb-document-r24-automatic-config-live.json）。自动提炼自然产生16条事件：precheck6/extraction5/persistence5，任务/时间线引用有效、字段白名单与未知覆盖准确，健康汇总一致（/tmp/mb-document-r24-candidate-audit-live.json）。这证明真实三阶段审计，不替代普通写入暂停现场或灰度非阻塞调度验收。

第三十六轮：新增迁移123持久暂停桶，零失败待办独立于水位恢复、SQL在LIMIT前筛选范围、队列统计同步资格；提交暂停先持久化再推进，旧metadata入口同样保护。cargo check通过，专项35314/53358及最新全量仍运行，未部署，实际批次恢复尚未验收。

第三十六轮专项终态：文档97 passed、0 failed、65.75秒（/tmp/mb-document-deferral-rust.log，35314退出0）；最终查询及队列统计专项1 passed、0 failed、0.30秒（/tmp/mb-document-deferral-final-rust.log，53358退出0），覆盖暂停恢复及LIMIT前范围筛选。最后将早期metadata暂停审计改为upsert，避免尚未建候选审计时无记录；该最终代码由全量50543继续验证（/tmp/mb-document-deferral-full-rust.log）。未部署。

第三十七轮：修正metadata暂停在构建队列时提前推进水位的问题，改为有序Skip消费且保留待办；新增第一项失败/后项暂停的pipeline时序回归4345。旧全量50543仍在运行，最新改动未部署，不据旧全量宣称时序修正通过。

第三十七轮验证跟进：持久暂停队列全量645 passed、1 ignored、301.49秒（/tmp/mb-document-deferral-full-rust.log，50543退出0）。时序专项4345首轮没有触发预期的不可用分支，误走completed而失败；测试客户端隔离环境代理后68564通过1项、1.90秒（/tmp/mb-document-deferral-order-final-rust.log），确认原水位和暂停记录保持。最新时序修改的提炼模块回归92729正在/tmp/mb-document-deferral-order-service-rust.log；r25启动加载在/tmp/mb-document-r25-runtime-start.log，尚未运行验收。

第三十七轮收尾：最新时序修改的提炼模块86 passed、0 failed、66.23秒（/tmp/mb-document-deferral-order-service-rust.log，92729退出0）。r25 Core42290加载迁移123及有序暂停处理，release1m19s成功；队列接口返回有效状态，953正文hash/head61不变（/tmp/mb-document-r25-deferral-live.json）。当前actual_paused_candidates=0，仅确认部署/接口，实际非零暂停恢复仍未验收。所有本轮测试及启动已终态。

第三十八轮：文件数据库多次关闭重开专项1项通过；真实备份隔离副本显式ignored验收1项通过，覆盖实际时间线候选的暂停/恢复、范围筛选及完成清理，全部文档正文联合校验和不变。日志/tmp/mb-document-deferral-reopen-rust.log和/tmp/mb-document-deferral-real-backup-rust.log。原始备份及运行库未修改；不将存储级实际数据验证等同于运行客户端任务端到端验收。全部本轮测试已终态。

第三十九轮：新增摘要来源投影，拒绝未绑定当前有效来源的摘要进入创作SQL/语义输入及RAG引用，并重新计算受影响引用分数；迁移124保留事件并增加摘要失配原因。Python206项及Rust文档98项通过、AST3.9通过；快照19144仍回归，未部署。摘要发布CAS绑定及带绑定版本的恢复尚未实现/验收，OBS003不标整体完成。


## 第四十轮：摘要编辑失效与导入绑定映射

- 第三十九轮快照测试已终态：12 passed、0 failed、26.41秒，/tmp/mb-document-summary-snapshot-rust.log。
- 修复普通文档编辑沿用旧摘要来源绑定的问题：摘要文字改变时撤销summary_source_snapshot_id并原子失效向量；仅修改其他属性且摘要未变时保留绑定。完整来源替换和正文/身份编辑清理摘要时同时清理绑定。
- 新存储测试模拟已发布绑定摘要，验证属性编辑保持、摘要编辑保留用户文字但撤销来源证明、旧索引删除入队以及正文head不变。文档存储20 passed、0 failed、12.90秒，/tmp/mb-document-summary-write-rust.log，25473退出0。
- 导入按两阶段处理文档到摘要来源的反向引用：新插入文档暂不绑定，待全部快照ID映射结束后，仅对匹配当前有效head的摘要恢复绑定；被拒绝绑定的摘要不降级成无版本legacy文字。已有本地文档不改写。
- 扩展完整快照测试，覆盖本地文档和来源快照ID同时冲突、摘要绑定重映射、同身份保留本地内容及重复导入。快照范围12 passed、0 failed、29.34秒，/tmp/mb-document-summary-bound-import-final-rust.log，41393终态。早期单项50012通过的是增强冲突场景前的版本，不作为最终验收。
- 本轮没有修改Python，没有部署或改写运行数据库；仍需实际摘要生成/发布CAS路径、运行客户端暂停恢复、扩展更新与完整最终验收。不能将来源读取/编辑/导入防护当作自动摘要重建已完成。


## 第四十一轮：摘要发布的事务版本校验

- 新增publish_document_source_summary，接收生成前读取的document_id、source_snapshot_id、updated_at和结果摘要；事务内重新核对完整来源、identity_match、快照所属文档、当前head、正文精确一致、文档未删除及版本未变。来源写入开关、自动文档开关和灰度范围同事务复核。
- 摘要与来源绑定、单调增加的更新时间、向量失效及持久删除队列一并提交。空摘要不发布；迟到结果返回false；暂停返回既有明确错误。不会把任意编辑摘要猜测绑定为当前来源。
- 将上一轮摘要编辑测试的SQL模拟发布改为调用实际发布方法，新增旧版本迟到、来源已换、暂停/灰度、空结果、版本严格增长和索引失效测试。文档存储21 passed、0 failed、30.37秒，/tmp/mb-document-summary-publish-rust.log，28604退出0。
- 加入清理索引失败的注入回归，证明摘要/版本/删除队列均回滚；最终摘要专项2 passed、0 failed，/tmp/mb-document-summary-publish-final-rust.log，7845退出0。
- 仍未部署。当前交付的是发布端原子契约，自动摘要生成worker尚未接入，不能声明正文更新已自动重建摘要。下一步需完成有预算的持久生成任务、实际调用与失效/重试验证；历史正文恢复脚本也需结合新绑定字段复核。


## 第四十二轮：后台摘要生成与持久任务接入

- 新增/bake/document_summary，仅接受正文与文档/来源/版本，通过现有P2 bake队列调用模型；不传旧摘要或标题。摘要限500字，必须给出1至6条能逐字匹配本次正文的引文；Rust再次校验返回版本、生成规则和引文。超出单次输入预算明确413，暂未实现长文分层摘要，不能默默截断。
- 新增迁移125与独立后台worker。任务按document/source/revision标识，持久租约与次数、退避、blocked/superseded/completed状态；文件重开后可恢复到期租约。发布校验租约、当前来源、版本和暂停，并与任务completed、摘要绑定、summary_generation_version及索引清理同事务提交。
- 复用来源写入/自动文档开关、灰度、重试配置；新增summary_execution_seconds默认300，1–1200秒。领取与发布同时遵守runtime.capture_enabled。非空用户摘要不自动覆盖。新版本生成独立任务；任务调度表不参与快照合并。
- 新HTTP测试真实经过reqwest请求、模拟模型端点、校验和数据库发布；错误来源ID只能进入重试。存储测试覆盖暂停退还次数、租约重开回收、旧worker无法写回/结束新租约、最大次数、延时和新版本恢复；到期但来源未变的拒绝结果保留可重试资格，不误标superseded。
- Python生成/既有提炼/API回归102 passed、1.51秒，/tmp/mb-document-summary-generator-python.log，86815退出0；三个修改Python文件AST 3.9通过。
- 文档范围103 passed、1 ignored、46.01秒，/tmp/mb-document-summary-worker-document-regression.log，87685退出0；全局暂停补充后专项5 passed、6.44秒，/tmp/mb-document-summary-worker-pause-final-rust.log，88412退出0；最终租约边界专项6 passed、9.02秒，/tmp/mb-document-summary-worker-lease-final-rust.log，19073退出0。
- 快照范围12 passed、29.42秒，/tmp/mb-document-summary-worker-snapshot-rust.log，51861退出0；主程序cargo check --bin memory-bread通过，/tmp/mb-document-summary-worker-bin-check.log，49086退出0（其后仅修改租约结束的可重试判定，并由最终专项验证）。
- 运行库检查runtime.capture_enabled=true、来源刷新配置缺省、953摘要NULL/正文1139字符。旧Core42290仍运行，旧启动锁PID97046已无进程。已通过标准start.sh启动r26，日志/tmp/mb-document-r26-runtime-start.log；尚未确认release切换/真实模型摘要。磁盘剩余约2.1GiB，不创建完整备份或DMG。
- 真实模型摘要质量、运行时发布与消费者使用同版摘要、剩余OBS覆盖、真实暂停恢复/扩展0.2.4及完整最终验收仍未完成；长文分层摘要及非空旧摘要处理策略需继续，不能将模拟模型HTTP测试作为真实模型准确性验收。


第四十二轮运行跟进（未通过真实摘要验收）：

- r26 release58.35秒完成，Core1420/迁移125，Model API99390、Creation99678、sidecar99367加载；摘要路由400无效请求探测确认可用。953自动任务实际领取，先后达到3次尝试后blocked，未发布摘要。
- 首次真实模型结果的诊断只输出统计：summary长度538超过500；5条引文均非逐字匹配，其中仅2条忽略空白后可匹配，因此不能降低门禁将其发布。已保留原任务与历史记录。旧实现复用了_call_bake_llm的原始回复追踪，已针对摘要调用新增capture_trace=False，保持其他既有调用行为；不删除历史记录。
- 生成协议改为完整正文分块并赋本次局部编号，模型选择evidence_block_ids，程序按编号恢复原文引文。每块最多300字符且不截断正文；输入预算按实际分块JSON估算。摘要要求120–200字，仍以500为硬上限。Python回归105 passed、0.95秒，/tmp/mb-document-summary-blocks-python.log，36196退出0；AST 3.9通过。
- r27仅加载新Python：sidecar8981、Model API8998，Core仍1420、Creation99678；标准start.sh已终态，/tmp/mb-document-r27-runtime-start.log。受控真实生成请求（不发布、不重置任务）仍返回422，16.04秒，/tmp/mb-document-r27-summary-generation-live.json，71143退出0。不能把此请求计为成功恢复。
- 为下一步排查细分DOCUMENT_SUMMARY_EMPTY / DOCUMENT_SUMMARY_TOO_LONG / DOCUMENT_SUMMARY_BLOCK_INVALID，并只返回白名单原因，不泄露原始回复。这项诊断细分尚未加载运行进程；最新Python三组105 passed，/tmp/mb-document-summary-validation-codes-python.log，87389退出0，三个文件AST 3.9通过。
- 保护证据/tmp/mb-document-r27-summary-protection-live.json：953 head61、正文SHA256仍460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8，摘要/绑定NULL；任务3次blocked。最新真实推理的raw_preview/response_preview均NULL，验证新摘要调用不保存原始回复追踪。
- 所有本轮测试、生成请求、启动均已终态，无待poll句柄。下一步先加载细分诊断并解决真实生成校验失败，再实现保留旧尝试记录的通用显式重试入口并恢复953；禁止直接清零失败记录冒充首次成功。长文分层摘要、非空旧摘要策略、其他派生字段/OBS/真实UI扩展与完整验收仍需完成。此次任务级blocked仅指953摘要队列状态，不是全局goal阻塞，仍有明确可执行修复工作。


## 第四十三轮：真实摘要引用数量误拒绝与审计重试

- r28加载细分诊断后，953受控真实生成仍422，16.66秒，明确为DOCUMENT_SUMMARY_BLOCK_INVALID；/tmp/mb-document-r28-summary-diagnostic-live.json，11646退出0。没有发布或重置旧任务。
- 为避免靠原始回复排查，添加仅包含正文块数、引用数、类型及有效数量的诊断；新增测试证明不返回模型原始字段值，摘要调用capture_trace=False有显式断言。Python106 passed、10.05秒，/tmp/mb-document-summary-diagnostics-final-python.log，31590退出0。
- 新增迁移126与POST /api/bake/documents/:id/summary/retry。仅针对当前有效来源/当前版本/摘要NULL/blocked任务；同事务保存旧任务完整调度JSON、次数、错误和时间再排队。审计失败不重置，重复调用幂等，正在运行/已完成/已变更来源不重置。旧租约不可发布。快照合并排除该运行时审计表。
- 最终审计重试专项7 passed、9.82秒，/tmp/mb-document-summary-retry-final-rust.log，1664退出0；快照12 passed、37.03秒，/tmp/mb-document-summary-retry-snapshot-rust.log，89351退出0。
- r29标准启动完成，Core25725/迁移126，日志/tmp/mb-document-r29-runtime-start.log。真实诊断9.07秒返回：38个正文块，模型选择7个编号，7个都是有效整数且在来源范围内，无数字字符串。拒绝仅因原固定6条上限，而非无依据编号。/tmp/mb-document-r29-summary-diagnostic-live.json，65351退出0。
- 将引用预算改为按实际正文块集合约束并去重；不截断引用/正文，不放宽来源版本、编号有效性或逐字出处。Rust额外验证引文总字符数不超过本次正文。摘要500字硬上限保留。新增7条有效引用放行、重复编号去重、总量超预算拒绝的Python/HTTP回归。
- Python107 passed、1.13秒，/tmp/mb-document-summary-source-budget-python.log，31448退出0；Rust最终摘要7 passed、5.94秒，/tmp/mb-document-summary-source-budget-final-rust.log，35107退出0；修改Python三文件AST3.9通过。早期49933测试已终态但不替代最终HTTP预算测试。
- 已启动r30加载引用预算修正，/tmp/mb-document-r30-runtime-start.log；尚未确认运行切换或953最终恢复。所有上述测试句柄已终态，r30启动须继续依据实际进程/日志等待，不重复启动。下一步待r30完成后，使用审计重试API恢复953，并核对真实摘要语义、来源绑定、正文SHA256及历史保留。


第四十三轮真实恢复完成（不等同于整套方案交付）：

- r30加载完成：Core33663/迁移126、Model API32297、sidecar32256、Creation99678，UI窗口20782。/tmp/mb-document-r30-runtime-start.log已终态。
- 通过POST /api/bake/documents/953/summary/retry发起一次显式恢复，200、queued=true；重试事件previous_record_json与重试前整个任务行精确相等，原3次blocked记录完整保留。/tmp/mb-document-r30-summary-retry-live.json记录结果。没有直接清零数据库或伪造新来源版本。
- 后台任务自动领取并在恢复后第1次尝试completed。实际模型摘要218字符，明确是快手灵机面向非L0商家的招商方案，包含原号+矩阵新号、品牌资产保护、分销授权、优质剧本/SOTA模型与ROI目标；与当前正文核对一致，没有引入旧侧栏内容或把方案冒充已验收结果。
- 摘要绑定61、summary_generation_version=document-summary.v1；正文SHA256仍460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8，head61未变化。详情API200且summary与存储精确相同。3条向量索引indexed_at均等于本次document.updated_at=1789213001664。证据/tmp/mb-document-r30-summary-publication-live.json。
- 通过运行Model API的/references执行references-only召回，200、9.85秒，953为rank1、source_snapshot_id=61。summary精确匹配当前摘要，overview和text包含当前摘要，text包含当前完整正文；三条索引版本时间匹配。/tmp/mb-document-r30-summary-rag-live.json，48545退出0。
- 所有本轮测试、真实请求和启动均已终态，无待poll句柄。953个案正文+摘要+详情API+真实RAG链路已验证；不能代替实际UI刷新/重试状态、创作消费当前摘要、长文分层摘要、非空旧摘要策略、其余派生字段/OBS覆盖、实际暂停恢复、扩展0.2.4与完整回滚/灰度验收。


## 第四十四轮：摘要状态、详情轮询与编辑保护

- 文档列表/详情增加summary_status，依据当前有效head、摘要绑定、文档版本及任务状态给出ready/pending/running/blocked/unverified；读取旧版本详情时不误报新版本ready。paused同时反映采集总开关、自动生成/来源写入、灰度和无效配置。无来源head保留旧接口兼容行为。
- UI单独展示摘要和正文；blocked提供审计重试入口，点击后重新读取真实任务状态。打开详情即时读取，pending/running每5秒轮询，完成/关闭/编辑后停止；异步令牌防止迟到详情请求覆盖编辑草稿或其他文档。错误显示可理解的更新失败提示。
- 额外修复同毫秒用户编辑竞争：摘要发布必须仍为NULL，即使updated_at碰巧未变化，自动结果也不能覆盖用户已填入的摘要；任务结束时已存在用户摘要判为来源已失效，不继续等待覆盖。
- 前端详情、搜索与API三组67 passed，/tmp/mb-document-summary-ui-regression.log，80328退出0；包含真实userEvent键盘输入、迟到请求不覆盖、生成到ready轮询停止、关闭后停止及重试读取。最终tsc无错误，/tmp/mb-document-summary-ui-tsc-passed.log，96630退出0；早期98975因测试getByRole不支持exact参数失败，已改为正则name，不能使用早期失败日志作为通过证据。
- Rust最终摘要专项8 passed、5.91秒，/tmp/mb-document-summary-status-publication-rust.log，97944退出0；文档范围106 passed、1 ignored、37.70秒，/tmp/mb-document-summary-ui-document-regression.log，76646退出0。此前所有相关专项及编译句柄均已终态。
- r31标准启动完成，release34.50秒，Core57999，Model API32297、sidecar32256、Creation99678、UI20782复用；/tmp/mb-document-r31-runtime-start.log。未修改Python，无需本轮Python语法检查；未打包DMG。
- CUA实际运行UI验证：新建本机1420验证页，经记忆→文档→搜索953打开详情；摘要218字符与原文分开展示。实际pressSequentially输入名称后缀“（编辑验证）”，点击取消恢复原名称，无保存请求。r31切换后重新打开详情显示“摘要已根据当前原文生成”，末尾“灵机内测反馈收集表”仍存在；截图检查抽屉、摘要区和固定底部按钮排版。
- 运行API200，summary_status.state=ready/paused=false，head61，正文SHA256仍460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8，summary_length218，updated_at1789213001664未变化，名称不含测试后缀。/tmp/mb-document-r31-summary-ui-live.json。
- CUA绑定summaryBrowser（iab浏览器ID1）、summaryUiTab（tab ID1）保留为handoff；已读local-web-development文档，页签仍在953详情。后续可复用绑定，跨压缩需重读CUA文档。没有修改用户原浏览器页签或访问chrome://extensions。
- 就绪摘要显示和编辑取消已完成真实UI验证；失败/暂停/轮询任务在UI中的真实状态流转仍只有组件测试，不能据此宣称全部运行验收。长文分层摘要、非空旧摘要处理、创作当前摘要消费、其余派生字段/OBS覆盖、真实暂停恢复、扩展0.2.4与完整回滚/灰度验收仍在原目标范围。


## 第四十五轮：长文分层摘要

- 单次预算不足时不再直接拒绝整篇正文；所有非空原文块按保守 token 预算分批处理，逐层汇总。每次模型返回的局部块编号先严格校验，再映射到原文块；最终引文直接取原文，不能用中间摘要冒充原文依据。
- 任一段失败、引用越界、取消或汇总无法收敛时，整个结果不发布。分层调用沿用 P2 队列执行时限；中间结果仅驻留内存，所有摘要调用关闭原文 trace。document-summary.v1 维持现有响应与发布契约。
- 新增跨多层完整覆盖、末尾来源保留、中途无效引用、取消后不继续推理、长正文端点路由、中间结果超预算拒绝测试。摘要与来源投影/审计合计39 passed，0.79秒，/tmp/mb-document-hierarchy-source-regression.log；三个修改Python文件通过AST feature_version=(3,9)检查。
- r32标准启动先等待另一真实启动进程72840释放互斥锁，随后成功；当前Model API73206、sidecar73188，Core57999及UI20782复用。/tmp/mb-document-r32-runtime-start.log。
- 真实模型分层请求正在通过专用合成夹具验证；使用不存在的专用文档/快照ID，仅调用摘要生成接口，不调用正文或摘要发布接口。未修改953或其他文档；不能将合成夹具的模型验证等同于真实长文恢复验收。

- 真实模型验证已完成：26,124字符、109原文块、2叶子批次+1汇总调用，HTTP200/33.92秒；最终摘要明确保留文末“尚未完成线上验收，收益仅为目标”的限制。6条引文全部属于原文且总长度未超过来源；三个来源身份/版本字段精确一致。/tmp/mb-document-r32-hierarchy-live.json；Model API日志确认0:0、0:1、1:0三个阶段完成。
- 953验证后正文SHA256仍460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8，摘要218字符、绑定61、updated_at1789213001664，均未变化。本轮请求和测试句柄已终态；长文生成的合成运行验证通过，真实长文恢复和原方案其他验收项仍未完成。


## 第四十六轮：非空未绑定摘要的显式重建

- 保留自动队列不覆盖非NULL摘要的原则。当前完整有效原文下，未绑定摘要新增can_regenerate能力；用户显式操作携带当前updated_at，后端拒绝过期版本、无效正文和已经绑定的摘要。
- migration127新增document_summary_versions内容归档，UUID唯一键、文档外键级联删除，保留原摘要、原绑定、生成版本与修订时间。归档、清空活跃摘要、修订递增、向量失效、旧任务失效同事务完成；任何归档失败不修改原记录。后续生成仍服从现有暂停/灰度/租约检查。
- 详情展示重建入口及归档说明；普通失败重试保持原接口；重建POST明确携带expected_updated_at。无核验来源或缺少修订时间时不能提交重建。当前生产库符合“完整有效正文+非空未绑定摘要”的记录为0，不修改已有953来制造测试场景。
- Rust摘要专项10 passed/5.96秒，/tmp/mb-document-summary-regenerate-rust-final.log；文档回归108 passed、1 ignored/39.03秒，/tmp/mb-document-summary-regenerate-regression.log。覆盖归档失败回滚、旧版本拒绝、旧文保留、后续正常生成，以及备份导入文档ID冲突/相同身份、归档关联和重复导入幂等。
- 前端三组69 passed及tsc无错误，/tmp/mb-document-summary-regenerate-ui.log、/tmp/mb-document-summary-regenerate-tsc.log；额外API hook请求契约测试另行运行。r33运行环境更新正在执行，尚未认定真实UI重建流转验收完成。

- 额外API hook契约组18 passed，/tmp/mb-document-summary-regenerate-api-ui.log；最终前端覆盖合计70个不同用例。r33新API已就绪，migration127归档表可读。真实请求验证缺失版本400、过期版本queued=false、953已绑定摘要使用当前版本请求亦queued=false。953正文/摘要/绑定/修订及归档数量前后完全一致；summary_status.ready/can_regenerate=false。/tmp/mb-document-r33-summary-regenerate-live.json。
- 本轮没有重写生产旧摘要（当前符合重建条件记录为0）。显式重建的正向流程已有存储/组件/备份回归，实际UI从未核对到生成完成的状态流转仍待运行验收；整体目标保持未完成。


## 第四十七轮：创作当前摘要验证及模板派生字段失效

- 真实Creation服务/creation/references返回953为rank1，HTTP200/10.94秒。摘要与当前218字存储精确匹配、source_snapshot_id=61、source_body_hash与1139字原文SHA256匹配，文档字段前后不变。/tmp/mb-document-r33-creation-summary-live.json。该接口是实际创作引用选择路径，证明候选消费当前摘要，不等同于完整创作输出最终采纳验收。
- 核对发现来源切换只清除summary/sections/tags/prompt而未清除style_phrases/replacement_rules/applicable_tasks。新增三个字段同事务失效，并回归原文版本归档保留全部旧值、partial不能覆盖当前状态。
- 当前存量有source head且这三个字段非空的仅文档16；其正文与head内容不匹配（body_matches=0），应继续由既有来源校验隔离。953三个字段均[]。不改写16原始证据，不将这些字段猜测绑定到新来源。
- 文档范围回归正在/tmp/mb-document-derived-invalidation-regression.log执行；本轮Rust变更尚未做运行部署。其他派生字段、OBS全入口覆盖、普通写入真实暂停/恢复、扩展0.2.4及完整UI/回滚/灰度范围仍需核对，不缩减原方案范围。

- 本轮文档回归终态108 passed、1 ignored、0 failed/38.61秒，/tmp/mb-document-derived-invalidation-regression.log。r34启动处理中，等待真实进程94272完成，未因观察超时重复启动。
- 补充明确未完成项：bake_service.rs的来源关联/去重/合并/拒绝日志仍有完整source_url/canonical_url输出（当前检索定位2456、2587、3007、4078、4086附近），需按NFR002去除敏感URL而保留内部ID和原因。此发现不能计为已修复。
- 更新原方案头部为“原始基线、当前验收中”，链接最新验收和实施记录；保留所有FR/AC/NFR/OBS/ROLL要求，不把当时未访问正文的结论当作当前953状态。

- r34启动成功并终态，Core95216、ModelAPI73206、Creation99678、UI20782；实际详情API200，953正文hash与218字ready摘要保持不变。/tmp/mb-document-r34-derived-runtime.json。本轮所有请求/测试/启动已结束，新增失效规则的实际来源切换仍以隔离回归证明，不宣称对953再次重抓验证。


## 第四十八轮：文档提炼诊断隐私

- 去除bake_service文档来源关联、同URL去重、延期关联、URL合并/拒绝、标题兜底匹配/拒绝中的完整URL和页面标题；保留timeline/document ID及固定原因。文档接纳日志不再输出模型任意review_status/match_level文本，仅保留内部ID与数值分数。
- 响应解码失败只记录reqwest decode flag，payload解析只记录serde错误类别；连接失败日志不输出原始请求错误。后台任务和候选失败持久化使用固定ApiError类别、受控上游code/status，避免把任意message写入任务诊断。原API业务错误返回机制不改，历史日志不追溯删除。
- 第一轮提炼服务87 passed/31.04秒，/tmp/mb-document-diagnostic-privacy-regression.log。新增HTTP运行级测试让本地模拟服务返回包含PRIVATE_BODY/PRIVATE_TITLE/SECRET/私有URL的错误类型响应，使用实际tracing订阅器捕获日志，验证仅有decode_error及固定错误码；最终88项回归正在/tmp/mb-document-diagnostic-privacy-final.log执行。
- 本文件tracing宏范围复查，原source_url/canonical_url/source_title/url直接插值格式剩余0；该有限范围检查不等于Python trace及其余消费入口的NFR002全范围完成。

- 最终提炼服务88 passed、0 failed/31.79秒，/tmp/mb-document-diagnostic-privacy-final.log；包含实际HTTP非法响应与tracing捕获测试。r35部署已启动。
- 新确认的NFR002缺口：Python knowledge/extractor_v2.py::_call_bake_llm默认capture_trace=True，普通提炼会向LLM跟踪器、专用错误日志和warning写入raw/response预览；摘要调用虽关闭trace，但普通文档提炼/合并仍需统一治理。下一步需保留内存解析/重试所需raw_content，去除持久诊断正文，并验证重试与计量统计不回归。本轮不能将NFR002整项标完成。

- r35 release31.84秒，Core6322已加载新版本，实际详情API200，953正文hash未变、218字摘要仍ready。/tmp/mb-document-r35-diagnostic-runtime.json；/tmp/mb-document-r35-runtime-start.log完成组件状态检查。所有本轮回归和HTTP请求均终态，无需重复启动。Python普通提炼trace治理仍为下一步必做项。


## 第四十九轮：Python文档提炼诊断正文隔离

- 普通提炼/合并/摘要共用的_call_bake_llm不再启用正文trace，即便旧调用显式传capture_trace=True也不持久化内容。内存raw_content继续支持截断重试/JSON恢复；返回raw_preview/response_preview为None。
- LLMCallTracker新增可选capture_content策略，bake关闭内容采集，保留token/耗时/状态；异常消息固定INFERENCE_FAILED、未知done_reason不写诊断，其他调用方默认行为保持兼容。专用bake错误日志使用白名单字段和内部ID格式验证，不接受任意正文/URL/错误消息或异常字段。
- 新测试覆盖正常、非法JSON、截断、传输异常、畸形元数据、旧trace=True参数及其他tracker调用兼容。首轮124 passed/2 failed揭露旧参数仍可能触发warning原文，已修正并保留首轮日志；最终提炼/摘要/传输/API共128 passed/1.07秒，/tmp/mb-document-python-privacy-endpoint-final.log。4个修改Python文件AST feature_version=(3,9)通过。
- API外层普通提炼和合并异常改为仅记录异常类别，不输出traceback；日志不回显任意trigger/retry描述、非数值来源ID或用户身份偏好。非法请求元数据与异常正文的组合测试验证日志及错误响应都不含测试秘密。
- r36运行ModelAPI14752、sidecar14726；实际/bake/extract合成文档夹具HTTP200/17.99秒，生成1条bundle用量记录，raw/response preview均NULL、token统计有效，新增model_api.log及bake错误日志不含PRIVATE_DIAG_R36_TOKEN；业务文档不由该接口发布。/tmp/mb-document-r36-privacy-live.json。API外层补充随后部署r37，不能将r36证据误称为该补充修改的实时失败分支验证。

- r37运行环境更新终态，ModelAPI20125、Core6322、Creation99678、UI20782。更新后再次通过实际/bake/extract执行独立夹具，HTTP200/6.47秒，新bundle用量记录raw_preview/response_preview均NULL、token统计有效，新增两类日志均无PRIVATE_DIAG_R37_TOKEN。/tmp/mb-document-r37-privacy-live.json；r36证据保持独立未覆盖。953正文hash、218字摘要及绑定61仍不变。
- 本轮请求、测试和部署均已终态；已证明当前普通bake共用调用及其API日志保护，不能替代剩余UI状态、普通写入暂停恢复、扩展0.2.4、全OBS入口/回滚验收。原目标继续保持未完成。


## 第五十轮：摘要真实 UI 验收与空白旧摘要恢复入口

- 新增 core-engine/examples/document_summary_acceptance.rs 与 desktop-ui/test-pages/document-summary.html/.ts：临时数据库、独立随机 loopback 端口、真实 Core handlers/summary worker 和现有 Model API，20 分钟自动退出。开发入口只在 DEV 下接受独立 127.0.0.1 fixture API，并在内存初始化客户端地址，不修改正式客户端设置。
- 真实 UI 已操作旧摘要显式重建：未核对 → 点击重建 → 暂停 → 恢复后 ready。数据库保留原摘要归档；第二条夹具 blocked → 点击重试 → pending → ready，重试审计保留 previous_attempts=3。第三条 29,920 字长文经实际后台模型完成，UI 显示 ready，摘要保留“尚未完成上线验收”。三条摘要分别绑定当前来源 1/2/3。
- 长文运行阶段在页面恢复前已结束，本轮没有直接看到 running UI；不得把最终 ready 推断为该状态已验收。夹具来源快照由测试数据建立，不能替代真实浏览器的完整采集验收。
- 证据 /tmp/mb-document-r50-summary-ui-live.json；独立数据库备份 /tmp/mb-document-r50-summary-ui-acceptance.db。旧 UI 标签已随回合清理关闭，恢复时使用原 IAB 的新标签；测试结束恢复 localhost:1420 常规页面，并停止自有 fixture PID26222，进程句柄终态143。正式953正文hash、摘要218字符、绑定61及updated_at均保持不变。
- 发现非NULL空字符串/全空白旧摘要被显示为 pending，但自动队列只领取NULL，导致永久等待。状态判定现与领取语义保持一致：非NULL未核对摘要显示 unverified，可经既有显式重建入口归档后生成。自动任务不清除用户/历史原值。新增空字符串、ASCII空白与Unicode空白存储回归，验证归档原值和后续可领取；组件覆盖空白摘要仍可点击重建并携带当前修订。
- Rust摘要专项11 passed，/tmp/mb-document-r50-summary-regression-final.log；前端详情29 passed，/tmp/mb-document-r50-detail-ui.log；包含新开发入口的独立tsconfig类型检查通过，/tmp/mb-document-r50-tsc.log。初次cargo调用因PATH缺少cargo未执行，之后显式使用现有cargo路径成功，首份失败日志保留。扩大文档回归与正式运行更新尚待本轮后续结果，不能将此前fixture运行视为新增空白状态修复的部署证据。

- 本轮最终文档回归122 passed、1 ignored、0 failed/31.09秒，/tmp/mb-document-r50-document-regression.log。r50标准启动已更换Core49540，实际953详情API200、摘要ready、正文hash与218字摘要不变，SQLite绑定61；/tmp/mb-document-r50-runtime.json。开发夹具已停止，新增空白状态修复已部署；其正向恢复以存储/组件回归证明，未改写真实953制造空白场景。
- 后续继续原验收缺口：扩展0.2.4真实重取/取消、普通提炼暂停恢复与灰度回滚、OBS全入口、派生字段完整性及最终创作输出采纳。另已定位来源替换UPDATE尚保留diagram_code/image_assets/language及部分匹配元数据，须核对各字段来源和用途再决定失效规则；不能只凭字段存在盲目删除用户内容。整体目标保持未完成。


## 第五十一轮：来源版本附属内容失效与完整回滚

- 追踪确认diagram_code和match_score/match_level由提炼payload生成，正文替换原先仍保留这些旧值；image_assets/language同属记录中的旧版本附属字段，没有证据可自动沿用到新正文。来源快照正式应用时，现在同事务清除活跃图表、图片引用、语言和匹配信息；全部原值已随完整record_json归档。未删除图片文件、来源关联、使用次数或其他文档，不改写存量历史记录。
- 扩展原子来源应用回归：旧派生值全部归档且当前值失效；当前版本补充图表/图片/语言/分数后遇到partial观察，所有这些字段保持不变。完整新版本继续可以替换较长旧版本，避免把“字数增长”当更新前提。
- 发现并补齐restore_document_body_version.py旧回滚字段白名单遗漏：样式短语、替换规则、适用任务、图表、图片引用、语言及匹配信息都恢复历史值；旧schema按实际列兼容，旧快照缺失JSON字段采用空结构，不能沿用较新派生内容。历史恢复移除source head时同步清空summary_source_snapshot_id及summary_generation_version，避免残留较新摘要绑定。
- 最终Rust文档回归122 passed、1 ignored、0 failed/30.90秒，/tmp/mb-document-r51-derived-final.log；首轮122项结果另存/tmp/mb-document-r51-derived-regression.log。Python重放/恢复4 passed、0 failed/0.07秒，覆盖完整和缺少可选字段的schema；/tmp/mb-document-r51-restore-final.log。两处Python修改AST feature_version=(3,9)通过。
- 基于第五十轮隔离数据库的新副本运行真实恢复脚本：dry-run不应用、显式apply恢复所有BODY_FIELDS、清除摘要绑定与head、其他两条文档不变、原始fixture文件SHA256不变，且写前备份保留新值。/tmp/mb-document-r51-restore-runtime/report.json。这是完整schema的隔离实际执行，不是对953做生产回滚。
- 只读核对当前两个有head的文档：图表/图片/语言均为空，仅一条仍有旧匹配字段；953五项均为空。不为制造测试场景修改真实文档。r51标准启动更新处理中；部署结束后的实际953复核仍待补充。扩展物理采集、普通提炼暂停恢复/灰度、OBS全入口及创作最终采纳仍在原目标范围。

- r51部署完成：release构建31.04秒，新Core57203已运行，/tmp/mb-document-r51-runtime-start.log；真实953详情HTTP200，正文hash、218字摘要、ready状态、绑定61及修订时间全部不变，/tmp/mb-document-r51-runtime.json。本轮测试/启动/恢复脚本均已终态，整体目标仍保持验收中。


## 第五十二轮：普通流水线暂停/灰度恢复与候选状态矛盾修复

- 新增core-engine/examples/document_pipeline_acceptance.rs，使用独立合成数据库、实际BakeService::run_bake_pipeline和当前Model API。显式新路径必须不存在，失败后保留夹具。依次关闭automatic_document_writes_enabled、设置automatic_document_rollout_percent=0、恢复默认策略，检查正文保护、来源补关联、观察队列与重试清理。
- 第一轮有效实际运行：暂停run1 completed（8ms）、灰度排除run2 no_op（5ms）、恢复run3 completed（28146ms）；实际/bake/extract HTTP200/约28秒，document被模型接受，knowledge/SOP被各自规则拒绝。恢复产生1条pending来源观察、旧正文保持，暂停failure_count=0，恢复后retry清空。/tmp/mb-document-r52-pipeline-valid.db、/tmp/mb-document-r52-first-runtime-evidence.json。
- 该运行暴露状态矛盾：document artifact audit为pending_source_refresh，但candidate audit被SOP的insufficient_source_capture_count覆盖为rejected。修复总候选审计：存在文档来源刷新状态时优先使用该状态和source_observed_not_applied；各产物审计独立保留。既有无文档刷新路径的SOP口径保持。新增实际来源持久化结果与SOP拒绝聚合的断言，并给运行夹具加入最终candidate状态校验。
- 验收工具开发失败记录保留：首次在根目录运行cargo未找到manifest；之后两处with_conn闭包错误类型编译失败；首次夹具entities=NULL导致row_to_timeline_record转换被旧InvalidQuery映射为“Query is not read-only”。补齐实体/详情字段后通过，此次并非SQLite只读权限或暂停写入缺陷。/tmp/mb-document-r52-pipeline-debug.db与对应日志保留，不绕过任何访问限制。
- 初轮提炼服务88 passed/21.05秒；补充断言后的最终回归及修复后第二次真实完整运行正在进行，尚未把修复前证据当作已修复审计的运行证据。
- 本轮范围是已有文档的普通流水线暂停、灰度排除与恢复；未证明全新文档在模型执行途中暂停后的提交边界，也不替代扩展物理采集与最终创作采纳。另实际Model API日志发现bundle artifact normalized仍输出模型任意reason，NFR002普通调用外层日志覆盖仍需补齐。

- 最终提炼服务88 passed、0 failed/21.72秒，/tmp/mb-document-r52-bake-final.log。修复后独立完整流水线再运行：暂停7ms、灰度排除5ms、恢复13669ms，真实模型执行完成。candidate和document均pending_source_refresh；原文不变、观察1条、retry为0，且precheck/extraction/persistence审计齐全。/tmp/mb-document-r52-pipeline-fixed.db、/tmp/mb-document-r52-fixed-runtime-evidence.json。r52部署等待真实启动进程69412完成，不重复启动。

- r52部署终态：Core71313实际运行，953详情HTTP200、正文hash/218字摘要/绑定61/修订均未变化，summary ready；/tmp/mb-document-r52-runtime.json。所有本轮测试、完整流水线和启动句柄已终态。原目标仍未完成，接续中途暂停提交边界与普通提炼外层reason日志缺口。


## 第五十三轮：普通提炼结果日志不回显模型理由

- 第五十二轮真实流水线发现Python bundle normalized日志输出自由文本reason，可能带入原文。现在单产物done及bundle normalized仅记录类型、caller、accepted和耗时；mismatch降级日志不再输出payload score/level。固定本地mismatch错误码保留。
- Rust artifact decision诊断不再输出extraction.reason或模型artifact_shape字符串，保留内部ID、固定规则结果、接受/有效性及恢复标志。模型原始拒绝理由仍保留在业务响应和产物审计记录，避免改变业务判断；不把内容型业务审计当成可外发的诊断日志。
- 新增Python单产物与bundle两条真实归一化路径的caplog负向测试，断言含PRIVATE_BODY/URL/token的reason只在返回结果中出现；新增Rust实际tracing订阅器验证决策日志无测试秘密，同时数据库业务审计保留原始理由。
- Python提炼/摘要/传输/API回归130 passed/3.16秒，/tmp/mb-document-r53-python.log；两个修改Python文件AST feature_version=(3,9)通过。Rust提炼服务回归仍在执行，尚未部署本轮日志修改。历史日志不追溯删除；其他日志入口的全范围覆盖仍需核对。

- Rust提炼服务89 passed、0 failed/45.72秒，/tmp/mb-document-r53-rust.log，新日志捕获测试通过。r53先等待自有启动进程84331释放锁，再完成Core87183、ModelAPI85893、sidecar85853更新；/tmp/mb-document-r53-runtime-start.log。真实合成提炼请求已发起，独立记录请求前日志偏移并检查新增normalized日志无自由reason；请求尚未结束时不记为通过。

- 首次受控真实请求530001因排队超时HTTP503终态，/tmp/mb-document-r53-live.log；当时正常任务13144完成，实际新增normalized日志已无reason字段。随后只读队列idle/ready确认后提交一次独立重试530003，使用不同标记及输出文件；该请求仍在排队，句柄36156，不能称为已通过。保持正常队列策略，不停止用户任务、不重复提交。

- 独立重试530003亦HTTP503终态，/tmp/mb-document-r53-live-retry.log；未进行第三次重复请求。受控生成验收本轮未通过，不能归为完成。保留正常任务13144实际normalized日志无reason的有限范围证据和当前队列统计，/tmp/mb-document-r53-runtime-evidence.json；953详情仍HTTP200，正文hash与218字ready摘要不变。所有本轮句柄已终态，下一步需查明排队资源状态后再选择受控验收时机；这不构成全目标阻塞，其他必做项仍可推进。


## 第五十四轮：排队超时定位与模型返回时暂停提交

- 上轮两次503继续只读定位：sidecar85853与ModelAPI85893共用/tmp/memory-bread-inference-slot-0.lock单槽。sidecar正常P1 seq1实际执行134161ms、seq2执行38285ms、seq3执行48097ms；/bake/extract等待预算固定90秒，与180秒执行预算分开。ModelAPI进程running=0不能证明跨进程推理槽空闲。
- 在观察到正常任务推进后仅提交一次新受控请求540001；它同样在排队阶段HTTP503终态，/tmp/mb-document-r54-live.log，无模型生成及业务文档提交。未修改队列预算、并发、优先级或停止正常任务，不将等待失败伪装成运行验收通过。
- 转向可独立执行的必要边界：新增完整BakeService流水线测试，HTTP模型替身在返回结果之前关闭自动文档写入。验证已产生的模型结果不能提交新文档，持久暂停记录保留且failure_count=0；恢复开关后第二次真实流水线请求完成，文档只生成一条、正文精确一致、暂停记录清除。
- 这是使用受控模型响应的HTTP集成测试，能确定提交边界的先后顺序，不能称为实际模型运行验收。专项测试运行中，后续必须检查结果并完成提炼服务回归；本轮不改变生产行为代码。

- 模型返回时暂停提交专项1 passed/1.31秒，/tmp/mb-document-r54-midflight-test.log；最终提炼服务90 passed、0 failed/23.63秒，/tmp/mb-document-r54-bake-regression.log。新增测试不改变产品运行行为，无需为测试代码重启服务。真实953正文hash、218字ready摘要仍一致，/tmp/mb-document-r54-runtime.json。所有本轮请求及测试句柄已终态；受控实际生成仍未通过，不继续盲目重试。后续推进其余全范围审计/扩展采集/最终消费验收。


## 第五十五轮：摘要来源错配审计与失效判断

- 原摘要提交失败通过SUMMARY_SOURCE_CHANGED终结任务，但没有进入统一版本错配统计。migration128兼容扩展component=summary_write并保留已有事件；不增加原文/URL字段。文档健康汇总按component动态聚合，可显示新事件。
- finish_document_summary_job现在在同一事务判断当前来源与终结租约：不仅比较head/修订，还检查快照归属、身份、完整性和正文精确一致。相同head但正文改变或快照降为partial时，旧任务直接superseded，不再重复执行旧输入。
- 仅当前拥有者成功终结且来源确实变化时追加一次summary_version_mismatch；预期/观察快照ID与计数用于定位。租约过期但来源仍有效保持pending且不记错配；重复finish不追加。审计插入失败记录固定告警，不允许旧摘要发布，也不阻止过期任务失效。
- 扩展过期租约/来源删除测试验证零误计和幂等；新增同head正文改变/partial降级、健康汇总、审计存储失败保护测试。初轮文档回归124 passed、1 ignored/69.97秒，/tmp/mb-document-r55-document-tests.log；补充健康/故障断言后的最终回归仍在执行。
- 已只读保存生产6条既有错配事件基线，/tmp/mb-document-r55-mismatch-before.json，部署后必须核对全部保留。本轮只证明摘要任务终结入口，不声称所有调度/无租约直接提交入口的OBS覆盖完成。

- 最终文档范围回归125 passed、1 ignored、0 failed/71.75秒，/tmp/mb-document-r55-document-final.log；同head错配健康汇总与审计插入失败保护均通过。r55标准启动实际进程33200执行中，等待构建/迁移完成后核对既有事件基线，未重复启动。

- r55部署终态：Core37473已运行，migration128实际schema包含summary_write，迁移前6条错配事件逐字段完整保留。953详情HTTP200，正文hash、218字ready摘要、绑定61及修订均不变；/tmp/mb-document-r55-runtime.json。所有本轮测试/启动句柄终态，未制造生产错配数据；摘要终结入口的行为与健康聚合由隔离集成测试证明，整体目标继续验收。


## 第五十六轮：交付范围复核与剩余错误日志

- 当前实际扩展仍connected=true、extension_version=0.2.3、active=queued=0；源码manifest0.2.4。/tmp/mb-document-r56-extension-status.json。历史Browser Use URL policy禁止chrome://extensions且禁止换控制通道绕过，仍需要用户手工重载现有扩展；未发起被禁止的UI访问。
- 新增document-source-quality-delivery-review-2026-09-12.md，汇总关键证据和剩余范围；原验收表标为早期快照，避免当成当前完整状态。全部原FR/AC/NFR/OBS/ROLL保留，未把已有实现改作新的成功标准。
- 补齐三类独立产物持久化失败日志，仅输出固定bake_retry_error_code；确定性文档恢复和false-negative日志不回显模型原始reason。模型业务审计和恢复正文保持。
- 增强真实确定性恢复测试：模型reason包含PRIVATE_MODEL_REASON/URL/token，实际tracing订阅器捕获恢复事件但不出现这些文字，文档仍从捕获原文精确恢复。提炼服务90 passed、0 failed/33.87秒，/tmp/mb-document-r56-bake-tests.log。
- r56标准启动部署处理中；尚未把错误路径日志修复扩展成全NFR002验收。原目标仍需扩展0.2.4实际重取/取消、完整消费/审计范围及联合回滚核对。

- r56部署终态：Core51036实际运行；953详情HTTP200，正文hash、218字ready摘要、绑定61及修订保持一致，/tmp/mb-document-r56-runtime.json。所有本轮测试/启动句柄已终态。下一步继续交付核对中未完成的入口；需要用户重载扩展这一环境边界仍存在，但不把其他可推进工作标为阻塞。


## 第五十七轮：创作召回的原始正文空壳检查

复核发现 CreationService 的关键词、语义种子和向量融合路径缺少共享空壳检查；长摘要和高相似度可能掩盖原始正文未加载。现对 document/pending_document 合并候选在重排前应用 embedding.document_quality.is_document_shell，并在向量编码与语义文本构造前拦截。检查仅使用 full_content，沿用共享规则，其他记忆域不套用文档规则。诊断记录唯一候选的 document_shell 过滤数量。

验证：test_creation_references.py、test_document_vectors.py、test_document_update_replay.py、test_rag.py 共220 passed，2个现有Qdrant兼容性告警，日志 /tmp/mb-document-r57-regression.log。新增回归覆盖三路重复召回、满分向量、长摘要、无head历史空壳的实际SQLite向量候选读取，以及共享正反例。两个修改Python文件均通过Python 3.9 AST检查。

实际953只读复核：1139字符，SHA256 460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8，摘要绑定61，新检查未拒绝，语义文本非空。证据 /tmp/mb-document-r57-real-body.json。该证据证明消费判定，不替代最终创作输出采纳、浏览器物理采集或完整联合回滚验收。服务加载日志 /tmp/mb-document-r57-runtime-start.log；完成状态以该日志与健康检查为准。整体交付仍待delivery-review所列剩余项。

第五十七轮运行确认：Creation Service已加载新源码，PID72573，8001/health返回ok；Core复用51036并通过关键接口自检。/tmp/mb-document-r57-service-health.json 同时显示浏览器扩展当前未连接，最后报告版本0.2.3，active/queued均0；新版扩展物理采集验收仍未完成。


## 第五十八轮：恢复后的消费隔离联合验证

发现并修复向量构建的衍生字段绕过：build_bake_document_snapshot原先在full_content和sections_json之间选较长文本后才做空壳判断；旧长章节可掩盖失败正文。现在先检查原始full_content，已确认空壳立即拒绝，再沿用正常候选处理；共享规则与短文行为不变。

新增test_restored_shell_stays_isolated_across_document_consumers在独立SQLite数据库实际调用restore_document_body_version.restore(apply=True)，随后调用向量快照构建、RAG旧URL向量实体化及CreationService真实关键词读取和融合召回。验证原始正文/来源关联保留、恢复前备份正确、head清空、旧向量删除入队，并且三个消费入口均拒绝恢复出的空壳，即使其包含长摘要与长章节。共享质量反例也加入长章节干扰。该测试未使用模型或真实Qdrant，不据此声明最终输出采纳、物理向量删除或全部ROLL-005已验收。

相关test_document_update_replay.py、test_document_vectors.py、test_creation_references.py、test_rag.py合计221 passed，2个Qdrant兼容性告警，耗时8.93秒，证据/tmp/mb-document-r58-regression.log。本轮三个修改Python文件通过3.9 AST检查。加载日志/tmp/mb-document-r58-runtime-start.log，使用既有MEMORYBREAD_LOCAL_ONLY入口避免无关仓库启动。

第五十八轮加载补充：第一次start复用了旧进程，不能算部署完成。实际原因是start.sh三个Python服务源码监测遗漏embedding目录；现已补齐，test/test-startup-freshness.sh通过新旧源码及较新pyc不触发重载验证，bash -n通过。第二次显式启动日志为/tmp/mb-document-r58-runtime-loaded.log。953实际正文经新向量构建器得到相同正文，校验和不变，证据/tmp/mb-document-r58-real-body.json。

第五十八轮部署确认：Sidecar81737、ModelAPI81751、Creation82005已加载，Core51036复用；7071与8001健康接口均HTTP200，证据/tmp/mb-document-r58-health.json与第二次启动日志。


## 第五十九轮：显式恢复可靠历史来源指针

补齐restore_document_body_version.py的可选--source-snapshot-id参数。默认行为仍恢复为未验证历史正文；显式选择时，预览及实际写入都在同一数据库读/写事务中校验快照归属、当前来源URL精确匹配、正文精确匹配、SHA256、完整性、身份、无截断及共享非空壳规则。不猜测历史指针。通过后原子恢复正文与head、修正来源元数据并沿用旧向量失效流程；仅恢复历史记录中已绑定该整数快照ID且存在生成版本的摘要绑定，否则消费投影不展示旧摘要。恢复前备份新增_source_head，保存被替换的指针。历史新鲜度保持historical_only，不表示重新访问了网页。

验证：test_document_update_replay.py新增成功及8类错误快照拒绝，拒绝前后DB字节不变且无备份；未绑定、错绑定、布尔伪整数摘要保持不可见。合计相关230项回归通过，2个Qdrant兼容性告警，10.96秒；日志/tmp/mb-document-r59-regression.log。两个修改Python文件通过3.9 AST。

实际CLI验证使用第五十轮完整schema夹具的隔离副本/tmp/mb-document-r59-source-restore/fixture.db，构造新旧版本后执行预览与--apply --source-snapshot-id 1，正文和来源head1一致，正确旧摘要可见，备份含恢复前head，其他文档和原始夹具不变。首次被原夹具占位哈希isolated-fixture-1拒绝，随后仅在隔离副本修正为实际SHA256，未放宽产品校验。报告/tmp/mb-document-r59-source-restore/report.json。未改动953，未触发服务重启（本轮为独立维护CLI）。

该证据补齐显式历史来源指针恢复，不替代新版扩展物理采集、最终创作输出采纳或完整灰度/规则回退联合验收。


## 第六十轮：真实创作消费验收与上游合并诊断修补（进行中）

真实请求通过8001/creation/agent/run发起，session=document-quality-r60-acceptance，run=document-quality-r60-acceptance-run，最多1条参考，仅memory_search，浏览器与联网关闭。事件保存在/tmp/mb-document-r60-creation/events.jsonl，原请求同目录request.json。tool.completed确认document953、source_snapshot61和正文hash460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8。已生成候选，首次delivery.checked为revise，发生修订；不能把草稿或首次检查当作最终通过。后续终态需单独核验。

并行日志审计发现extract_merged上游仍输出响应正文、无效模型字段、摘要、来源URL及异常文本。已移除这些诊断原文，保留计数/固定错误；主调用tracker显式capture_content=False，业务返回正文不变。新增成功、JSON错误、传输异常、无效coherence/groups/overview回归。test_background_processor.py、test_timeline_single_segment.py、test_bake_diagnostic_privacy.py、test_similar_merge_gate.py共117 passed，7.61秒，日志/tmp/mb-document-r60-regression.log。两个修改Python文件通过3.9 AST。真实创作请求仍在运行时未重启服务，本轮代码部署待该请求终态后完成。上游其他分段及补发入口仍需继续审计，不据此认定NFR-002全范围完成。


第六十轮真实创作终态：同一请求296.6秒完成，62个事件，delivery.checked先revise后pass，随后run.completed/completed。最终7项检查均passed，文档SHA256 35b65a88bcb64bccabe8dc1446e6d7708d13453c808510571a37f192d282455c，与delivery_checked_hash的16位前缀一致。最终唯一引用953/head61/body hash与数据库当前值一致，953正文及摘要绑定未变。人工对照正文确认非L0对象、主号保留品牌、人设与自主运营、矩阵新号作为增量、分销授权与自动化描述均有原文依据。最终正文与完整事件位于/tmp/mb-document-r60-creation/final.md及events.jsonl，汇总report.json。该证据是实际本地模型创作输出采纳，不是模拟响应或召回列表检查；仅证明这次953案例，不推及全部消费方。

本轮上游日志修补在真实请求终态后启动加载，日志/tmp/mb-document-r60-runtime-start.log。仍需核对其他日志入口、新版扩展物理采集及剩余联合回滚范围。

第六十轮部署确认：Sidecar7996、ModelAPI8010已加载上游日志修补，Creation82005/Core51036复用；7071与8001健康接口HTTP200，/tmp/mb-document-r60-health.json。


## 第六十一轮：单条与分段提炼诊断原文清理

extract_sync主调用使用capture_content=False，成功、解析失败与异常日志移除模型正文/响应和异常原文。_generate_segments移除摘要预览、异常traceback和任意丢弃原因，只保留计数与固定状态；业务返回和缓存摘要保持不变。新增单条成功/JSON失败/异常与分段成功/异常/缓存复用回归。

数据事实恢复调用曾尝试同样设置capture_content=False，但已有安全错误码被抹为INFERENCE_FAILED，首次相关测试3 failed/97 passed，日志/tmp/mb-document-r61-recovery-tests.log；已撤销该入口的尝试，保持其原有脱敏错误类型及早停原因行为，没有改测试来迎合退化。最终7组受影响测试共222 passed，7.71秒，/tmp/mb-document-r61-final-tests.log。最终两个Python文件通过3.9 AST检查。

本轮部署使用标准本地启动入口，日志/tmp/mb-document-r61-runtime-start.log。仍不能把这些入口通过扩大为全NFR-002完成：数据恢复入口的任意元数据、相似合并异常及剩余模块需要继续核对。

第六十一轮部署确认：Sidecar17201、ModelAPI17217已加载；Creation82005、Core51036复用；7071/8001健康接口均HTTP200，证据/tmp/mb-document-r61-health.json。


## 第六十二轮：恢复调用的诊断元数据校验

数据事实恢复原先把上游usage和done_reason直接传给用量追踪器，并在finally日志打印token字段；恶意或格式错误的元数据可携带正文，即使不记录message.content也仍泄露。现只接受非布尔、非负、SQLite整数范围内的token数值，否则使用合法后备计数或本地估算；显式0不被估算替换。结束原因限定为已有stop/length/repetition/ungrounded/cancelled；传给tracker的响应只含经过校验的usage和done_reason，不含原始响应对象。原有异常类型、早停错误码和业务事实解析保持不变。

新增5类恶意/畸形元数据及显式0回归，断言事实仍正常产出、私有元数据既不进入用量事件也不进入caplog。最终7组受影响测试228 passed，8.49秒，/tmp/mb-document-r62-regression.log；两个修改Python文件通过3.9 AST。标准本地加载日志/tmp/mb-document-r62-runtime-start.log。本轮没有新请求真实模型或修改953。

剩余诊断入口仍按原NFR范围继续核对；本轮不宣告全部日志或全部质量方案完成。

第六十二轮部署确认：Sidecar21546、ModelAPI21605已加载；Creation82005、Core51036复用；7071/8001均HTTP200，证据/tmp/mb-document-r62-health.json。


## 第六十三轮：公共用量数值字段边界

LLMCallTracker.set_response/set_tokens及log_llm_usage最终写库入口校验非布尔、非负、SQLite范围内整数。非字典usage、异常message结构与任意token元数据不会直接进入计数；总token和截断到SQLite整数上限避免溢出。其他调用方既有正文预览开关和安全错误码行为不变。新增私有tracker畸形元数据回归及实际SQLite数值字段写入验证，证明文本不能借动态类型整数列落库。

提炼7组测试232 passed，7.65秒，/tmp/mb-document-r63-tests.log；创作召回和RAG196 passed，7.57秒，2个Qdrant兼容性告警，/tmp/mb-document-r63-consumer-tests.log。两个修改Python文件通过3.9 AST。

共享monitor源码加入三个Python服务的显式加载检测，test-startup-freshness.sh验证通过，bash -n通过。标准加载日志/tmp/mb-document-r63-runtime-start.log。未改动953或新发起模型验收请求；原第六十轮真实创作证据保持。仍须完成剩余诊断、扩展与回滚范围，不据此宣告全方案交付。

第六十三轮部署确认：Sidecar28580、ModelAPI28606、Creation28986已加载，Core51036复用；7071/8001均HTTP200，/tmp/mb-document-r63-health.json。


## 第六十四轮：直接摘要发布的错配审计与剩余项复核

publish_document_source_summary_guarded的无租约发布路径，在拒绝旧/无效来源时增加summary_write/summary_version_mismatch计数。使用来源ID、正文一致性、完整性、身份与预期updated_at检查；同来源同revision仅因已有摘要而拒绝不计错配。租约调用仍由任务终结入口记录，避免双计；暂停/灰度错误在计数前返回。审计写入异常只输出固定告警，不让旧摘要发布。扩展现有实际SQLite事务回归覆盖旧来源事件、审计失败保护和同revision已有摘要不误计；原有暂停与向量清理失败回归继续通过。

Rust document_相关113 passed、1 ignored，0 failed，51.48秒，/tmp/mb-document-r64-tests.log；ignored为需要外部数据的验收入口，不冒充执行通过。标准本地加载日志/tmp/mb-document-r64-runtime-start.log。扩展重新核对仍connected=false/version0.2.3、active/queued=0，/tmp/mb-document-r64-extension-status.json。

原方案剩余范围继续保留：扩展0.2.4重新获取/物理取消/最终UI（FR001/002/006/007，AC004/006/010，NFR003，ROLL003）；其余文档诊断入口及敏感URL（NFR002）；评估审计字段与全部调用入口、队列前去重统计及摘要调度错配统计（OBS001/002/003）；规则回退、指针恢复、外壳消费隔离和兼容读取的联合运行证明（ROLL005/NFR004）。第六十轮已完成953最终创作采纳，本轮补齐无租约摘要提交错配，不能继续把这两项列作缺失，但不代表同类全部入口完成。

第六十四轮部署确认：Core40633已加载直接摘要审计，953详情HTTP200，正文校验和和head/摘要绑定61不变，证据/tmp/mb-document-r64-runtime.json。


## 第六十五轮：正式正文归档保留摘要绑定及导入映射

确认正式apply_document_source_snapshot此前直接序列化BakeDocumentRecord，而该DTO不含summary_source_snapshot_id/summary_generation_version。因此即使维护CLI已支持显式恢复，正式归档仍缺少摘要绑定证据。现于同一来源切换事务中读取并归档这两个内部字段，不改变公共DTO或增加迁移。

备份导入对新归档字段执行快照ID映射，并校验导入快照归属、历史正文、身份和完整性；失效/缺失映射清空绑定与生成版本，保留历史摘要原文。无绑定字段的旧归档不猜测绑定。扩展原实际SQLite文档发布测试验证第一版正确摘要随第二版切换归档；完整snapshot导入测试覆盖有效绑定、缺失来源和错配正文，分别与本地同/不同身份、占用/未占用快照ID组合，并重复导入验证幂等及本地正文不变。

文档Rust113 passed/1 ignored，54.33秒，/tmp/mb-document-r65-tests.log（扩展导入负例前，生产代码相同）；扩展负例后storage::snapshot::tests 12 passed，29.85秒，/tmp/mb-document-r65-import-tests.log，其中外部DB入口未设置环境变量，不作为真实全库重放证据；Python恢复14 passed，9.84秒，/tmp/mb-document-r65-restore-tests.log。无Python生产代码修改。标准Core加载日志/tmp/mb-document-r65-runtime-start.log。953保持原记录，不用生产来源更新来制造验收数据。

本轮补齐归档→导入→显式恢复所需的绑定契约，仍不替代新版扩展物理采集及全部回滚/审计剩余范围。

第六十五轮部署确认：Core53004已加载归档/导入修复，953详情HTTP200，正文hash及head/摘要绑定61不变；证据/tmp/mb-document-r65-runtime.json。


## 第六十六轮：摘要调度拒绝的可观察性

claim_document_summary_job在已启用且处于灰度范围内、摘要为空的文档中，记录无效来源的summary_schedule/head_invalid评估事件；正文不符、身份不符、partial或归属错误均拒绝生成租约。计数表示实际调度评估次数，不是去重文档数；暂停/范围外不计数。审计写入异常只留固定告警，不放行失配来源，也不阻塞有效文档获取任务。迁移129增补组件枚举并保留旧事件。

新增四类无效来源与暂停/灰度/审计失败保护组合测试，并核对document_source_health中的聚合结果。首轮113 passed/1 failed/1 ignored，失败为测试构造其他文档漏填created_at，修正夹具后114 passed/1 ignored/0 failed，73.58秒，/tmp/mb-document-r66-final-tests.log。旧失败日志/tmp/mb-document-r66-tests.log保留。不涉及Python修改。

迁移前只读保存8条现有事件到/tmp/mb-document-r66-audit-before.json；标准部署日志/tmp/mb-document-r66-runtime-start.log，待部署后逐条核对。新增调度审计补强OBS003，不代表OBS001/002或全部诊断/物理采集已验收。

第六十六轮部署确认：Core67065及迁移129已加载，原8条错配事件逐字段不变；953详情HTTP200、正文hash与head/摘要绑定61不变，证据/tmp/mb-document-r66-runtime.json。


### 第六十七轮：回访去重分阶段审计

入队前同 URL 候选跳过现在保存规范化来源身份的 SHA256（migration130），沿用候选审计唯一行，重复 upsert 不放大计数。健康接口 prequeue_coalescing 按来源哈希分组，无历史哈希时明确返回 null；不写入原 URL。队列内 duplicate_enqueues_by_document 按文档聚合，与已有创建时间窗口的累计 duplicate_count 口径一致。两阶段可能重叠，不合并计数。

实际 SQLite 回归覆盖同候选幂等、同来源分组、未知历史来源、窗口边界及隐私字段；队列测试覆盖窗口外创建却窗口内重复、窗口内创建而窗口后重复，明确其为创建集合累计值。最终 Rust document_ 回归 116 passed / 1 ignored，69.62秒，/tmp/mb-document-r67-final-tests.log。忽略项需要隔离真实数据库，不作为已执行证据。运行加载日志 /tmp/mb-document-r67-runtime-start.log；运行时结论待加载终态后补充。无 Python 生产修改，953 本轮不做内容写入。


第六十七轮运行终态：标准本地启动完成，Core87976；migration130列实际存在，source-health返回两阶段新增字段。953正文1139字及SHA256、摘要来源61保持不变；五条历史existing_document_url_linked审计保留。证据/tmp/mb-document-r67-runtime.json。新Core扩展状态connected=false、extension_version=null，不能用旧心跳版本冒充当前连接；物理采集仍未验收。本轮测试与启动均终态。


### 第六十八轮：初始化与相似查找诊断隐私

移除提炼器初始化日志中的用户身份原文，保留内存配置和已配置状态。相似查找异常统一为 SIMILARITY_LOOKUP_FAILED，不输出可能含来源 URL/正文的异常字符串；失败仍返回 None，不改合并规则。新增初始化及向量/数据库两种异常注入回归，确认隐私字符串不进入日志且原行为保持。

五组受影响回归131 passed / 1 skipped，1.80秒，/tmp/mb-document-r68-tests.log；跳过项为未显式启用的真实模型数据事实金标，不作为真实模型验证。修改的生产与测试Python通过3.9 AST。标准本地加载终态：Sidecar95389、ModelAPI95412已更新，Core87976保持，启动健康检查通过；/tmp/mb-document-r68-runtime-start.log。953详情HTTP200，正文hash和摘要来源61未变，/tmp/mb-document-r68-runtime.json。本轮不宣告全部诊断入口、扩展物理采集或联合回滚验收完成。


### 第六十九轮：刷新调度异常日志

刷新队列领取、结束、摘要worker及触发计数写入四个失败分支不再输出原始异常，改为固定错误码和内部文档/run标识。仅调整诊断输出，重试、租约、发布和失败状态不变。现有document_回归116 passed / 1 ignored，76.95秒，/tmp/mb-document-r69-tests.log；忽略项仍为外部隔离数据库入口，不扩大测试证据。运行加载日志/tmp/mb-document-r69-runtime-start.log，终态待核验。

剩余诊断审计已具体定位：共享api/handlers/data.rs的快照持久化异常、焦点门禁stderr、后台截图失败last_error、前台准备stderr仍输出原始错误，需要继续处理。这些入口不能因文档刷新自身无直接日志而排除在共享采集范围外。


第六十九轮运行终态：Core5061已加载，标准启动健康检查通过；953详情HTTP200，1139字正文hash和摘要来源61保持，证据/tmp/mb-document-r69-runtime.json。测试及启动已结束。共享浏览器data.rs原始异常日志是下轮明确修补项，扩展物理验收及联合回滚仍未完成。


### 第七十轮：共享浏览器采集诊断

api/handlers/data.rs五处日志去除原始数据库异常、任务异常、脚本stderr和截图last_error，保留固定failure_code以及内部source/browser标识；截图仍区分SCREENSHOT_BLANK与SCREENSHOT_FAILED。错误响应与证据清理逻辑不变。模块回归35 passed / 1 ignored，1.00秒，/tmp/mb-document-r70-data-tests.log；忽略项需要本机Chrome、Apple Events JavaScript和录屏权限，不能作为物理浏览器验收。继续执行文档相关回归。

本轮源码核对范围：document_refresh.rs、browser_extension.rs、api/handlers/browser_extension.rs无直接tracing日志；api/handlers/data.rs现有日志不再输出上述原始异常。此为明确文件范围的代码检查，不替代全消费者诊断审计或真实浏览器证据。


第七十轮终态：文档回归116 passed / 1 ignored，65.19秒，/tmp/mb-document-r70-document-tests.log。Core11416已加载，标准启动健康检查通过；953详情HTTP200，1139字正文hash及摘要绑定61保持，/tmp/mb-document-r70-runtime.json。源码日志五处修补已运行，不将35项共享浏览器模块回归或116项文档回归冒充实际Chrome物理测试。已完成第六十九轮定位的data.rs日志缺口；全消费者审计、扩展采集及联合回滚仍需验收。


### 第七十一轮：恢复事务故障与配置关闭后的隔离

扩展test_document_update_replay.py的真实SQLite恢复调用：在正文/head/摘要绑定更新后，通过vector_deletion_queue的ABORT触发器注入清理失败，断言完整SQL dump与操作前一致。移除触发器后重试，验证旧head7和对应摘要恢复、当前索引解除、旧point进入删除队列，一次提交完成。

恢复空壳后跨向量构建、RAG materialize和Creation检索的联合测试增加三组配置：默认开启；enabled/source_writes_enabled关闭；rollout空范围且capture关闭。三组均保留配置值和来源关联，拒绝空壳，即使旧摘要/章节很长也不能回流。测试通过证明Python消费隔离不依赖刷新开关；未启动Rust worker，不将配置存在等同于运行时暂停已验证。

最终16 passed，6.73秒，/tmp/mb-document-r71-final-tests.log；新增参数前14 passed，8.27秒，/tmp/mb-document-r71-rollback-tests.log。测试Python3.9 AST通过。无生产代码改动、无953写入、无需服务加载。仍需完整运行时暂停/来源恢复/实际Qdrant删除联合验收及扩展物理采集，不以隔离SQLite测试替代。


### 第七十二轮：恢复清理实际本地Qdrant

发现VectorStorage构造参数qdrant_path未被初始化使用，实际始终打开默认~/.qdrant。修补为显式路径优先，未提供参数保持默认；普通单例仍无参数初始化，未迁移用户向量库。

新增实际qdrant_client本地持久化集成测试，不使用FakeQdrant：独立临时SQLite与Qdrant目录写入当前文档和其他文档两点，调用正式restore后确认旧点尚在、删除outbox已生成；正式drain_deletion_queue删除旧点并清除outbox，其他点和SQLite关联保留；再次drain无任务；关闭并重新打开同一Qdrant目录确认删除持久化。此证据补齐实际向量删除，但尚未在同一隔离Core进程串联暂停/指针恢复/消费者状态，不据此宣告全部ROLL005。

恢复/文档向量/embedding回归78 passed，11.11秒，/tmp/mb-document-r72-tests.log；后台/RAG回归197 passed、2 warnings，8.17秒，/tmp/mb-document-r72-consumers.log。两修改Python文件通过3.9 AST。运行加载日志/tmp/mb-document-r72-runtime-start.log，终态待核对。未写生产953或默认Qdrant测试数据。


第七十二轮运行终态：Sidecar18998、ModelAPI19087、Creation19331已加载共享embedding修正；Core11416保持，标准健康检查通过。953详情HTTP200、1139字正文hash和摘要绑定61保持，/tmp/mb-document-r72-runtime.json。真实本地Qdrant清理证据已取得，不再将所有实际向量删除列为缺失；完整Core运行时联合回滚和扩展物理采集仍待验收。


### 第七十三轮：Rust租约与Python恢复跨运行时串联

新增core-engine/examples/document_rollback_acceptance.rs，可执行验收入口要求全新数据库路径，拒绝覆盖。使用当前完整schema，先由正式Rust调度方法领取head8摘要租约，再设置source_writes_enabled/automatic_document_writes_enabled=false和capture=false，确认不再领取任务；子进程调用正式Python恢复CLI切换归档正文与head7，旧租约发布失败，检查旧摘要绑定7保持。随后恢复空壳归档，确认head和摘要绑定清除且暂停继续生效。

首次执行因fixture归档字段误用created_at失败，/tmp/mb-document-r73-rollback.log；修正为saved_at并提供replaced_by_snapshot_id后，在全新/tmp/mb-document-r73-rollback-fixed.db完成，/tmp/mb-document-r73-rollback-fixed.log输出四项断言成功。没有复用失败数据库或改产品规则。

随后在同一完整schema数据库调用实际Python向量构建、RAG materialize及Creation检索，空壳与旧向量chunk均被拒绝，/tmp/mb-document-r73-consumers.json。本例直接调用正式Rust repository方法，不启动HTTP服务或常驻worker循环；因此证明租约/暂停/恢复/消费组合，不冒充浏览器物理运行或HTTP端到端。实际Qdrant删除由第七十二轮独立持久化集成测试证明，未在本例重复。无生产代码或953数据修改，无需加载服务。


### 第七十四轮：评估指标缺失与未知口径

source-check新增body_character_count，直接按已脱敏快照Unicode字符计数，包含空白，不信任采集器自报计数。采集失败检查显式记录正文字符/实质块/排除块/脱敏比例/覆盖证据为null，避免缺省被视为零。现有候选审计已区分输入字符与未知正文指标，本轮对齐来源检查；substantive_block_count仍为null，因为匹配块数不能证明逐块实质性分类。

更新现有Unicode对齐和失败检查测试，验证实际字符计数与显式未知字段。完整document_回归日志/tmp/mb-document-r74-tests.log，终态待补。原OBS001尚需逐块分类及全评估入口核验；本轮不调整正文准入规则。


第七十四轮终态：116 passed / 1 ignored，93.45秒，/tmp/mb-document-r74-tests.log；Core34344已加载，启动健康检查通过。953详情HTTP200，正文hash和摘要绑定61保持，/tmp/mb-document-r74-runtime.json。本轮未新触发真实刷新，新增审计字段行为由SQLite/handler回归证明，不能声称生产新快照验证。OBS001逐块实质性指标与全部入口仍待完成。


### 第七十五轮：对齐正文块候选计数

来源审计对每个已对齐到脱敏快照的块记录body_candidate，使用现有is_document_shell规则判定；substantive_block_rule=aligned-non-shell.v1明确算法。substantive_block_count是已提供且对齐的非空壳正文候选块数，不是语义正确性证明，也不代表未采集页面。缺少blocks、任一无法对齐或超过5000上限时返回null。页面coverage继续独立，未改变准入规则。

新测试覆盖普通短段落、导航空壳、短数值表格单元格、缺块与无blocks，确认不泄露正文。第七十四轮暂缺逐块指标现已补入规则化候选计数；不得将其扩大成业务语义真值。document_回归日志/tmp/mb-document-r75-tests.log，终态待补。无扩展源代码变动，实际扩展采集验收仍待完成。


第七十五轮终态：117 passed / 1 ignored，65.37秒，/tmp/mb-document-r75-tests.log。标准本地加载完成，/tmp/mb-document-r75-runtime-start.log；953详情HTTP200，正文hash和摘要绑定61保持，/tmp/mb-document-r75-runtime.json。本轮无真实新采集，块计数由隔离回归证明。


### 第七十六轮：文档向量诊断与重试记录

vector_storage.py去除文档URL doc_key、原始异常及traceback的日志输出，保留内部capture/document/point ID及固定代码；删除outbox失败改为VECTOR_DELETE_FAILED，同时用于日志、返回诊断和SQLite last_error，保留待删点与原有退避/尝试次数。原始异常只用于内部异常处理，不再扩散至诊断。

五组向量/恢复/后台/RAG回归275 passed、2 warnings，33.68秒，/tmp/mb-document-r76-tests.log；新增私有异常注入删除失败测试单独1 passed，1.33秒，/tmp/mb-document-r76-private-test.log，确认重试保留且三个诊断出口无正文/URL。两Python文件3.9 AST通过。首次追加测试路径多写ai-sidecar未执行，随后修正并单独运行新增测试，未将它冒充已包含在275项中。标准加载日志/tmp/mb-document-r76-runtime-start.log，终态待补。

本轮重新读取扩展状态connected=false、version=null、active=queued=0。已通过异步问题请用户手动重载扩展，说明此前工具明确拒绝chrome://extensions及替代通道；没有绕过该限制。当前仍可推进其他验收，不标整体blocked。


第七十六轮运行终态：标准本地加载及健康检查通过，/tmp/mb-document-r76-runtime-start.log。953详情HTTP200、正文hash和摘要绑定61保持，/tmp/mb-document-r76-runtime.json。276项相关测试通过（275既有回归+1新增专项），当前不据此宣告所有消费日志或扩展采集完成。


### 第七十七轮：检索和文档刷新消费诊断

rag/retriever.py内部HTTP、Qdrant连接/检索/filter和FTS/知识检索失败不再记录原始异常。creation/service.py文档刷新失败不再记录接口任意reason或traceback，关键词/向量/跨域候选/文档读取/重排失败均保留固定错误码，原回退与返回逻辑不变。新增内部HTTP私有异常注入，确认None失败语义和固定日志，原始URL/正文不出现。

RAG、Creation引用和新增隐私测试197 passed、2 warnings，12.82秒，/tmp/mb-document-r77-tests.log；三修改Python文件3.9 AST通过。标准加载日志/tmp/mb-document-r77-runtime-start.log。此轮限定文档召回入口，不将其他创作生成、OCR、互联网或报表日志一起宣告已审计；仍需按原NFR范围核对相关共享调用。


第七十七轮运行终态：Sidecar50524、ModelAPI50627、Creation50899已加载，Core42771保持，标准健康检查通过；953详情HTTP200、正文hash和摘要绑定61保持，/tmp/mb-document-r77-runtime.json。197项回归通过，扩展连接及全范围验收仍未完成。


### 第七十八轮：原方案完成度审计

重新读取原方案32项编号、当前审计调用点及关键原始报告，形成document-source-quality-closeout-matrix-2026-09-12.md；修正delivery-review中过期缺失项。已取得的真实创作、实际向量删除、暂停/恢复/消费联合证据不再重复列缺失。发现恢复CLI校验和与反向操作清单尚未统一；下一步补此原ROLL005要求。明确原方案未要求所有验证在单一HTTP进程，不额外增设该门槛。本轮仅文档审计，无产品代码/生产数据修改，无需重启或重复测试。整体仍未通过，扩展物理采集、剩余共享诊断与评估拒绝入口有具体待办。


### 第七十九轮：恢复校验和、清单与反向CLI

正式restore脚本新增document-restore-receipt.v1：备份文件SHA256、文档+head的规范化前后状态hash、数据库绑定、reverse_argv与undo_of。备份和清单使用O_EXCL/0600创建并fsync；先持久化prepared意图，再提交SQLite。prepared不是提交成功证明，反向操作按真实after状态hash校验。清单写入失败时连接finally关闭，整个事务回滚。

--undo-manifest默认预览，--apply才写；与version参数互斥。检查备份checksum、归属数据库、文档ID及当前状态未变，再沿用原有严格来源head验证及正文allowlist恢复。保留历史/来源关联；旧索引继续失效重建，不复活旧point。反向操作本身生成新的备份和清单，不能重复应用旧清单覆盖后续修改。

22项恢复/重放测试全部通过，11.38秒，/tmp/mb-document-r79-verified-tests.log；包含备份被改、当前文档改变、数据库不符、清单写失败原子回滚、真正subprocess反向执行、重复拒绝以及既有真实本地Qdrant删除。两Python文件3.9 AST通过。

实际完整schema程序再运行生成/tmp/mb-document-r79-cli.db，暂停/恢复/迟到摘要拒绝通过，/tmp/mb-document-r79-cli.log；使用最新清单reverse_argv执行，head7与摘要绑定7恢复，/tmp/mb-document-r79-cli-undo.json。第一次收集清单时未解析/tmp符号链接导致未找到匹配，修正验收脚本为resolved路径后成功，未放宽产品数据库绑定。无生产953写入，无服务代码加载需要。历史旧操作不伪造追补清单。


### 第八十轮：拒绝路径保留已知过滤统计

源码核对确认普通候选三阶段审计均在对应处理开始前；来源刷新transport失败已有失败检查。发现经过隐私过滤后因终态页面、空壳/空文本或身份不符提前拒绝时，已取得的字符和过滤统计被丢弃。新增typed measurements辅助入口保留source/body字符数、redacted字符数及fraction，未取得正文的超时/暂停保持null。没有持久化拒绝正文，没有创建verified快照或改动旧正文。

扩展现有失败handler集成测试，验证已知测量值、无snapshot/覆盖未知及正文保持；document_回归/tmp/mb-document-r80-tests.log，终态待补。此处补齐三条已定位拒绝路径，仍不以搜索结果替代全部门禁审计。


第八十轮终态：117 passed / 1 ignored，93.79秒，/tmp/mb-document-r80-tests.log。Core66537已加载，标准健康检查通过；953详情HTTP200、正文hash和摘要绑定61保持，/tmp/mb-document-r80-runtime.json。新拒绝测量值由handler回归验证，未在生产制造失败采集。


### 第八十一轮：咨询上下文选择诊断

rag/pipeline.py七处共享读取/向量/文档关联异常改为固定代码；上下文候选选择不再输出任意activity/importance元数据，拒绝分支不再打印未知source_type原文。保留计数、耗时和内部selection_origin，选择逻辑不变。新增有效document候选携带私有activity及无效source_type的测试，验证一选一拒且日志无正文/URL。

隐私专项、RAG和Creation引用198 passed、2 warnings，10.82秒，/tmp/mb-document-r81-tests.log；两Python文件3.9 AST通过。当前pipeline所有logger调用已逐项核对，剩余为固定文字、数量、时长和内部阶段。rag/llm/ollama.py无实际logger调用。此结论限定上述文件，不扩大为生成服务所有共享出口。标准加载日志/tmp/mb-document-r81-runtime-start.log，终态待补。


第八十一轮终态：Sidecar70651与ModelAPI70674已加载，Core66537保持；标准启动在等候旧锁后自行继续并通过健康检查，启动进程已退出。953详情HTTP200、正文hash和摘要绑定61保持，/tmp/mb-document-r81-runtime.json。198项回归通过，生成服务其余相关共享出口和扩展验收继续保留。


### 第八十二轮：创作服务共享诊断集中核对

对creation/service.py全部logger调用做AST清单核对，集中修补16处原始异常/traceback/用户doc_type/搜索query/来源URL输出，保留固定代码、内部source/domain标识、计数和时长。证据OCR、状态保存、操作解释/校验、技能推理/提炼、用量写入、语料统计、报表和联网补充回退逻辑不变。

9组受影响回归387 passed，7.94秒，/tmp/mb-document-r82-tests.log；随后新增联网双引擎私有query+异常注入专项，privacy文件3 passed（其中2项重复），7.89秒，/tmp/mb-document-r82-privacy.log。总不同测试388项，不把重复2项重复计数。两Python文件3.9 AST通过。专项没有发出真实联网请求。

再次检查本文件，无logger.exception/traceback、query/source_url/error原文输出；剩余日志为固定文字、内部标识/阶段、配置模型名、统计或异常类型，不将该文件结论扩大到未核对的其他模块。标准加载日志/tmp/mb-document-r82-runtime-start.log，终态待补。


第八十二轮终态：Sidecar77444、ModelAPI77498、Creation77832已加载，Core66537保持，标准健康检查通过，启动进程终态。953详情HTTP200、正文hash和摘要绑定61保持，/tmp/mb-document-r82-runtime.json。388项不同回归通过；扩展真实采集仍未验收，未将日志整改等同于整体交付。


### 第八十三轮：当前工作区集中回归

本轮无代码变动，以当前代码进行收尾回归：Rust全库667 passed/2 ignored，263.97秒，/tmp/mb-document-r83-rust.log；扩展46 passed，13.91秒，/tmp/mb-document-r83-extension.log；详情/搜索54 passed，24.55秒，/tmp/mb-document-r83-ui.log；19组Python文档/提炼/恢复/向量/RAG/创作相关测试800 passed/1 skipped/2 warnings，13.36秒，/tmp/mb-document-r83-python.log。

Rust忽略项分别为物理Chrome/AppleEvents/录屏测试和隔离真实备份测试；Python跳过项为未显式开启的真实模型数据事实金标。不同语言测试数不相加作覆盖率。扩展测试是模拟环境代码验证，不能当作物理标签验收。

使用本机/usr/bin/python3（3.9.6）直接compile15个本任务核心生产文件，同时扫描annotation中的BitOr联合语法，全部通过，/tmp/mb-document-r83-python39.json含文件清单；不泛化为所有客户端文件。953正文hash和摘要绑定61保持。扩展实时状态及汇总/tmp/mb-document-r83-report.json。全部本轮测试句柄已终态，不需要重启。尚未据此证明全部入口覆盖或扩展真实采集，目标保持未完成。
