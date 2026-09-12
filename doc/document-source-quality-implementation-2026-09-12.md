# 文档质量修复执行记录

状态：实施中，未交付。953 已通过真实产品刷新切换到完整正文并保留旧版本；后续回访更新保护、自动补全与完整消费链路仍需验收。

## 已落地的第一批改动

- 去掉已存在 URL 只登记来源后直接跳过的分支；批内以 URL + 来源指纹区分任务，允许同一 URL 的不同内容进入处理。
- 凭据字段要求明确冒号分隔，不把“商家账号运营”等普通表述过滤至行尾；保留带空格的明确密码字段的隐私保护。
- 普通正文合并的保留校验失败时抛出 BAKE_DOCUMENT_MERGE_PENDING，不登记已应用指纹；该类失败允许在已有其他产物时进入重试。
- 刷新完整性读取扩展的真实 completeness，不再凭缺失字段默认完整或凭空设置稳定次数。
- 浏览器任务增加 content_kind=document；文档采集使用语义正文根及编辑器结构适配，排除导航/工具栏/隐藏节点；保持普通报表采集路径。
- 文档采集保留段落、表格和代码边界，有限遍历内部滚动并恢复位置；发现不同渲染片段时保守标记部分覆盖。

## 当前验证证据

- Rust 隐私过滤：2 项通过。
- Rust bake_service：81 项通过（第一批 URL 调度及失败指纹改动之后；后续重试改动还需重新回归）。
- Rust document_refresh：18 项通过。
- Chrome content-runtime：33 项通过，含新增正文排噪、结构保留及正文根选择测试。
- 真实 Chrome 已打开 953 URL，当前标题为“快手灵机：面向非 L0 商家的规模化 AIGC 招商方案 - 云文档”。页面正文根唯一，innerText 1146 字符、44 个非空行，包含账号人设及第四节，不含知识库目录和字体工具栏。这是正文范围验证，尚不是修复后采集链路验收。
- Rust 合并重试专项正在运行，日志 /tmp/mb-document-retry-tests-20260912.log。

## 尚未完成，下一步继续

1. 新的文档采集器通过真实扩展链路验证；不能用手工 DOM 读取替代产品路径验收。
2. 收紧正文质量规则并贯通来源原文、脱敏统计和所有检索消费路径；明确空壳、部分、完整状态。
3. 增加可靠版本指针、旧正文备份和摘要/索引版本绑定；解决刷新快照“观察到”与正文“已应用”目前混用指纹的问题。
4. 覆盖刷新合并路径的拒绝处理（目前仅普通合并已修）；覆盖超时/失败的更新重试，不能将临时失败永久跳过。
5. 历史已登记来源而未合并的候选需要独立重放；当前 watermark 和成员表仍会挡住它们。不能靠重置全局水位或篡改采集时间解决。
6. 对 953 备份后走修复路径重建原文及索引，保留 ID、收藏与引用，做实际页面/检索核验。
7. 更新详情页完整性和重新获取入口、可用范围提示；沿用现有 UI。
8. 重新执行所有受影响 Rust/Python/JS/前端测试；Python 若修改必须兼容 3.9。自测通过后才运行受控部署与最终验收，不默认打包 DMG。

当前实现只覆盖完整方案的一部分，不应将本记录或局部测试通过当作交付完成。

## 第二批实施与真实验证进展

- 新增迁移 115：完整来源指针与旧文档 JSON 版本备份；完整版本原子应用，部分版本不覆盖、较短完整版本允许替换、过期版本禁止倒灌。
- 快照观察不再抢先登记已应用指纹；完整正文应用后才登记。相同内容从部分变完整可升级覆盖证据。
- 强化空壳过滤，排除把版本号小数点、侧栏文本和只读提示当作正文句子的误判。真实 953 旧正文现在判为 shell。
- 来源刷新也执行隐私过滤；实际验证暴露主账号字段误过滤，已补充字段边界测试。
- 增加手工刷新语义及同文档单任务锁；手工刷新不改变自动策略，30 秒内不重复请求；后台 require_latest 不绕过既有策略。
- 新增历史更新重放、旧正文版本恢复工具，默认 dry-run，写入前保存备份，保持采集时间、水位、历史及新来源关联。
- UI 明示部分覆盖和历史未校验状态。

验证记录：全量 Rust 595 通过、1 既有忽略；随后刷新专项 19 通过，版本专项 2 通过，隐私专项 2 通过。Python 文档向量 14 通过、RAG/隔离/重放组合 124 通过、重放与恢复专项 2 通过；修改的 Python 通过 3.9 语法检查。前端详情 17 通过，前端构建通过。扩展正文/滚动覆盖共 34 项通过。

实际刷新：产品接口生成 953 的来源快照 60，1086 字符、4 段、稳定次数 2、partial。由于误过滤及保守覆盖判定，按规则没有覆盖旧正文。后续已改进主账号边界及累积懒加载覆盖验证，正在加载最新修复再次验证，尚不能认定 953 修复完成。

运行环境：普通 exec 启动器结束后服务退出，已改用持久 screen 会话承载既有启动脚本。目前最新会话 memorybread-docquality-0912-r3；日志 /tmp/mb-document-detached-restart-r3.log。必须看真实 PID/健康状态再请求，不得仅依赖启动日志。

剩余重点：完成 953 新快照应用和索引/UI核验；补齐已完整来源在后续部分回访时的保护与自动更新任务；验证模型产物与来源版本绑定；完成历史重放实际执行及端到端回归。原方案仍作为完整验收范围，不将局部通过视为交付完成。

### 最新实际状态（第二轮结束前）

- r3 持久会话已启动，Core PID 36285，UI PID 36710；需下轮重新核验 PID 而非直接沿用。
- 已通过产品接口 `POST /api/bake/documents/953/refresh`（manual=true）生成快照 61、62。collector 均为 document-body.v2，证明新正文采集器确实被执行；无需猜测扩展未加载。
- 快照 61：1139 字符、4 段、partial；快照 62：1104 字符、1 段、partial。两者 reached_end=true、stable_passes=2、redaction count=0，说明隐私误过滤已解除，但覆盖判定仍未通过。快照 62 的最终稳定读取循环已启用。不能将 partial 改写成 complete 来绕过验收。
- 953 原正文仍为 8106 字符，尚无 source head 或 body version。这是保护逻辑的预期行为，个案恢复仍未完成。
- 下一步优先把正文采集的覆盖判定证据（最终快照是否涵盖历史块、选择的 adapter、稳定次数、缺失块数量）作为安全统计持久化/返回，以定位真实 partial 原因。不要继续无证据猜测或反复重新抓取。
- Chrome 扩展管理页被 Browser URL 安全策略拒绝访问；未绕过。后续产品接口已证明新 collector 生效，因此不需要用户处理扩展设置。
- 仍需修复 legacy shell 正文在普通模型合并中被“必须保留全部旧内容”锁住的问题；以及完整 source head 后续收到部分新回访时的版本保护和自动补全。

最新日志：/tmp/mb-document-privacy-final-tests.log（2 passed），/tmp/mb-document-manual-tests.log（19 passed），/tmp/mb-document-extension-settle-tests.log（34 passed）。修复和回滚 Python 脚本测试 2 passed。

### 第三轮：953 完整来源实际应用

- 增加安全覆盖诊断日志，只记录布尔值、稳定次数和脱敏数，不写正文与 URL。真实日志先显示 final coverage=false / final stable=1 / redaction=0；据此排查编辑器从临时 DOM 转为行布局时的零宽字符和中文软换行。
- 正文移除零宽占位字符，覆盖比较忽略中文软换行但保留英文词间边界；最终稳定等待预算从 2.5 秒调整为 5 秒，仍受整体截止时间约束。部分结果也只保存一份实际观察快照，不再拼接不同渲染时刻冒充原文。
- 加入完整异步采集测试，稳定短文通过、显式虚拟化保持 partial；缺失章节及不同英文词仍拒绝覆盖。扩展测试 35 通过。
- 真实接口刷新在日志时间 2026-09-11T21:16:36Z 成功：document 953 的 source head=61，full_content=1139 字符，generation_version=document-source-v2，完整性 complete；旧 8106 字符正文保存为 body version 1。新摘要清空以免旧摘要冒充新版本。
- 真实 Chrome 页面初始渲染只含前三节及第四节开头；滚动后明确显示第四节余文、ref 和正文内的“灵机内测反馈收集表”链接，证实末尾引用属于正文，另一个页面引用面板则在正文根外。
- 发现实际旧 artifact_vector_index 尚存 22 条，补上来源切换事务内失效与现有 Qdrant 删除队列；迁移 116 清理已有 source head 下的旧索引。r5 已实际应用迁移：953 索引登记=0，删除队列=22，后续需确认清理与重建。
- RAG 版本化来源拒绝旧向量的相关性分数转移；当前来源向量保留 source_snapshot_id。专项 5 通过。Python 3.9 解析通过；迁移/重放/恢复 3 通过；前端当前构建通过。
- 版本事务/索引失效 Rust 专项 2 通过。随后统一正文 updated_at 与 head.applied_at 时间戳（避免毫秒偏差造成新索引误判过期），此最后改动正在专项回归，日志 /tmp/mb-document-source-epoch-tests.log，尚未装入 r5。
- 当前运行会话 memorybread-docquality-0912-r5，日志 /tmp/mb-document-detached-restart-r5.log。Core=61955，UI=62309，运行 PID 仍需重新核验。
- 剩余完整范围：普通模型合并对已完整来源的版本保护及旧 shell 更新策略；自动有界补全；历史漏处理候选重放；摘要/索引并发写入的来源绑定；部分来源用途约束；真实 UI、检索及回滚证据。不能以 953 单次成功替代整套方案交付。

本轮收尾证据：时间戳统一专项 2 通过；RAG 全文件加重放/恢复/迁移共 125 通过（2 条既有 Qdrant 警告）。实际 `7071 /references` 返回 document_id=953、新标题、1278 字符引用正文（含展示标题等），包含第四节且不含旧知识库目录；这是实际检索成功，不只是单测。953 旧索引登记已清零，Qdrant 删除队列仍 22 条，后台重建尚未验证。不要反复重启等待维护，否则会重置 5 分钟维护时钟；应继续检查现有进程并解决更新任务的优先调度。RAG serializer 当前尚未输出 source_snapshot_id（只在 materialize metadata 内），需贯通全部消费接口。

下一切片实现自动补全时需注意：普通合并共有四个调用点，现有 Result<()> 被上层当成“已合并”记账；不能简单入队后返回 Ok 从而再次制造假成功。应明确 pending 返回契约、任务状态、成功应用与仅检查的区别。旧观察指纹不能因为刷新成功就冒充新来源实际应用指纹。并发运行时追加的新观察必须留待后续处理，不能被旧任务完成覆盖；never 策略必须持续生效。

### 第四轮：持久化回访补全队列

- 新增迁移 117、document_refresh_queue repository 和 Core 后台 worker。观察按 document+fingerprint 去重，按文档合并待处理项；全局单租约，过期恢复，迟到结果不能覆盖新租约，新到观察不会被旧任务确认。三次失败阻塞，浏览器断开时不领取任务。
- 在线文档回访在 persist_document_artifact 的模型接受/质量创建门之前安排原文检查；保存来源关联，保留正文及已应用指纹。审计为 pending_source_refresh / source_checked / source_refresh_blocked，不增加文档创建数。历史 metadata-only 分支也会触发观察队列。
- source links 用 SQLite JSON union 合并，不修改正文和内容更新时间，避免来源关联更新触发无意义重建；已完整来源的元数据更新走专用链接写入。
- 自动观察更新允许越过常规 TTL，但不绕过 never、URL/身份/删除门禁；30 秒冷却，重试 30 秒/2 分钟。旧 snapshot reused 不算新观察已检查。登录/权限错误从扩展传递为稳定错误码以便阻塞。
- 历史重放脚本优先使用新观察队列，不依赖模型重新提炼；旧数据库兼容重试 lane。重复执行不会重复排队，写前备份已有重试和观察记录。
- 回归：bake_service 82 通过；document_refresh/queue/policy 21 通过；pending audit 专项 1 通过；Python 重放/迁移/回滚 3 通过及 Python 3.9 语法通过。Core binary cargo check 通过，随后 release 构建也通过。
- r6 持久会话已启动编译/切换流程，日志 /tmp/mb-document-detached-restart-r6.log。需确认启动完成后再应用 953 历史重放；本记录写入时尚未执行真实重放。
- 953 本轮确认正文仍为 1139/complete；旧向量删除队列已清空，新索引尚无记录。仍需补上版本更新的及时索引调度、索引写入竞争保护，完成真实 UI 验收及消费范围绑定。当前队列状态尚未接入详情 UI。

第四轮真实重放已执行：备份 `/Users/xianjiaqi/.memory-bread/repair-backups/document-update-replay-953-1789162668092249000.json`；五条 timeline 10070/10569/10652/10762/10764 加入 source_refresh lane。r6 的 worker 实际自动领取，第一次和第二次都读到 partial 快照 66（1161 字符），当前完整 head=61、正文 1139 字符保持不变。第二次结束后状态 pending、attempts=2，等待 2 分钟退避后的第三次；必须检查已有队列，不手工重置 attempt。快照 66 与 61 主要差异为排版空行，但确实缺少末尾 ref 和引用链接，不能仅归因于空白差异或把它标 complete。

r6 Core PID 81263、UI 81610；下一轮需复核。worker 把重复 partial 的原因误用 source_fingerprint_already_seen，已将这种结果统一为 COVERAGE_UNVERIFIED（代码刚改，尚未重新编译/加载）；不影响 pending/重试逻辑。后续应改善虚拟滚动覆盖，不能靠降低 complete 门槛完成真实重放。历史回访自动更新与 partial 保护已得到真实执行证据，但此批完整来源检查还未成功。

第四轮结束前最终状态：第三次自动抓取 job `6a0fb71f-365f-4ca3-9615-0ff97a2b16cf` 已结束；五条历史观察均为 blocked、attempts=3、checked_snapshot_id=66。正文保持 1139/document-source-v2，head 61 未被 partial 覆盖。日志时间 2026-09-11T21:41:40Z 仍为 covers_observed=false / stable=2 / redaction=0。不要重置重试计数盲目再抓；需先修复稳定正文/虚拟滚动覆盖，再进行显式受控恢复。最新 worker 代码（含失败原因修正）刷新测试 21 通过，日志 `/tmp/mb-document-worker-final-tests.log`；r6 未加载最后这处原因文案修正。

下一轮重点：1）采集器在恢复滚动位置后丢失已见末尾块，考虑可验证的块覆盖/修订证据，不能跨版本盲目 union；2）普通标题兜底与 identity-race 的 legacy merge 尚未全部改为 source queue/CAS；3）snapshot/head/derived 绑定与 UI 状态；4）优先更新来源版本的索引维护，当前后台普通积压使 953 重建仍未执行。用户目标保持实施中，此轮不是线程 blocked：仍有明确可推进的代码与验收工作。

### 第五轮：虚拟分页与及时索引

- 先冻结并校验正文，再恢复滚动位置；明确页面到底后会卸载头部段落，不能只靠最终单个 DOM。分页适配器新增 v3，两次完整遍历的页/位置/块文本必须一致，相邻视口必须重叠，任一位置冲突或有正文未映射到块则 partial。表格按单元格行列采集并还原，避免单元格虚拟卸载覆盖旧值；预算仍 60 秒/20 步。
- 第一版 v3 误漏 data-block-type 表格，真实逐段对比发现后立即纠正：snapshot 71 已备份并改为 partial；通过 restore version 3 恢复旧验证正文，备份文件 snapshot-71-coverage-correction-1789163445449182000.json、document-before-restore-953-1789163456521908000.json 均在 repair-backups。随后细化表格单元格校验，新增遗漏块、修订变化、视口跳块、表格行列与恢复滚动的回归。
- 修正后真实刷新再次 complete，head=61，正文1139字；核实表头、表格三项问题、四节及末尾引用均在。内容哈希复用旧快照，因此 snapshot 的旧 segment_count/collector 不能证明本次采集器；后端新增 positioned_matching_passes 安全统计，加载 r7 后需用新日志证明两次遍历实际执行。
- 已完整来源每30秒最多4份独立向量补齐，不再被普通 capture 积压长期饿死；保留普通后台批量维护。source_snapshot_id 贯通 loader/build/payload；完整短文可索引，未验证短文不因本改动放行。
- 向量写入在 Qdrant 前及 SQLite事务内检查 source head+updated_at；生成期间来源改变则拒绝登记并排队清理孤儿向量。RAG contexts serializer 添加 source_snapshot_id。
- 成功的新来源检查可确认检查开始前的 pending/blocked 观察，但不确认期间新到观察；记录 checked_snapshot_id，不冒充历史文本已合并。
- 测试：扩展40通过；文档向量与background_processor共89通过；Rust刷新21通过；source snapshot/ack 2通过；相关Python文件通过3.9语法检查。
- r7 正在编译/启动，日志 /tmp/mb-document-detached-restart-r7.log。需要检查真实状态后验证：953优先索引落盘、最新collector日志matching_passes=2、五条历史观察完成及真实UI。不得仅按本节推断运行时已加载。

第五轮运行时验收补充：r7 启动完成（Core 7704、sidecar 7423、model API 7442）。实际 POST 953 refresh 返回 complete/head 61；core.log 2026-09-11T22:00:58.868093Z 明确 positioned_matching_passes=Some(2)、covers_observed=true、truncated=false，证明新 v3 两次遍历实际执行。五条历史观察均 completed、attempts 保持3、checked_snapshot_id=61；这是新鲜检查确认，不是把历史片段冒充已应用。953 artifact_vector_index 现有3条，indexed_at=1789163737798 与当前文档版本一致。响应存 /tmp/mb-document-r7-refresh.json。尚未完成标题兜底/identity race 的统一队列、legacy merge CAS、UI队列状态及全消费路径验收，目标仍 active。

### 第六轮：统一观察更新入口与旧结果写入保护

- 提取 queue_existing_document_observation，共用于直接URL、已有来源、规范化URL、标题兜底及identity插入冲突；已有文档保存URL时统一排队，不再走模型拼接，不将待检查记成创建或已应用。新增无URL观察沿用已有来源URL的回归。
- 增加 update_bake_document_from_observation：同一SQLite UPDATE检查读取时updated_at+正文相等、未删除且不存在source head，防止模型等待期间或同毫秒并发更新后旧结果覆盖。普通显式文档编辑API仍用原方法。模型错误分支只写来源关联；失败的保护返回MERGE_PENDING且不登记应用指纹。来源元数据全量回写也走该保护，竞争失败退回链接合并。
- 存储回归14通过，日志 /tmp/mb-document-observation-cas-tests.log。服务82项与全库测试正在实际进程执行，handles 21183、49434；最后元数据分支调整另跑 refresh_document_source_metadata，日志 /tmp/mb-document-metadata-cas-tests.log。需检查最终结果，不能把在跑当通过。
- r7运行时尚未加载第六轮改动，953第五月采集/索引证据不代表这些新分支已完成运行时验收。剩余：标题兜底双方均缺URL的旧路径策略，结构化块/校验记录落盘、UI队列状态、来源用途与提炼版本全链路审核和最终真实验收。

第六轮验证进展：服务回归82通过（/tmp/mb-document-unified-observation-tests.log）。全库新增 stored_url 回归已通过，完整运行未结束：cargo PID93476、test PID94879，handle49434；元数据专项 cargo PID96087/handle54744 已开始编译。当前完整结果需继续读取 /tmp/mb-document-cas-full-lib-tests.log 与 /tmp/mb-document-metadata-cas-tests.log，不因观察超时重启。队列helper已覆盖4个旧merge调用前置及直接URL命中；无URL双方标题路径仍legacy且受CAS保护。还需审查显式通用update在改正文时sourcehead/派生产物失效契约，当前新CAS仅约束观察/模型写入。

### 第七轮：回访任务状态展示

- 文档列表/分页/详情新增 source_collection，读取队列优先running/pending/blocked/completed，避免旧成功掩盖待处理观察。详情显示正在采集、等待采集/重试、暂停原因和下次尝试下限；与正文覆盖状态分开。复用已有手动刷新按钮。
- 新增队列状态优先级断言及UI失败任务不污染完整正文测试；BakeDetailDisplay 18通过（/tmp/mb-document-queue-ui-tests.log），tsc --noEmit运行handle17464，日志 /tmp/mb-document-queue-ui-types.log。Rust document_refresh编译/测试handle65019（/tmp/mb-document-queue-status-tests.log）尚待最终结果。
- 上轮 metadata CAS 专项1通过（/tmp/mb-document-metadata-cas-tests.log）；完整库handle49434仍live，PID93476/test94879，继续读取原log不可重启。第六七轮均未加载到r7应用。剩余：结构化块/校验记录持久化、消费来源范围/派生版本和通用手动编辑失效契约、最终新版本运行时/UI验收。状态字段目前随文档读取刷新，未新增轮询。

第七轮最终测试结果：上一轮完整库601通过、1忽略、0失败（369.30s）；本轮Rust document_refresh 21通过（0.92s），包含状态优先级断言；UI 18通过，tsc --noEmit退出0。handles49434/65019/17464均已完成，无需再等待或重启测试。新UI和API字段尚未在r7实际进程验证；这仍非最终交付。

### 第八轮：通用正文编辑的来源失效

- update_bake_document_guarded改为SQLite事务：正文/来源身份改变时，有source head则解除关联、覆盖标记unverified并清旧摘要/结构化正文/提示；保留不可变快照。正文、标题、来源身份或删除变动时原子删除artifact登记并入Qdrant删除队列。元数据单独变动保留来源关联，模型观察CAS仍生效。
- 新增手动正文编辑、来源改换、纯标题编辑、旧索引与快照保留测试。第一轮14通过1失败：测试中的未知URL双方canonical=None，被误当作相同身份。已修编辑判断，在canonical未知时回退原始非空URL严格比较。修正重跑handle59775，日志 /tmp/mb-document-edit-provenance-tests.log（尚未终态）。
- 新发现需继续解决的全局身份缺口：storage/document_identity.rs仍对路径整体lowercase、全部去query；apply_document_source_snapshot仍允许canonical(None)==canonical(None)通过。方案明确要求身份碰撞保护，不能仅靠本轮编辑局部fallback宣称该项完成。需要通用保守身份规则及旧身份迁移/别名兼容与测试。
- source_snapshot_id=None的向量写入source_current仍直接True，需保护普通文档编辑期间的向量写入竞态（已绑定snapshot的向量竞态已保护）。结构化块持久化/全消费路径/最终运行时验收仍未完成。未重启r7加载本轮修改。

第八轮修正后存储回归15通过、0失败（17.14s），/tmp/mb-document-edit-provenance-tests.log；handle59775已终态成功。新增未知URL来源改换回归通过。全局URL身份/快照应用碰撞仍需下一轮处理，不能混同为已根治。

### 第九轮：未知来源身份的独立校验

- apply_document_source_snapshot不再把None==None作为身份通过：未知URL仅允许完全相同且合法HTTP(S)、有host无内嵌凭据。测试伪造identity_match=true但不同未知URL被拒绝，原正文保留；同一未知合法URL可应用。存储16测试通过，/tmp/mb-document-source-identity-guard-tests.log。
- 标题兜底双方均有未知URL时严格比较，避免旧_=>true错误合并；只有一方缺URL才可继续标题兜底。测试handle81868，/tmp/mb-document-title-identity-tests.log尚待终态。
- 全局身份升级尚未实施。已定位：document_identity.rs整体lowercase/去所有query；bake_service normalize_doc_url另有独立去query实现；repo find_document_by_source_url查询信任旧stored_identity；db.rs迁移062特殊入口与唯一索引，迁移110已处理旧重复。下一轮需versioned身份、保守query/path规则、原ID不变重算与别名审计，不能只改canonical导致旧查询漏命中或唯一键失效。
- 本轮未切换r7应用。结构化块持久化、普通无snapshot向量CAS及消费范围/最终运行时验收继续待办。

第九轮标题兜底专项handle81868退出0，最终日志 /tmp/mb-document-title-identity-tests.log。未遗留在跑测试。

### 第十轮：版本化URL身份与迁移

- canonical_document_identity改为document-url-v2:加解析后的URL，保留协议/path大小写/尾斜杠/未知query；仅分页编辑器路径忽略section、ro=true/false和章节fragment。host默认端口交给URL解析器；文档识别仅检查host/path，禁止query内伪装/document/及内嵌凭据。bake_service normalize_doc_url不再自行去所有query。
- 新迁移118_document_identity_v2：事务内把旧identity归档到bake_document_identity_aliases，重算活动记录。重复新身份只一个记录持有唯一键，其他ID/正文/来源记录保留，不按别名绕过新版校验。db.rs新增特殊迁移入口及重复运行/大小写旧碰撞/原ID正文保留测试。尚未在实际用户库执行118，r7仍运行旧binary。
- Python embedding document_chunks共享归一化用于creation.service._canonical_memory_url、RAG retriever和pipeline；RAGmaterialize从source_url重新建比较键，不信任旧stored_identity。新shared/document-quality/url-identity-v2-cases.json覆盖12个跨语言语义案例。当前Python一般HTTP URL可保守比较；Rustcanonical仍有限定文档host/path的识别门，未知URL走已有严格fallback。
- 旧测试将任意query/from、协议/路径大小写变化视同源，与新契约冲突；调整同源夹具为明确view参数，并新增unknown query/path/scheme不同身份断言。Python最终document_vectors+creation_references+rag共198通过、2既有Qdrant连通性警告；日志/tmp/mb-document-identity-v2-consumer-final-tests.log。相关7个Python文件3.9语法检查通过。
- Rust document_专项70通过（/tmp/mb-document-identity-v2-final-rust-tests.log），包含迁移新测试但启动时尚未加入最新shared fixture test。旧整库运行在修正旧夹具前编译，601通过4失败1忽略，失败均为已更新的旧等价假设，日志/tmp/mb-document-identity-v2-full-tests.log。最终整库已启动handle38673，日志/tmp/mb-document-identity-v2-acceptance-rust-tests.log，需继续等待终态；不能把70专项当作最终全库通过。
- 后续：先查最终Rust测试；实际迁移前备份DB并加载新binary验证953同ID/正文/来源检查和任务状态UI；结构化块/校验记录持久化、无snapshot向量写CAS、完整消费范围与派生绑定审计仍待完成。不要标goal complete。

### 第十一轮：实际迁移、索引竞态与界面验收

- 最终Rust整库606通过、1忽略（/tmp/mb-document-identity-v2-acceptance-rust-tests.log，137.64s），无遗留失败。
- 普通无snapshot文档向量写入也校验真实owner/deleted/updated_at/source head，生产loader传source_body_hash，Qdrant前后再比正文hash，防同毫秒编辑；缺owner或缺版本拒绝。新增真实owner测试夹具与无snapshot同毫秒竞态测试，document_vectors+background_processor 92通过（/tmp/mb-document-all-source-vector-cas-tests.log）；相关Python3.9语法通过。
- 备份 /Users/xianjiaqi/.memory-bread/repair-backups/before-identity-v2-1789190480819551000.db（约879M，quick_check ok），配套baseline.json保存每份原文档正文hash/URL/创建时间/旧identity；文件权限600。路径索引 /tmp/mb-document-identity-v2-backup-path.txt。
- r8已完整启动并加载代码：Core30125、sidecar29912、modelAPI29938、creation29997、UI30536（launcher30299）。日志/tmp/mb-document-detached-restart-r8.log。真实118迁移通过，965条原记录无丢失；对比备份全部正文hash、source_url、created_at未改，433旧身份已归档。报告/tmp/mb-document-identity-v2-migration-live.json。953保持1139字符/head61，5历史观察completed。
- 实际重新获取953返回complete，table三表头/末尾ref存在，core.log 2026-09-12T05:25:42Z matching_passes=Some(2)、covers_observed=true、redaction0。响应/tmp/mb-document-identity-v2-refresh-live.json。实际7071/references召回953，source_snapshot_id=61，报告/tmp/mb-document-identity-v2-rag-live.json。
- CUA原生MemoryBread名称不可解析且不在app inventory；使用同一运行时的localhost:1420 Chrome开发界面进行UI验收（实际Vite仅::1监听，127.0.0.1会拒绝，勿据此重启服务）。按ID953搜索并打开详情，界面显示完整快照/完成来源检查，正文含表格、四节及ref。不是原生窗口直接验收。
- 截图发现来源plain text被Markdown软换行折叠，已新增contentFormat mapper从document-source-v2映射plain_text，详情用pre-wrap原文显示；其他Markdown沿用原渲染。19界面测试通过（/tmp/mb-document-source-lines-ui-tests.log），tsc noEmit退出0。浏览器刷新后截图确认换行恢复；改动HMR已加载，无需为此重启。
- 当前无在跑测试/API请求。CUA变量mbQualityTab=Chrome2/tab1972566948，URL http://localhost:1420/，已搜索953并打开详情，继续后续UI验收可复用；source953V2=1972566905为原文只读页面。
- 剩余完整目标：结构化正文块/定位引用/质量版本及每次检查证据落盘（当前v3只持久化flattened正文与部分覆盖统计，hash复用还保留旧collector元数据）；共享partial/全文用途限制和全部创作派生source_snapshot_id绑定审计；总体验收/必要回归和文档交付。不可将本轮真实953成功当作全方案完成。

### 第九轮：每次检查证据及创作来源绑定（实施中）

- 新增迁移 119、只追加的 `bake_document_source_checks`。同一内容快照被复用时仍保存每次检查的采集器版本、覆盖证据、脱敏数及正文块引用；不覆盖历史检查。外键及写入校验禁止引用其他文档的快照。
- 正文块引用只指向已脱敏快照的 UTF-8 字节范围，带类型、顺序、哈希和白名单数字定位；不持久化原始块文本、页面属性或任意元数据。通用正文识别标题、段落、列表、代码及表格，虚拟分页保留逐格定位。最新检查可能对应较新的 partial 快照，消费时必须核对 snapshot_id，不得把它的范围套到当前旧完整正文。
- 真实完整采集证据增加 reached_end、truncated、stable_passes、segment_count、character_count 白名单；快照复用不会丢掉本次验证结果。详情 API 返回最新检查，列表避免加载所有块引用。
- 创作刷新此前替换 full_content 却保留旧 summary/sections/style/prompt，现清空这些旧派生内容，并绑定 source_snapshot_id、正文 SHA-256；Agent 上下文、引用状态和预览保留版本字段。数据库召回在同一 SELECT 中校验来源 head 的文档和正文一致才携带快照 ID，旧库返回 NULL。尚需真实运行与部分范围的最终验收。
- 验证：document_ Rust 73 通过（此前块引用版本）；最终检查证据专项 2 通过，日志 `/tmp/mb-document-check-completeness-tests.log`；扩展 41 通过，日志 `/tmp/mb-document-semantic-blocks-js-tests.log`；创作引用及 Agent 回归 265 通过，日志 `/tmp/mb-document-creation-binding-regression.log`；同文档/精确正文/旧库绑定专项 1 通过，日志 `/tmp/mb-document-creation-binding-sql-tests.log`。5 个改动 Python 文件通过 3.9 AST 检查。
- 备份代码复核：ASSET_TABLES 是 legacy v1 清单；现行 v2 使用完整 SQLite 快照，因此不能仅因旧清单未列新表就断言新版导出漏表。仍需真实导出/导入及来源指针一致性验收。
- 已启动 r9 持久重启加载本轮改动，日志 `/tmp/mb-document-detached-restart-r9.log`。当前正构建，后续须核对健康与迁移 119，再走真实 953 刷新，不能把测试成功当运行成功。

完整范围继续保留：当前改动的真实页面/创作消费验收，partial 块范围和消费一致性，导入/恢复对版本链的保护，最终全部需求逐项核验。未交付。

第九轮真实运行结果：r9 release 构建及关键 API 自检通过，迁移 119 已实际创建。产品 `POST /api/bake/documents/953/refresh` 返回 updated/complete，快照 76、1135 字符，当前 head=76。最新检查 id=1，采集器 document-body.v2、paginated_editor、62 个正文块（text/table），unmatched=0，所有 UTF-8 范围哈希与快照一致，redaction=0、reached_end=true、stable=2、truncated=false。证据 `/tmp/mb-document-source-check-r9-refresh.json`、`/tmp/mb-document-source-check-r9-evidence.json`。本次不是虚拟变化页面路径，不应称为 v3 实测。

真实创作预览尚未通过：`8001 /creation/references` 查询“快手灵机 非L0商家 账号矩阵 分销模式”两次均未在 Top 10 返回 953。接口正常返回，候选 936、合格 240、relevance_threshold 过滤 696，需继续定位 953 具体在哪一步退出/降权；不能靠扩大 Top-K 或更换查询宣称修复。诊断 `/tmp/mb-document-creation-binding-r9-diagnostics.json`，包含各候选 ID/排序理由。当前评分仍以 summary/prompt_hint/sections 给文档质量和格式加分，完整来源更新会清空这些旧派生字段，这是待证实的降权原因之一，不应直接修改权重绕过。下一步追踪 953 的候选载入、评分、同源仲裁和最终多样性选择，再用相同查询验收。

### 第十轮：创作排序与导入保护，以及分页预览漏尾修复

- 同一查询的只读评分追踪确认 953 已进入候选，相关性 0.7462，却因无旧摘要/提示词/章节被质量分 0.61 和格式分 0.2 压到第 12；不是未采集或候选 SQL 丢失。追踪 `/tmp/mb-document-953-ranking-trace.log`。
- 来源召回在同一 SELECT 中核对文档 ID、精确正文、identity_match，返回来源完整性；完整且绑定一致的原文质量/完整性按验证证据评分，不依赖已清空的模型派生字段，不改变格式分或全局权重。无绑定、partial、身份不符仍不享受完整来源评分。
- 创作引用/Agent 回归 267 通过，日志 `/tmp/mb-document-source-quality-ranking-tests.log`，Python 3.9 通过。r10 实际加载后，同一查询与同一 Top 10 返回 953 第 1，snapshot=76，hash 与当前正文一致，score=0.7058；证据 `/tmp/mb-document-source-quality-ranking-r10-live.json`。随后 76 被发现覆盖不完整并纠正，因此该证据只证明版本绑定/排序，不证明全文正确。
- 新增完整资产导入测试暴露历史快照可附到已编辑的本机正文。已在导入来源指针前校验 owner、精确正文、身份、complete 和未删除状态，并按 document_id 保留本机已有 head，禁止将 owner 主键当可自增 ID。导入整个模块 7 通过（`/tmp/mb-document-source-provenance-import-regression.log`）；增加本机已有 head、快照 ID 碰撞后专项 1 通过（`/tmp/mb-document-source-import-local-head-tests.log`）。这项 Rust 修复尚未加载 r10。
- 对 61/76 逐段比较发现 76 缺末尾 TVC 句及 ref 链接，差异不是空白。根因：分页编辑器临时预览 DOM 稳定但无 positioned blocks，v3 返回 null 后落到 v2 普通页完整判定。现在最多额外等待 5 秒布局就绪，且 paginated_editor 回退路径一律 partial；不能将稳定预览证明为完整。扩展 41 通过，包含新增分页预览拒绝 complete 场景；日志 `/tmp/mb-document-paginated-preview-tests.log`。
- 76 已备份并改 partial，追加更正检查记录（保留原检查）；通过归档 version 5 恢复已验证的 1139 字符正文。备份 `snapshot-76-coverage-correction-1789192760496647000.json`、`document-before-restore-953-1789192760589582000.json` 均在 repair-backups，权限 0600。恢复脚本已补原子失效 artifact_vector_index + durable Qdrant 删除，避免恢复后消费旧索引；相关 3 项通过，Python 3.9 通过，日志 `/tmp/mb-document-restore-index-tests.log`。
- 当前再次通过真实产品刷新验证新 JS，执行句柄 16869，报告目标 `/tmp/mb-document-paginated-preview-r10-live.json`。必须读取结果及快照块证据再判定；不要根据前次 complete 字样交付。当前运行 r10，日志 `/tmp/mb-document-detached-restart-r10.log`。

第十轮收尾采集证据：真实刷新完成，当前 head 恢复为 61、正文 1139 字符、fresh_complete。重要区别：source_snapshot.collector 仍是历史 v2，但最新 source_check id=3 明确记录本次 **v3**，两次完整定位块遍历一致、14 步、36 个块、unmatched=0、所有范围哈希通过、最后块到达正文末尾；没有脱敏或截断。证据 `/tmp/mb-document-paginated-preview-r10-evidence.json`。这同时验证快照复用时本次检查证据独立保留。再次同查询创作预览正在执行（句柄 3534，报告 `/tmp/mb-document-creation-complete-r10-live.json`）；后续读报告，不重复盲请求。导入 Rust 修复还需加载运行，其余完整方案验收范围不缩减。

同查询创作最终本轮结果：953 排第 1，source_snapshot_id=61，正文哈希与已恢复完整正文一致（`/tmp/mb-document-creation-complete-r10-live.json`），句柄 3534 已结束。已启动 r11 持久重启以加载导入指针修复，日志 `/tmp/mb-document-detached-restart-r11.log`；后续先核验进程与健康，不凭超时重启。剩余重点：版本导入实际恢复验收、partial 引用范围贯通、最终 FR/AC 逐项验收与统一报告；当前没有真正阻塞，目标保持实施中。

### 第十一轮：实际片段范围与消费时的版本一致性

- r11 已完成启动及关键 API 健康检查，导入来源指针保护已加载。当前 953 head=61，三个 artifact 索引时间戳均与 applied_at=1789192811740 一致。
- 新增 `creation/source_scope.py`：正文引用记录实际提供的 UTF-8 范围、片段哈希、整份来源哈希、snapshot_id、来源覆盖及本次截短状态。保留原始换行和空白，不对归一化文本伪造偏移。再次缩短上下文时同步缩短范围并更新片段哈希；版本或文本不匹配时拒绝该条引用。
- `allows_full_document_claims` 只有来源 complete 且本轮片段未截短才为真；Agent 所有生成角色均获得对应使用限制。部分快照仍可支持实际取得段落，不能用于未读章节。无有效正整数 snapshot ID 的刷新响应不再升级为 fresh，保留历史正文并记录刷新失败。
- `_merge_reference_states` 原来只按 final_weight 保留更高分引用，可能阻止稍低分的新正文替换旧版本。现后来取得的不同版本按正文/快照绑定整体替换；无绑定的旧摘要不能覆盖已绑定版本；多次合并仍保留历史匹配步骤。
- 咨询 `materialize_documents` 使用一个明确的 SQLite 读取事务覆盖身份、head 和正文读取。新增 WAL 并发测试在读取 head 后提交新正文/新 head，验证结果仍只包含一致的旧版本，后续数据库确实已是新版。
- 最终联合回归：创作引用、Agent、片段范围和 RAG 共 402 通过（2 条既有 Qdrant 兼容性警告），日志 `/tmp/mb-document-version-scope-consumers-final.log`。新增及修改的 Python 通过 3.9 AST 检查。范围专项包含 Unicode、换行、反复截短、错误哈希/快照、partial 禁止全文结论、版本替换与匹配步骤保留。
- 已启动 r12 加载本轮 Python 修复，日志 `/tmp/mb-document-detached-restart-r12.log`；先核验该会话实际进程/健康，不因观察超时重复重启。

剩余交付工作继续按完整方案：最新运行时消费/界面验收，导入与恢复的真实隔离副本证据，FR/AC/NFR/OBS/ROLL 逐项验收清单及最终报告。以上测试通过不自动等于全部完成。目标未阻塞、未交付。

### 第十二轮：真实隔离恢复与逐项交付审计

- r12 已启动并通过关键 API 健康检查，运行窗口 PID 95856；本轮 Python 片段范围及咨询读事务修复已加载。
- 真实相同查询：8001 创作预览第 1 条为 document 953/source_snapshot_id 61；7071 咨询 references 第 1 条为 document_id 953/source_snapshot_id 61。报告 `/tmp/mb-document-r12-creation-live.json`、`/tmp/mb-document-r12-rag-live.json`（0600）。
- 从当前数据库经 SQLite backup 创建隔离副本 `/tmp/mb-document-source-acceptance/source.db`（905109504 字节，目录 0700、数据库 0600），没有对当前生产文档执行本轮恢复测试。
- 另一个隔离 restore.db 上实际运行恢复工具：dry-run 不变；恢复归档 1 的旧外壳后仍被共享质量门识别，source head=0、artifact index=0；恢复归档 5 后得到 1139 字符可靠正文但状态仍 unverified，未伪造来源 head。采集 31676、时间线 12899、创作历史 154、收藏 1、文档 965 的数量均不变，953 ID/创建时间/来源关联不变，其他 964 个文档整行哈希不变。证据 `/tmp/mb-document-real-restore-acceptance.json`。
- 扩展现有外部数据库快照验收，加入 source_heads/checks/body_versions 数量及 owner/body 一致性校验。实际 source.db 完整导出/导入正在运行：exec 9891，进程 96817（最近核验 CPU 活跃，非超时终止），日志 `/tmp/mb-document-source-real-import-tests.log`。不要因 over 60 seconds 的测试提示重跑；它在处理约 900 MB 真实副本。
- 全量 Rust 也正在运行：exec 90280，日志 `/tmp/mb-document-final-rust-regression.log`，新测试输出仍在增长。后续读取两条现有任务结果。
- 新增 `doc/document-source-quality-acceptance-2026-09-12.md`，按 FR/AC/NFR/OBS/ROLL 共 32 项记录已有证据和未验证项。未完成项包括最终 UI 复核、显式取消与配置路径、审计统计覆盖、停止新任务/回滚开关，以及运行中的最终回归；不得把它们省略后宣称交付。

### 第十三轮：取消协议补齐与最终回归结果

- 上轮全量 Rust 已结束：609 passed、1 ignored、0 failed，206.09 秒，日志 `/tmp/mb-document-final-rust-regression.log`。这是新增取消协议前的全量结果，不能冒充协议改动后的全量结果。
- 取消能力核对确认此前只有超时/断线清理，尚无文档任务显式取消。新增 broker.cancel_job：原子移除排队与 pending、返回 SOURCE_REFRESH_CANCELLED、保留 cancelled 状态、丢弃预览；迟到 progress/result 不得复活任务或返回正文。Native Bridge 响应增量携带 cancelled_job_ids，扩展即使 busy 也处理取消并关闭对应后台标签；标签创建期间收到取消时在创建后立即中止，取消 ID 集合有界。
- 此协议层 Rust 9 项通过（`/tmp/mb-document-browser-cancel-rust-tests.log`），扩展 Service Worker 3 项通过（`/tmp/mb-document-browser-cancel-js-tests.log`）。源码尚未部署至 r12，也尚未接通文档 ID -> browser job 的取消入口，因此 NFR-003 仍未完成，不宣称已具备产品取消能力。
- 下一切片：给浏览器任务增加后端私有 cancellation scope，document refresh 使用 document ID scope；接通文档取消/停止自动补全入口；取消时阻止快照应用及队列迟到确认；增加运行和发布版本兼容验收。不要只取消 HTTP 等待而让迟到结果改正文。
- 真实副本导入测试 exec 9891 / PID 96817 仍运行，最后核验 elapsed 08:35、CPU 100%、RSS 426784 KB，未终止。未因超过 60 秒重复启动。已发起只读执行栈采样以定位耗时：输出 `/tmp/mb-document-real-import-sample.txt`（采样句柄以实际工具返回为准）；后续优先看已有进程和日志 `/tmp/mb-document-source-real-import-tests.log`。
- 当前实际运行仍为 r12。最终验收清单中的显式取消、审计统计/开关、UI 复核和真实完整导入结果仍待完成，目标持续推进，无真正阻塞。

取消协议收尾：修复一个 Native Host 无效消息响应初始化缺字段的编译错误后，Rust broker 9 项全部通过；JS Service Worker 3 项通过。取消协议仍未接文档入口，勿部署后宣称可取消文档。采样句柄 27692 已结束，执行栈仅有未符号化地址，不能据此判定具体 SQL 慢点；它只确认测试线程仍在运行。不要用后来重建的二进制为旧进程地址强行符号化。真实导入进程继续等待原句柄 9891。

### 第十四轮：文档取消入口与提交互斥

- 浏览器任务增加后端私有 cancellation_scope（不序列化给页面），broker.cancel_scope 同时记录短期取消标记并取消已注册任务；取消早于提交浏览器任务也会被拒绝，避免注册竞争漏取消。不同刷新采用不同 scope，不永久阻止以后重取。
- 文档刷新 lease 从单纯去重改为 running/cancelled/committing 原子阶段。`POST /api/bake/documents/:id/refresh/cancel` 接受取消后，采集结果不能进入快照写入/正文应用；已进入提交阶段返回 finishing，没有活动或待处理任务返回 idle，不谎报取消成功。浏览器错误保留 SOURCE_REFRESH_CANCELLED。
- 取消将当前 pending/running 观察设为 blocked、清租约并保留取消原因；迟到 worker finish 不得复活。观察任务在提交前核验原租约仍有效，覆盖“worker 已领取但尚未注册运行 lease”的竞争窗口。新访问仍可建立新的观察，取消不改变长期刷新策略。
- 前端详情增加“取消本次刷新”，手工运行或 pending/running 自动观察时可用；显示取消、正在提交或无任务的准确状态，原文继续显示。
- 验证：scope broker 10 通过，日志 `/tmp/mb-document-cancel-scoped-broker-tests.log`；文档取消与提交互斥/队列取消专项上一轮 23 通过，最后 idle 响应改动后正在复跑，exec 以实际返回为准，日志 `/tmp/mb-document-cancel-entry-final-tests.log`。前端详情 20 通过（`/tmp/mb-document-cancel-ui-final-tests.log`），最终 TypeScript 检查正在执行（`/tmp/mb-document-cancel-ui-final-types.log`）。
- 当前线上开发运行仍 r12，取消入口的 Rust/扩展 Service Worker 尚未实际加载验证，不称为取消功能已交付。后续需要加载 Core 和更新扩展 Service Worker，再做真实取消页签/迟到结果/UI验收；旧扩展可忽略新增字段，Core 仍不能接受取消后的结果。
- 真实副本完整导入测试仍是 exec 9891/PID 96817；最近核验 elapsed 16:58、CPU 94.7%，进程未结束。没有因观察超时重复启动。其他待验收项继续见 acceptance 清单。

第十四轮收尾：最后改动后文档 refresh 23 项通过（exec 41620 已结束）；TypeScript 检查通过（exec 38576 已结束，空日志/退出码 0）。当前已通过 scope broker 10、文档 refresh 23、UI 详情 20；真实取消仍待部署验证。实际数据库导入原任务继续运行，最新 elapsed 18:26、CPU 96.7%、RSS 708240 KB。

### 第十五轮：真实取消、诊断统计和运行配置

- r13 已成功启动。真实 953 排队和 running 两种阶段均实际调用取消入口，浏览器 broker 活动任务数变为 0，正文/关联/创建时间哈希、head 61、正文归档数 3 和来源快照数 8 均未变化。running 证据 `/tmp/mb-document-r13-cancel-running-live.json`。失败检查以 snapshot_id=null、SOURCE_REFRESH_CANCELLED 追加，保留取消审计；第一版验收脚本错误要求检查数量不变，已经按实际审计契约纠正。尚未证明扩展 Service Worker 已更新并即时关闭物理标签，不能把 Core 取消证据扩大到这部分。
- 诊断新增正文根内部排除子树数量，明确 exclusion_scope=final_body_root，不冒充全页或所有遍历步骤统计；Rust 记录脱敏前字符数、实际删除字符数和比例。重叠脱敏区间按并集计算 Unicode 字符数量，不拿替换占位符后的长度差冒充删除量。白名单检查保存上述排除统计。
- 配置接现有偏好键 runtime.document_source_refresh，包含总开关、自动开关、扩展采集秒数/步数、轮询间隔、尝试上限和退避间隔。偏好入口拒绝非法配置，worker 每轮读取；暂停保留观察且不消耗未执行尝试。配置说明见 document-source-refresh-runtime-config.md。旧 Apple Events 预算/主动终止、规则灰度和监控范围尚待补齐。
- 验证：扩展 44 项通过（content 41 + worker 3）；内容过滤 3 项、source checks 2 项、刷新配置/取消/队列 26 项通过；前端详情 20 项通过。新增配置后的全量 Rust 617 passed、1 ignored、0 failed，172.64 秒，`/tmp/mb-document-config-full-rust-tests.log`。Python 本轮未修改。TypeScript 原句柄 54032 待读取退出状态。
- 原真实导入 PID 96817 在第 33 分钟仍存活，但已查明夹具错误：数据根下 source.db/restore.db 被普通附件收集，绕过被测数据库脱敏导出路径。基于此明确原因中止原测试（SIGINT，非观察超时重启）。原日志保留 `/tmp/mb-document-source-real-import-tests.log`。测试改为 SQLite backup 到独立临时根的标准 memory-bread.db，并断言 local_file_count=0。
- 修正夹具后第一次测试终态 DiskFull（96.60 秒，`/tmp/mb-document-source-isolated-import-tests.log`）。确认并清理中止测试遗留 `/var/folders/yr/mj_wg3_10kl86vw8xq_vsvk40000gn/T/.tmpo53JDb/external-assets.mbsnapshot.json`：先验证 manifest.source_db_path 为本任务 source.db，移除 2580205845 字节可重建临时产物，原数据库/恢复副本/修复备份均保留。空间由 2.9 GiB 恢复至 5.3 GiB。
- 正确夹具现已重新启动实际导入，exec 16330，日志 `/tmp/mb-document-source-isolated-import-retry.log`；须读取此任务结果，不再等待已结束的 9891/78620。
- r14 release 构建完成（1m08s），重启终态失败于 Ollama 进程登记时 PID 已消失，未把失败当成已部署。已用 start.sh start 发起 r15 恢复服务，screen memorybread-docquality-0912-r15，日志 `/tmp/mb-document-detached-start-r15.log`；应核验原启动进程/健康后再做配置运行验收。

目标继续保持完整范围；当前不是最终交付。剩余逐项证据仍见 acceptance 清单，尤其兼容采集取消/配置、完整导入、全链路统计/灰度、最终 UI 与运行恢复。

第十五轮收尾：r15 已成功恢复全部服务，Core PID 52957、UI 窗口 54411，配置已加载。真实 PUT 暂停后手动刷新返回 SOURCE_REFRESH_PAUSED、953 正文哈希/head 不变；非法 max_steps=0 返回 400；原偏好不存在状态已准确恢复。证据 `/tmp/mb-document-r15-runtime-config-live.json`。r15 浏览器 UI 已打开 953，正确正文末尾“灵机内测反馈收集表”可见；取消后显示“本次刷新已取消，原正文保留”，不再直接显示错误码。最终 UI 20 项及 TypeScript 检查通过，日志 `/tmp/mb-document-readable-status-ui-final-tests.log`、`/tmp/mb-document-readable-status-ui-final-types.log`。原 TypeScript 54032 也已返回 0。本轮所有受影响文件 diff --check 通过。

真实完整导入继续执行原句柄 16330/PID 53773，最近核验 elapsed 05:06、CPU 100%、RSS 1900976 KB，非终态，不重启。CUA 浏览器 2，页面 1972566948，变量 documentUiTab，当前停留文档 953 详情并标记 handoff；后续继续使用既有页面。仍未证明扩展 Service Worker 更新及物理页签取消，不能将 Core 取消当成该证据。配置兼容路径/灰度/监控和原方案其他未验收项保持待办。

### 第十六轮：兼容采集终止与真实导入引用修复

- Apple Events 新增 ScriptControl，采集线程局部作用域绑定取消标记及截止时间，互斥锁等待也响应取消；子进程管道并发排空，取消/超时后 kill+wait 回收；窗口清理使用独立最多 5 秒预算。r16 已运行此实现。真实取消证据 /tmp/mb-document-r16-legacy-cancel-live.json：约 0.207 秒结束，已观察脚本被回收，953 正文关联哈希/head 61/归档数 3 不变。预算证据 /tmp/mb-document-r16-legacy-budget-live.json：1 秒配置实际约 1.214 秒返回 SCRAPE_TIMEOUT，正文/head 不变。测试临时配置均恢复原状态。
- 浏览器失败原因改为静态错误码白名单，未知错误保持通用原因，不保存任意页面消息；前端补充可读失败说明。失败码专项 1 项通过，前端 20 项通过。兼容控制改动后全量 Rust 622 passed、1 ignored，日志 /tmp/mb-document-legacy-and-import-full-tests.log；此结果不覆盖之后的所属主键修复。
- 正确夹具真实导入 16330 终态失败 integration_import_items 外键。原副本存在大量已清理 capture 的悬空引用，导出副本现按声明外键及 JSON ID 数组补无正文占位，以 snapshot_ref_missing:<原始 ID> 区分不同缺失来源；再次导出保留标记，源库不变。相关小型专项通过。
- 真实导入 68895 终态失败 operation_replay_queue 外键，耗时 976.39 秒，日志 /tmp/mb-document-pruned-reference-real-import.log。运行中采样明确处于 merge_database_snapshot/find_existing_merge_row/SQLite 查询，未按观察超时重启。根因是主键兼所属外键的行被当独立主键分配；现统一按映射后的所属键保留本地状态，源映射使用重映射前 ID。新增碰撞/重复导入/本地完成状态/外键完整性回归通过。
- snapshot 专项 9 项通过，日志 /tmp/mb-document-owner-key-tests.log，其中外部真实库用例无环境变量时并未执行外部导入。已单独启动真实 900 MB 副本测试，句柄 66535/PID 58024，日志 /tmp/mb-document-owner-key-real-import.log；不得提前计为通过。
- 当前未完成项仍包含该真实导入结果、扩展物理页签取消、最终成功刷新/UI，以及统计/灰度/回滚范围。953 最近成功来源检查仍为 3，后续取消/超时检查为失败审计，不能称当前刷新 fresh。

### 第十七轮：失败后详情同步与检查耗时

- 上一轮所属主键修复后的全量 Rust 已完成：624 passed、1 ignored、0 failed，413.34 秒，/tmp/mb-document-owner-key-full-tests.log；原句柄 5777 已读取退出 0。
- 从真实 UI 再次刷新 953，来源检查 9 返回 BROWSER_EXTENSION_TIMEOUT。发现失败响应 document=null 时打开的详情继续显示旧原因；BakePanel 现对无 document 的正常返回重新请求当前列表，使原详情同步新鲜度/失败原因。新增真实组件网络回归证明失败返回后更新原因且正文保留。前端 BakeSearchFlow + BakeDetailDisplay 共 44 项通过，TypeScript 退出 0，日志 /tmp/mb-document-failed-refresh-ui-tests.log、/tmp/mb-document-failed-refresh-ui-types.log。
- 实际 UI 热更新后执行刷新并取消，详情不关闭即可从超时切换为“本次刷新已取消，原正文保留”，检查 10 记录 SOURCE_REFRESH_CANCELLED。正文 SHA256 460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8、head 61 保持不变。证据 /tmp/mb-document-r17-failed-refresh-ui-live.json。Chrome 采集标签 1972567042 仍存在，不能称物理标签取消已通过；Core broker 当前 active=0，扩展报告 0.2.3。
- 来源检查新增 started_at_ms/finished_at_ms/execution_ms，耗时使用单调时钟；范围是检查开始到构建审计，不含排队、后续正文应用和派生重建。成功/部分及已记录失败路径均接入。专项 12 项通过，/tmp/mb-document-check-timing-tests.log；该改动尚未重建到 r16 运行 Core，不能以本轮真实 UI 报告宣称时间字段已运行验收。OBS-002 聚合、排队/重试与 OBS-003 仍未完成。
- 真实完整导入继续原句柄 66535/PID 58024，日志 /tmp/mb-document-owner-key-real-import.log；最近读取仍活跃，未因观察超时重启。下一步读取该原任务终态。文档页面继续使用 CUA browser 2 / documentUiTab / 1972566948，并已标记 handoff。

### 第十八轮：扩展取消不再等待总超时

- 对取消后物理标签残留继续审计，发现源码虽关闭标签，但挂起浏览器调用可能使 busy 保持到总超时。增加每任务取消等待，取消时结束 Promise.race 并释放槽；迟到 progress 丢弃，采集关键异步边界复核取消状态。新增回归：挂起调用取消后 500ms 内结束、下一任务成功、旧结果不二次发布。
- 修复断线前迟到执行影响重连任务：执行槽绑定本次 job 对象，旧结果不发送到新 nativePort，旧 finally 不释放新任务。新增并行未完成新任务回归验证 busy 保持正确。
- 扩展全套 46 项通过，日志 /tmp/mb-document-cancel-release-extension-final-tests.log；manifest 升为 0.2.4，测试读取真实 manifest，之后 worker 5 项再次通过，/tmp/mb-document-cancel-release-manifest-tests.log。这些是源码测试，不代表已安装扩展更新。上轮运行心跳仍 0.2.3；物理标签即时关闭和成功刷新仍需实测。
- 原真实导入继续 66535/PID 58024，日志 /tmp/mb-document-owner-key-real-import.log，最新检查 13 分 37 秒仍 CPU 活跃，不因观察超时重新启动。

### 第十九轮：被排除运行日志的引用语义

- 原真实导入 66535/PID 58024 仍存活，18 分 38 秒时 CPU 99.9%；未按观察超时重新启动。继续只读扫描原副本引用发现 operation_replay_queue.last_run_id 有 180 条非空值指向 bake_runs，而完整资产合并明确排除 bake_runs。此前主键修复并不能单独解决这条独立外键问题。
- 通用 MergeForeignKey 读取 PRAGMA 的 on_delete。对明确排除父表且声明 SET NULL 的引用置空，避免悬空或误连目标同号日志；不对普通业务外键套用此规则。全源库扫描确认目前只有这一个被排除父表引用种类。
- 新回归同时覆盖目标无日志和目标存在同 ID 不相关日志：导入成功、旧引用为空、本地日志数量不变、原库引用 123 保留、外键检查通过。snapshot 专项 10 项通过，日志 /tmp/mb-document-excluded-runtime-reference-tests.log；其中未配置外部库用例未实际运行大库导入。当前在跑的原 66535 未加载这次修改，待其终态后再用新代码执行真实副本验收，不能以该旧任务结果证明本次修复完成。

### 第二十轮：真实导入重验与本地健康统计

- 核验 66535/PID 58024 仍为旧编译的真实导入进程。鉴于真实副本已证明存在 180 条到排除运行日志的引用、旧执行不包含已通过专项的新修复，按该确定性缺陷主动 SIGINT 结束（退出 101），不是观察超时或误判进程结束。原日志保留 /tmp/mb-document-owner-key-real-import.log。新代码真实导入启动为句柄 23036/PID 83818，日志 /tmp/mb-document-excluded-runtime-real-import.log；最近 5 分 19 秒 CPU 98.5%，仍运行。后续只读取新句柄，原 66535 已结束。
- 新增只读 GET /api/bake/documents/source-health，document-source-health.v1，默认一天、最多 31 天。检查数量按 complete/partial/failed 独立汇总，预算/超时单独计数，耗时只使用合法数值样本，缺失样本不按零参与平均。观察按窗口内创建的 cohort 返回当前状态、重试观察数和最老 pending 年龄。窗口外积压、每任务首次等待/退避/重复数不是该字段语义，不冒充全部指标完成。
- 聚合不返回正文、URL、指纹或任意错误文本。新增测试覆盖窗口外检查排除、无耗时 null、字符串耗时拒绝、未知错误不泄露、覆盖分类及 pending 年龄。API 时间范围专项 1 项、存储聚合专项 1 项通过；相关文档模块回归 82 passed，58.57 秒，/tmp/mb-document-health-related-regression.log。
- 健康接口与上一轮检查耗时尚未部署到 r16 Core，不宣称运行接口已验收。说明位于 document-source-refresh-runtime-config.md。OBS-002 每次排队/重试时长及重复计数、OBS-003 版本错配统计、灰度与规则写入回滚仍需完成。

### 第二十一轮：按租约统计队列尝试与重复观察

- 迁移 120 新增每租约一行的运维尝试记录和观察 duplicate_count。领取/结束/取消记录与观察状态在同一事务内更新；多个观察 coalesce 只计一次尝试，迟到 finish 不改取消/中断终态。重复 enqueue 保持原 false 返回及状态，只增加累计计数。
- 队列等待从最早合格观察的可领取时刻计算；退避记录本次安排值，执行墙钟间隔独立于来源单调时钟耗时。中断结束点未知、暂停尚未派发，两者执行耗时 null；不以租约到期时长冒充实际运行。按租约的统计不含没有 worker 租约的手动刷新，手动仍在来源 checks 内。
- 健康 API 增加 attempts 聚合和 observations.duplicate_enqueue_count。重复计数从迁移之后起算，是窗口内创建的观察集合累计值，不是窗口内所有浏览访问事件；进入队列前已去重的访问不在此计数。详细语义已写入 shared/document-quality/README.md 和运行配置说明。新运维表从资产导入排除，业务正文及来源表仍保留。
- 新增合并两观察、重复 enqueue、安排退避、取消后迟到完成、过期中断和暂停不伪造耗时回归。文档相关 84 passed、0 failed，61 秒，/tmp/mb-document-attempt-metrics-tests.log。由于新增迁移，完整 Rust 回归已启动，日志 /tmp/mb-document-attempt-metrics-full-tests.log，句柄以工具实际返回为准。
- 真实副本导入仍读取原 23036/PID 83818，/tmp/mb-document-excluded-runtime-real-import.log，最近 10 分 59 秒仍活跃，不因观察超时重启。该任务启动时尚无迁移 120，后续结合其结果及新迁移/快照专项评估，不冒充新统计运行验收。新统计和检查耗时仍未部署到 r16 Core；扩展 0.2.4 实际加载及物理取消也仍待验收。

### 第二十二轮：扩展加载交接与运行构建

- 核验历史浏览器拒绝证据：Browser Use URL policy 明确阻止 chrome://extensions 并禁止换控制通道绕过。已向用户异步请求手工重新加载“MemoryBread 浏览器集成”，确认 0.2.4；目前未收到完成答复，不能替用户认定已加载。其他工作继续。
- 空间降到 1.2 GiB 后，定位旧已中止 66535 留下的两个临时目录。通过 external-assets.mbsnapshot.json 的 source_db_path 确认 .tmpRXau9a 对应 .tmpLV0RxM/memory-bread.db，时间分别为 15:47 和 15:37；lsof 确认无进程打开。仅清理该 168479846 字节导出和 905109504 字节数据库副本及 shm/wal，共 1073622118 字节，原始 /tmp/mb-document-source-acceptance/source.db 和日志保留。当前 23036 用的是 .tmp7v5HUI/.tmpe7ung6，未清理。释放后 2.2 GiB。
- 新发布 Core 构建已启动：句柄 78461，/tmp/mb-document-r18-release-build.log，cargo PID 6829。完整 Rust 仍为原句柄 17197，/tmp/mb-document-attempt-metrics-full-tests.log；已核验日志继续增长。真实副本导入原句柄 23036/PID 83818 仍活跃，继续等待原任务。没有用观察超时推断结束或重跑。
- 当前尚未切换运行 Core，下一步先读取上述构建/回归/真实导入终态，核验用户扩展答复；不可把源码 0.2.4 或未部署 API 当成运行完成证据。

第二十二轮回归收尾：完整 Rust 630 passed、1 ignored、0 failed，384.34 秒，/tmp/mb-document-attempt-metrics-full-tests.log；原句柄 17197 已读取退出 0。发布构建 78461 和真实导入 23036 尚未终态，不能计为通过。

### 第二十三轮：运行耗时验证与恢复投影契约

- r18 release 构建完成，2m27s，原句柄 78461 已退出 0。通过项目 start 路径加载新 Core（PID 14801），UI 复用窗口 20782。启动日志 /tmp/mb-document-r18-runtime-start.log，schema migration 120 已在实际库存在。初始健康接口返回 10 次检查（complete 2/partial 1/failed 7）、timed_samples=0，旧观察不伪造新的租约记录。
- 实际兼容采集 1 秒预算验证：检查 11，SCRAPE_TIMEOUT，单调检查耗时 1512ms，API 往返约 2.590s；健康接口该窗口只统计一次失败和一个耗时样本。953 正文哈希 460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8、head 61 不变；原偏好不存在状态已恢复并复查。证据 /tmp/mb-document-r18-timing-health-live.json。扩展实际心跳仍 0.2.3，继续等待用户重新加载答复，不绕过受限扩展管理页。
- 真实导入 23036/PID 83818 已终态：外键合并成功后验收断言失败，1578.69s，left 1/right 2，/tmp/mb-document-excluded-runtime-real-import.log。只读源库核验：head 共 2，文档 16/head 70 的 snapshot_text != full_content；953/head 61 有效。原验收无条件要求 head 数相等不符合“拒绝失配当前指针”契约，现按源库有效身份/完整性/正文一致性计算应恢复数，并继续逐条核验目标 owner/body、checks 和 archives 数量。
- 同时发现新导入的失配文档可能保留 fresh_complete 声明；现仅对本次新插入且缺少有效 head 的 fresh_complete 文档降级 historical_only/unverified，历史快照保留，本地已有正文/状态不变。新增新导入及已有本地有效 head 两分支回归通过。
- 采样 /tmp/mb-document-real-import-progress-r18.txt 指向重复查找/SQLite step。优化：非主键身份字段子集查找已失败时，全行完全相同已不可能，跳过冗余全行扫描；继续检查其他合法语义/唯一键。所属主键只按所属 ID 查找，避免不同所属记录因状态完全相同被合并。新增两所属记录同状态及重复导入回归通过。
- 快照专项先 11 项通过，加入新鲜度投影后 12 项通过，/tmp/mb-document-head-projection-snapshot-tests.log；不冒充完整新回归。新真实副本导入已启动句柄 57500，/tmp/mb-document-head-projection-real-import.log；下一步读取该新任务。旧 23036 已读取退出 101，不再等待。当前运行 Core 尚未包含本轮最后的导入优化/投影修改。


### 第二十四轮：真实导入通过与当前来源证据校验

- 原真实导入任务 57500 已终态通过：1 passed，565.85 秒，日志 `/tmp/mb-document-head-projection-real-import.log`。测试确实设置了外部数据库环境变量，完成真实备份副本导出、导入；验证各资产数量、检查/归档保留、仅有效来源 head 被恢复、快照所属一致以及原始 capture 内容不被导出。此项不等于最终 UI 或全部回滚开关验收。
- Python 新增共享 `embedding/document_source.py` 投影：当前来源必须同时满足所属文档一致、身份通过、完整、快照文本等于当前正文。后台向量调度、向量写入、RAG 和创作引用统一使用；存在失配 head 的向量不能调度或发布，历史快照不会冒充当前正文证据。
- Python 七文件回归 528 passed、2 条既有 Qdrant 版本探测警告，日志 `/tmp/mb-document-source-projection-python-final-tests.log`；八个调整文件的 Python 3.9 AST 兼容检查已通过。
- Rust 新增当前有效来源查询；刷新节流复用仅返回 fresh_complete 且当前正文匹配的来源，保留 latest 查询原有历史语义。新增回归验证更新的 partial 只作为历史，以及正文编辑、删除、partial、身份失败和跨文档 head 均不能复用。专项测试正在 `/tmp/mb-document-current-evidence-rust-tests.log`，尚未计通过。
- 最新上述 Python/Rust 改动尚未运行部署。仍需完成实际扩展 0.2.4 取消/成功采集、最终 UI/消费验收、OBS 全入口审计和版本错配计数、灰度及停止新规则写入边界；完整目标保持实施中。

第二十四轮只读运行数据核验：新 Python 投影在当前本地库同一读事务中检查到 2 条有效文档的原始 head，其中 1 条正文匹配。953 返回 raw=61、verified=61；文档 16 为失配，verified=NULL。未修改任何生产记录。Rust 新测试首次因夹具缺少来源 URL 被既有身份门禁正确拒绝，补全夹具后当前来源专项已通过，完整 document_ 回归仍执行中。

第二十四轮回归收尾：document_ 相关 Rust 85 passed、0 failed，58.48 秒，日志 /tmp/mb-document-current-evidence-regression.log，句柄 20170 已退出 0。包含当前来源有效性新回归。全量 Rust 已启动为句柄 35745，日志 /tmp/mb-document-current-evidence-full-rust.log；尚未计通过，后续继续读取同一任务。


### 第二十五轮：向量来源身份核验与运行部署

- 上轮全量 Rust 已结束，633 passed、1 ignored、0 failed，209.23 秒；日志 `/tmp/mb-document-current-evidence-full-rust.log`，句柄 35745 已退出 0。
- 审计发现 RAG 已校验当前 head/正文，但向量仅以时间大于等于正文更新时间判新，未强制检查向量自身来源 ID。现要求来源 ID 为整数且与当前有效 head 完全一致，时间戳也必须等于当前正文版本；来源已撤销的带版本向量不能退化成无版本证据。未改变普通无来源 head 文档的旧兼容路径。
- 新增缺失/错误/字符串/布尔来源 ID、未来时间戳及来源 head 撤销回归；RAG/向量/共享来源 149 passed、2 条既有 Qdrant 探测警告，日志 `/tmp/mb-document-vector-identity-regression.log`。Python 3.9 和 diff 检查通过。
- 已启动 r19，日志 `/tmp/mb-document-r19-runtime-start.log`；需继续核验新进程加载以及实际咨询/创作引用，不能将启动命令视为运行验收。
- 当前真实扩展状态接口仍返回 connected=true、extension_version=0.2.3、active=0、queued=0。尚未收到用户完成 0.2.4 重载的答复，物理取消验收仍待完成。


第二十五轮运行及界面补充：
- r19 已启动完成：Core 80816、sidecar 78546、Model API 78637、Creation 78931；日志确认按源码变化加载。健康接口正常，证据 `/tmp/mb-document-r19-source-health.json`。
- 相同查询“快手灵机 非L0商家 账号矩阵 分销模式”真实 7071 /references 返回 953/head61，包含当前完整正文；8001 /creation/references 同 Top10 返回 953 第1、head61、source_body_hash 与当前 1139 字正文一致。断言报告 `/tmp/mb-document-r19-reference-evidence.json`，完整响应权限0600保存于 `/tmp/mb-document-r19-reference-responses.json`。
- UI 实测发现重复相同条件搜索不触发读取，旧详情仍显示检查10的取消信息。增加显式搜索版本触发原有请求序号保护的加载流程；不改变后台刷新或筛选逻辑。新增回归模拟后台更新后相同条件搜索，测试首次误用扩展超时文案，改为 SCRAPE_TIMEOUT 的既有“页面读取已达到时间预算”文案。
- HMR 后实际 localhost:1420 页面搜索并打开953，显示上次检查2026/9/12 16:21:29、历史版本、上次未完成原因“页面读取已达到时间预算”，正文保留四节、TVC句及末尾灵机内测反馈收集表；与当前数据库检查11一致。该UI验收是失败状态及正文保护，不能称新的完整采集已通过。

第二十五轮前端回归终态：BakeSearchFlow + BakeDetailDisplay 共45 passed，/tmp/mb-document-repeat-search-tests.log；tsc --noEmit 退出0，/tmp/mb-document-repeat-search-types.log。当前没有未结束的本轮测试或运行启动任务。原方案OBS全入口、规则灰度/写入回滚及扩展物理取消仍未完成，目标保持实施中。


### 第二十六轮：来源错配事件统计

- migration121 新增白名单来源错配事件表，健康接口增加按时间窗口分组件/原因聚合的 version_mismatches，资产快照排除运行诊断表。
- Python 共享审计装饰器在消费调用退出、原 SQLite 事务释放后落盘；短暂写失败不改变拒绝结果，不记录正文/URL/任意异常内容。RAG、向量写入及创作关键词/语义读取已接入，创作使用一致读事务并在模型计算前释放。
- 新测试覆盖事务正常返回、异常回滚、数据库锁、旧表兼容、无效值不进日志、只统计实际创作候选，并在实际RAG/向量拒绝测试中验证落盘次数。Rust 来源相关13项已通过，/tmp/mb-document-source-audit-rust.log。
- 最终消费测试正在 /tmp/mb-document-source-audit-consumers.log；全量 Rust 正在 /tmp/mb-document-source-audit-full-rust.log。最新迁移及审计尚未部署，不将当前r19的健康响应冒充新统计验收。
- OBS003已从单纯拒绝推进到部分生产入口持久计数；背景调度、摘要及其他版本门禁覆盖仍需核对。OBS001/002全入口、规则灰度和停止新规则写入、扩展0.2.4物理取消仍保留为未完成项。

第二十六轮补充：背景向量调度已增加有界待处理候选拒绝统计，与有效候选加载分开，不消耗正常调度名额。RAG和向量真实拒绝路径的隔离库测试验证累计事件，创作测试验证只计实际候选。此前消费247项通过；包括background的最终回归句柄67323，日志/tmp/mb-document-source-audit-consumers-final.log。全量Rust句柄76135继续原任务；当前新代码仍未部署。Python3.9八文件再次通过。

第二十六轮回归终态：全量Rust634 passed/1 ignored，178.55秒，/tmp/mb-document-source-audit-full-rust.log，句柄76135退出0；Python消费及后台320 passed/2条既有Qdrant警告，/tmp/mb-document-source-audit-consumers-final.log，句柄67323退出0。r20已启动加载迁移121及审计，日志/tmp/mb-document-r20-runtime-start.log，尚待实际进程/迁移/健康统计核验。


### 第二十七轮：错配统计真实运行验收

- r20启动成功：Core13749、sidecar12280、Model API12300、Creation12592、原UI窗口20782复用。发布构建37.21秒，日志 `/tmp/mb-document-r20-runtime-start.log`。迁移121统计表实际存在，健康接口已含version_mismatches。
- 使用当前库已有的文档16失配head70，在真实8001 /creation/references走原文档标题查询，产生两次creation/head_invalid事件；健康接口同一时间窗口合计occurrences=2。分别对应候选评估调用，不宣称两份失配文档。该文档未进入最终引用；16及953的正文hash/head前后完全一致。
- 报告 `/tmp/mb-document-r20-mismatch-live.json`，由API返回和数据库事件实际比对生成，权限0600。没有直接插入测试统计或改造生产文档制造失配。
- 正常953引用正在同一r20运行时复核，日志/报告 `/tmp/mb-document-r20-reference-evidence.json`；需读取真实终态后再计通过。
- 后续继续原方案剩余：OBS001每次评估审计、OBS002前置去重和实际worker尝试统计范围、OBS003摘要/剩余版本门禁覆盖、ROLL002/005规则选择/灰度及停止新规则写入、扩展0.2.4物理取消及成功采集。已验证路径不因这些缺口回退，也不得用局部验收替代完整交付。

第二十七轮正常引用复核完成：r20的953咨询包含完整1139字正文并标head61，创作Top10第1、head61、正文hash匹配。/tmp/mb-document-r20-reference-evidence.json，句柄94521退出0。运行统计新增未破坏正常来源引用；本轮没有运行中的测试或部署任务。


### 第二十八轮：来源刷新提交点暂停

- 新增 source_writes_enabled 配置（默认true），入口/worker停止派发；存储快照、正文apply、成功状态写入在事务内重新读取配置，未知或非法配置拒绝写入。新增明确StorageError，API转为暂停结果，不确认新来源观察。
- 在采集后暂停使用SOURCE_WRITES_PAUSED，保持pending并退还重试次数，保留实际耗时；健康统计分paused_before_dispatch/paused_after_dispatch。入口暂停仍使用原SOURCE_REFRESH_PAUSED。界面新增明确原因文案。
- 首轮文档Rust86passed/1failed，失败是测试直接插入偏好缺少updated_at，已改用产品upsert_preference；原句柄82459已结束。最终文档回归句柄6516，/tmp/mb-document-write-pause-final-rust.log，需要确认编译是否包含最后夹具更改，不能据旧结果交付。
- UI专项21项已通过（首轮测试因列表及详情同时出现正文而定位不唯一，改为限定详情）；/tmp/mb-document-write-pause-ui.log。类型检查句柄65130执行中。
- 新代码尚未部署；当前r20没有该写入开关。此轮补来源刷新实际提交点，完整灰度/规则选择及普通提炼写入回滚、OBS全入口仍未完成，不改变原方案范围。

第二十八轮测试跟进：6516终态87passed/1failed，仍为编译时已读取的旧夹具缺updated_at错误，并非修正后代码结果。已结束原任务，启动/tmp/mb-document-write-pause-corrected-rust.log验证当前夹具；类型检查65130已退出0。当前写入开关未部署，不计交付完成。


### 第二十九轮：提交暂停回归收尾与运行验收准备

- 当前夹具修正后的文档Rust88 passed、0 failed，71.80秒，/tmp/mb-document-write-pause-corrected-rust.log；句柄62998已退出0。此前两轮旧夹具失败保留，不能混用。
- 全量Rust句柄78389正在/tmp/mb-document-write-pause-full-rust.log执行；r21启动位于/tmp/mb-document-r21-runtime-start.log。启动曾等待supervisor的正常启停锁，后由r21进程43050取得，不是锁故障，未删除锁或重复重启。release rustc44382当前仍在运行。
- 已准备/tmp/mb-document-r21-pause-entry-check.py，在新健康字段出现后验证source_writes_enabled关闭时手动刷新被拦截、953正文/元数据/head/快照数/归档数不变，使用完整偏好行CAS恢复用户原配置；尚未执行，不计实际验收通过。
- 更新验收表中过期的导入与错配统计状态，历史轮次记录保留并明确标为历史，避免把已完成的导入误报仍运行。


第二十九轮运行入口验证完成：r21 Core48958已加载新代码，健康字段paused_after_dispatch存在。运行/tmp/mb-document-r21-pause-entry-check.py返回skipped/SOURCE_REFRESH_PAUSED，953正文hash、刷新元数据、head、快照及归档数量均不变；原偏好完整行（含原先不存在的情况）精确恢复。报告/tmp/mb-document-r21-pause-entry-live.json。原句柄27220退出0；全量Rust78389也已退出0，最终计数见日志。本次实际证明入口开关与配置恢复；采集中途关闭后的实际浏览器提交拒绝仍需另验，不能用入口测试冒充。

第二十九轮全量结果：Rust636 passed、1 ignored、0 failed，355.18秒，/tmp/mb-document-write-pause-full-rust.log。本轮测试、启动和入口验证均已终态，无需继续等待旧句柄。


### 第三十轮：运行中暂停实测与来源灰度范围

- 已完成真正采集中途暂停：实际扩展0.2.3任务268c6f0f-8815-4d4b-95c2-5444024dcc21先进入running/reading，再关闭source_writes_enabled；API最终skipped/SOURCE_WRITES_PAUSED。953正文hash/head/快照数/归档数不变，原偏好行精确恢复（当前该键不存在）。报告/tmp/mb-document-r21-pause-running-live.json，脚本/tmp/mb-document-r21-pause-running-check.py，句柄78956退出0。此项不是入口拦截或取消，也不代表0.2.4取消验收。
- 当前953保留1139字符/head61，状态historical_only，last_error=SOURCE_WRITES_PAUSED；不称最新刷新成功。提交暂停目前仅记文档失败元数据，缺少对应source_check诊断事件，OBS001仍需补齐该分支。
- 新增quality_rule_version和rollout_document_ids配置。版本只允许已安装v2，未知拒绝；范围缺失全部/空列表暂停/正整数列表只启用指定文档。worker领取SQL按范围筛选，入口和事务提交再次核对。
- 回归覆盖范围外较早观察不阻塞范围内任务、未领取观察不消耗次数、空范围不领取、移出范围拒绝已采集快照提交及移回后允许。测试句柄5681，日志/tmp/mb-document-rollout-rust.log，尚待终态。
- 最新灰度改动尚未部署，当前运行r21只证明来源写入开关。首次自动提炼/普通观察写入的统一灰度与回滚、剩余OBS全入口、扩展0.2.4取消仍未完成。

第三十轮回归收尾：文档相关89 passed、0 failed，/tmp/mb-document-rollout-rust.log，句柄5681退出0；新全量Rust正在/tmp/mb-document-rollout-full-rust.log。浏览器当前active_job_count=0，中途暂停任务已结束，原偏好已恢复。新灰度配置仍未部署。


### 第三十一轮：失败状态和检查审计原子化

- 上轮实测确认SOURCE_WRITES_PAUSED更新了文档失败状态，但没有对应source_check。现提交暂停与超时/页面消失/空正文/身份不匹配统一走失败检查入口。
- 新存储方法把失败状态和source_check置于同一事务；审计插入失败则文档状态回滚，不再吞掉数据库失败后报告记录成功。检查记录含规则版本、原因、开始/结束/单调耗时，不写正文。
- 规则版本使用与灰度配置相同的受支持常量。提交暂停从外层开始计时，包含采集时间；source_snapshot_id为空，避免把未提交来源标成当前。
- 新回归用触发器拒绝检查插入，验证原状态/旧检查/正文一致保留；API测试验证暂停原因和1800ms耗时。文档回归38055在/tmp/mb-document-failure-audit-rust.log运行，灰度改动之前启动的全量37752继续/tmp/mb-document-rollout-full-rust.log。
- 最新原子失败审计及灰度均未部署。实际运行仍是r21，暂停诊断事件不可用新测试推定已有。


## 第三十二轮：失败审计部署与灰度运行验证

- 灰度版本的全量 Rust 已终态：637 passed、1 ignored、0 failed，405.50 秒，`/tmp/mb-document-rollout-full-rust.log`。之后增加的失败状态/检查原子事务专项为 90 passed、0 failed，89.38 秒，`/tmp/mb-document-failure-audit-rust.log`。
- r22 release 构建 1m22s 成功，Core PID 77296 已加载灰度与原子失败检查；启动日志 `/tmp/mb-document-r22-runtime-start.log`。
- 真实 API 验证灰度范围排除 953 时刷新返回 SOURCE_REFRESH_PAUSED，空范围同样暂停；未知规则版本返回 400，偏好未被无效配置修改。953 正文、状态、head、快照和正文归档数量不变，测试后精确恢复原偏好。报告 `/tmp/mb-document-r22-outside-scope-live.json`，脚本退出 0。
- 最新全部代码的 Rust 全量回归正在 `/tmp/mb-document-failure-audit-full-rust.log`（句柄 73976）；真实采集中途写入暂停审计正在验证（句柄 89867），均尚未计为通过。
- 全入口评估审计、普通自动提炼写入灰度/回滚、扩展 0.2.4 实际取消及最终 UI 等原范围仍保留，未宣称整体交付。

第三十二轮测试终态：最新全量 Rust 638 passed、1 ignored、0 failed，174.25 秒，/tmp/mb-document-failure-audit-full-rust.log，73976退出0。首轮89867实际在running后发生NAVIGATION_TIMEOUT，故不计提交暂停通过；检查13准确记录规则v2和34110ms，正文/head/版本数量不变、偏好恢复、active_job_count=0。报告/tmp/mb-document-r22-scope-pause-live.json。全量测试结束后对该导航瞬态错误进行一次有界重试，句柄10110，独立保留/tmp/mb-document-r22-scope-pause-retry-live.json，不覆盖首轮证据。

第三十二轮实际重试已通过：10110退出0，浏览器任务0c8bd052-e72d-4585-a1e1-4a576d834939先running后暂停写入，返回SOURCE_WRITES_PAUSED；对应最新检查snapshot=NULL、quality_version=document-quality.v2、execution_ms=33413。953正文/head及快照/归档数量不变，原偏好恢复。报告/tmp/mb-document-r22-scope-pause-retry-live.json。没有把首轮导航超时改记为通过。当前全量638项、启动r22和两轮实际检查均已结束；扩展仍0.2.3，物理取消等原验收范围尚未完成。


## 第三十三轮：普通候选质量审计

- 契约新增 document-candidate-quality.v1，迁移122增加独立候选评估事件。按 precheck/extraction/persistence 三个语义阶段追加，run/timeline/document/capture ID 可关联已有跳过和提炼审计；入库阶段透传真实 audit_run_id。
- 事件只接受类型化ID、计数和布尔证据，原因使用固定码；不保存正文、URL或模型解释。被动采集缺失的正文块、排除块、正文字符和脱敏比例明确null，输入字符另计，coverage固定unverified，不能将候选可提炼误报为完整来源。capture_id_scope为candidate_members，不能声称覆盖未知聚合来源。
- 健康接口新增candidate_evaluations按阶段/原因计数；该运行表排除资产导入，避免跨库ID重绑定。
- 新回归覆盖阶段独立记录、未知证据不升级、实际同URL回访入队时记录原文档ID、空壳被拒仍审计且正文/URL/模型解释不入事件。首轮91项通过，72.37秒，/tmp/mb-document-candidate-audit-rust.log。后续新增健康统计与拒绝测试的全量正在93080，/tmp/mb-document-candidate-audit-full-rust.log；最后的run ID透传改动另以/tmp/mb-document-candidate-audit-final-rust.log验证。
- 当前新增审计未部署；不能将三阶段接入当作OBS001全部入口已验收，仍需检查手动/确定性路径覆盖和实际运行事件。普通提炼灰度/回滚、剩余OBS版本门禁、扩展0.2.4取消和最终UI仍在原范围中。

第三十三轮专项终态：最终任务ID透传及新增行为92 passed、0 failed，58.84秒，/tmp/mb-document-candidate-audit-final-rust.log，72870退出0。全量93080尚运行；r23正在/tmp/mb-document-r23-runtime-start.log加载迁移122和新审计代码，尚不计实际运行验收通过。

第三十三轮全量终态：新增审计、健康汇总及拒绝行为全量640 passed、1 ignored、0 failed，303.06秒，/tmp/mb-document-candidate-audit-full-rust.log，93080退出0。之后仅任务ID透传的最终源码专项92项通过。r23仍在发布构建，当前暂无运行中的回归测试。

第三十三轮运行收尾：r23 Core99066实际加载迁移122及新三阶段审计。/tmp/mb-document-r23-candidate-audit-live.json校验健康汇总与数据库同窗口结果一致、953正文hash/head61不变；当前live_event_count=0，所以仅确认迁移与接口加载，非零实际候选审计尚未验收，不能使用空统计替代。所有本轮测试/构建已终态；继续原范围工作。


## 第三十四轮：普通自动文档写入暂停与灰度

- 实际审计仍无候选事件，未把空结果升级为非零实际样本通过。
- 共享配置新增automatic_document_writes_enabled及automatic_document_rollout_percent；首次自动文档通过独立insert_bake_document_from_observation事务检查，普通观察合并在现有CAS事务检查，手工编辑/沉淀入口继续独立。灰度按规范化来源身份SHA-256稳定分桶，旧文档合并使用旧身份，防止改名绕过范围。
- 暂停使用DocumentAutomaticWritesPaused/固定DOCUMENT_AUTOMATIC_WRITES_PAUSED；现有bake untracked transient分支保留水位并defer，不将其转换为模型false-negative或消耗失败次数；插入竞态兜底不得吞掉该错误。API将其返回409而非数据库500。
- 当前普通批次会在暂停候选处延后，尚未完成范围外候选不阻塞范围内候选的调度改进；不能宣称完整灰度调度验收。来源快照仍由独立source_writes_enabled控制，停止所有自动正文发布需同时关闭两个开关。
- 回归包含关闭后拒绝首次写入、延迟观察不能改旧正文、手工编辑仍可用、恢复后继续写入、分桶稳定/单调、bundle暂停仍deferred且不触发false-negative，以及API固定错误码。当前专项/tmp/mb-document-automatic-write-rust.log（28808）、补充专项/tmp/mb-document-automatic-write-final-rust.log（26089）、最新全量/tmp/mb-document-automatic-write-full-rust.log（48205）运行中；未部署。

第三十四轮专项终态：文档94 passed、0 failed、78.61秒（/tmp/mb-document-automatic-write-rust.log，28808退出0）；补充自动写入3 passed、0 failed、5.00秒（/tmp/mb-document-automatic-write-final-rust.log，26089退出0），覆盖bundle暂停deferred而非false-negative。最新全量48205仍在/tmp/mb-document-automatic-write-full-rust.log执行，含最后的API响应回归；新代码尚未部署。


## 第三十五轮：普通灰度队列缺口核对与运行加载

- 上轮全量48205已确认仍有真实cargo/test进程，继续同一任务，未因等待重启。r24在/tmp/mb-document-r24-runtime-start.log加载上轮已通过专项的提交控制。
- 确认普通灰度若直接跳过并推进水位会漏候选，当前deferred整批策略则可能阻塞后续范围内项。后续应扩展现有bake_retry_state表达独立暂停，不把暂停计为失败：保存固定暂停原因和稳定分桶，保持原failure_count；fresh查询排除已暂停项；retry查询在LIMIT之前按当前开关和比例过滤暂停桶，允许failure_count=0及已存在兄弟产物的文档续作。
- 持久化暂停必须先于推进水位；恢复候选要显式绕过旧水位/纯metadata短路，不能仅靠retry_failure_count>0；实际文档提交继续事务重查范围，完成后再清暂停记录。更换来源身份导致分桶变化时更新暂停桶。上述调度改动尚未实现，保持明确未完成。
- 实际配置检查脚本/tmp/mb-document-r24-automatic-config-check.py已准备：无效比例400且偏好不变，有效开关/0比例保存，953全状态不变，精确恢复原配置。须等r24实际加载后运行；它只证明配置链路，不替代真实普通写入暂停验收。

第三十五轮运行完成：全量644 passed/1 ignored、414.39秒，48205退出0；r24 Core19699已加载，构建3m07s。/tmp/mb-document-r24-automatic-config-live.json验证错误比例不写配置、合法关闭/0比例接受、953不变且原偏好恢复。运行中自然出现16条候选审计（预检6、提炼5、入库5），全部事件的run/timeline有效、仅白名单字段、无证据项仍null/unverified，同窗口健康汇总严格匹配。报告/tmp/mb-document-r24-candidate-audit-live.json。已补齐非零真实审计样本，当前没有运行中的测试/启动任务；普通暂停实际写入验收、持久暂停队列调度及其他原缺口仍继续。


## 第三十六轮：持久暂停队列与恢复资格

- 迁移123在现有bake_retry_state增加automatic_document_pause_bucket；暂停保存固定原因/桶，保留原failure_count和历史失败时间，不创建新的失败。StorageError仅携带0–99桶号，不携带URL。
- 普通提炼和旧文档metadata入口在捕获暂停后，先持久化待办及deferred审计，再推进水位继续后续候选；无法写待办则不推进。暂停恢复显式绕过水位和纯metadata短路，零failure_count也不会被当成普通旧候选。
- fresh排除待办，retry在LIMIT之前按当前开关/比例筛选暂停桶，允许已有知识/SOP等兄弟产物时继续文档；成功/幂等跳过后清待办。operation replay对暂停范围同样筛选。
- 队列统计同步处理零失败待办：关闭/范围外不算actionable，恢复范围内算retry_ready；metadata避免重复计数。提交仍复核配置，重新暂停时更新桶。
- 新测试覆盖水位已推进、较早范围外候选不阻塞LIMIT=1的范围内候选、兄弟产物存在、关闭无可执行任务、恢复后全部可选、重复暂停不增加失败以及清待办。cargo check通过（/tmp/mb-document-deferral-check.log，82052退出0）。专项35314与最终查询/监控专项53358执行中；最新全量/tmp/mb-document-deferral-full-rust.log已启动。
- 本轮代码未部署；运行中的r24保持迁移122。不能将编译通过或单个查询通过当作完整批次运行/重启恢复验收；原OBS、扩展0.2.4取消、最终UI等范围继续保留。

第三十六轮专项终态：文档97 passed、0 failed、65.75秒（/tmp/mb-document-deferral-rust.log，35314退出0）；最终查询及队列统计专项1 passed、0 failed、0.30秒（/tmp/mb-document-deferral-final-rust.log，53358退出0），覆盖暂停恢复及LIMIT前范围筛选。最后将早期metadata暂停审计改为upsert，避免尚未建候选审计时无记录；该最终代码由全量50543继续验证（/tmp/mb-document-deferral-full-rust.log）。未部署。


## 第三十七轮：队列构建期水位顺序修正

- 审查发现第三十六轮旧metadata暂停发生于work_queue构建期，若立即推进水位，会越过之前已入队但未完成的Extract。修正为持久保存暂停后插入有序Skip（clear_retry=false），消费该项时才按原顺序推进水位。暂停待办不被清除。
- 新增真实pipeline回归：第一项连接不可用的测试sidecar，后项旧文档metadata因写入关闭暂停；必须返回SIDECAR_UNAVAILABLE、原水位保持1、后项暂停待办存在、前项不增加失败。只访问127.0.0.1:1，不调用用户的真实模型服务。
- 专项/tmp/mb-document-deferral-order-rust.log（4345）执行中；前轮全量50543继续原任务，它编译于本次时序修正前，不作为新修正的最终证明。运行仍r24，本轮未部署。

第三十七轮验证跟进：持久暂停队列全量645 passed、1 ignored、301.49秒（/tmp/mb-document-deferral-full-rust.log，50543退出0）。时序专项4345首轮没有触发预期的不可用分支，误走completed而失败；测试客户端隔离环境代理后68564通过1项、1.90秒（/tmp/mb-document-deferral-order-final-rust.log），确认原水位和暂停记录保持。最新时序修改的提炼模块回归92729正在/tmp/mb-document-deferral-order-service-rust.log；r25启动加载在/tmp/mb-document-r25-runtime-start.log，尚未运行验收。

第三十七轮收尾：最新时序修改的提炼模块86 passed、0 failed、66.23秒（/tmp/mb-document-deferral-order-service-rust.log，92729退出0）。r25 Core42290加载迁移123及有序暂停处理，release1m19s成功；队列接口返回有效状态，953正文hash/head61不变（/tmp/mb-document-r25-deferral-live.json）。当前actual_paused_candidates=0，仅确认部署/接口，实际非零暂停恢复仍未验收。所有本轮测试及启动已终态。


## 第三十八轮：文件重开与真实备份恢复验证

- 将暂停队列专项从内存库改为文件数据库，分别在设置灰度后、暂停后、清理完成项后关闭并重新打开StorageManager。专项1 passed、0 failed、0.42秒，/tmp/mb-document-deferral-reopen-rust.log，86814退出0。
- 新增显式ignored真实备份验收，必须提供MEMORYBREAD_DOCUMENT_DEFERRAL_ACCEPTANCE_DB且路径为隔离mb-document-deferral-real-*目录下working.db，缺失环境变量不会空跑通过。使用现有真实备份/tmp/mb-document-source-acceptance/source.db的APFS克隆/tmp/mb-document-deferral-real-r26/working.db，原备份inode82227420与副本82534276不同，原备份mtime保持16:29。
- 只调整隔离副本的调度状态，选择其实际时间线候选，验证超过水位后恢复、LIMIT前灰度过滤、关闭暂停、恢复全部、完成清理及多次重开；所有文档id/正文/更新时间的联合SHA256前后一致。显式--ignored执行1 passed、0 failed、2.98秒，/tmp/mb-document-deferral-real-backup-rust.log，5260退出0。未调用模型、未修改运行库或原备份。
- 这是实际备份数据上的存储恢复验证，不是运行客户端非零暂停任务的端到端证明。运行库仍无暂停候选。本轮仅改测试，无需重启运行服务。
- 下一项OBS003核对线索：rag/retriever.py的文档预筛与creation/service.py的_query_document_rows仍包含summary字段；须继续验证旧摘要是否可能影响当前来源的筛选/评分以及失配计数，尚未据此认定具体错误。


## 第三十九轮：摘要来源读取保护

- 确认创作_vector_recall将summary和正文拼接后编码，RAG最终_document_row_to_chunk也携带summary；原来只保护正文来源head，没有独立摘要版本证明。
- 新增共享source_summary_select：存在来源head时只允许summary_source_snapshot_id等于当前有效head的摘要；缺失/错误绑定或已撤销head时拒绝带版本摘要。无head且无绑定的旧文档保留原兼容行为，不修改存储摘要。
- 接入创作文档SQL筛选/评分、语义补充候选及编码输入、RAG SQL候选和最终materialization；摘要被拒时重新计算引用相关性，不继承旧摘要分数。
- 迁移124新增nullable摘要来源ID（不猜测回填），扩展summary_version_mismatch诊断原因并保留旧事件。新版Python先于迁移时只跳过旧CHECK不支持的新原因，不丢失其他诊断。已评估行的摘要版本拒绝按ID记录，不写正文/URL。
- 新回归证明过期摘要不进入当前引用/元数据、旧999分不继承、原存储摘要保留、有效绑定可读、撤销head不可回退为legacy；创作候选SQL及实际编码输入不含旧摘要，失配计数准确；迁移保留旧事件。
- 最终Python四组206 passed、2条既有Qdrant兼容警告、8.74秒，/tmp/mb-document-summary-consumers-final.log，2228退出0；8个修改Python文件AST 3.9通过。Rust文档98 passed、1 ignored、40.46秒，/tmp/mb-document-summary-migration-rust.log，2625退出0。快照范围19144正在/tmp/mb-document-summary-snapshot-rust.log回归。
- 当前未部署；摘要发布方的CAS绑定写入、带绑定摘要的导入/恢复以及其余派生元数据范围尚需补齐。不能将读取拒绝实现当作摘要全生命周期验收完成。运行仍r25/迁移123。


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


## 第五十八轮：恢复后的消费隔离联合验证

发现并修复向量构建的衍生字段绕过：build_bake_document_snapshot原先在full_content和sections_json之间选较长文本后才做空壳判断；旧长章节可掩盖失败正文。现在先检查原始full_content，已确认空壳立即拒绝，再沿用正常候选处理；共享规则与短文行为不变。

新增test_restored_shell_stays_isolated_across_document_consumers在独立SQLite数据库实际调用restore_document_body_version.restore(apply=True)，随后调用向量快照构建、RAG旧URL向量实体化及CreationService真实关键词读取和融合召回。验证原始正文/来源关联保留、恢复前备份正确、head清空、旧向量删除入队，并且三个消费入口均拒绝恢复出的空壳，即使其包含长摘要与长章节。共享质量反例也加入长章节干扰。该测试未使用模型或真实Qdrant，不据此声明最终输出采纳、物理向量删除或全部ROLL-005已验收。

相关test_document_update_replay.py、test_document_vectors.py、test_creation_references.py、test_rag.py合计221 passed，2个Qdrant兼容性告警，耗时8.93秒，证据/tmp/mb-document-r58-regression.log。本轮三个修改Python文件通过3.9 AST检查。加载日志/tmp/mb-document-r58-runtime-start.log，使用既有MEMORYBREAD_LOCAL_ONLY入口避免无关仓库启动。


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


## 第六十一轮：单条与分段提炼诊断原文清理

extract_sync主调用使用capture_content=False，成功、解析失败与异常日志移除模型正文/响应和异常原文。_generate_segments移除摘要预览、异常traceback和任意丢弃原因，只保留计数与固定状态；业务返回和缓存摘要保持不变。新增单条成功/JSON失败/异常与分段成功/异常/缓存复用回归。

数据事实恢复调用曾尝试同样设置capture_content=False，但已有安全错误码被抹为INFERENCE_FAILED，首次相关测试3 failed/97 passed，日志/tmp/mb-document-r61-recovery-tests.log；已撤销该入口的尝试，保持其原有脱敏错误类型及早停原因行为，没有改测试来迎合退化。最终7组受影响测试共222 passed，7.71秒，/tmp/mb-document-r61-final-tests.log。最终两个Python文件通过3.9 AST检查。

本轮部署使用标准本地启动入口，日志/tmp/mb-document-r61-runtime-start.log。仍不能把这些入口通过扩大为全NFR-002完成：数据恢复入口的任意元数据、相似合并异常及剩余模块需要继续核对。


## 第六十二轮：恢复调用的诊断元数据校验

数据事实恢复原先把上游usage和done_reason直接传给用量追踪器，并在finally日志打印token字段；恶意或格式错误的元数据可携带正文，即使不记录message.content也仍泄露。现只接受非布尔、非负、SQLite整数范围内的token数值，否则使用合法后备计数或本地估算；显式0不被估算替换。结束原因限定为已有stop/length/repetition/ungrounded/cancelled；传给tracker的响应只含经过校验的usage和done_reason，不含原始响应对象。原有异常类型、早停错误码和业务事实解析保持不变。

新增5类恶意/畸形元数据及显式0回归，断言事实仍正常产出、私有元数据既不进入用量事件也不进入caplog。最终7组受影响测试228 passed，8.49秒，/tmp/mb-document-r62-regression.log；两个修改Python文件通过3.9 AST。标准本地加载日志/tmp/mb-document-r62-runtime-start.log。本轮没有新请求真实模型或修改953。

剩余诊断入口仍按原NFR范围继续核对；本轮不宣告全部日志或全部质量方案完成。


## 第六十三轮：公共用量数值字段边界

LLMCallTracker.set_response/set_tokens及log_llm_usage最终写库入口校验非布尔、非负、SQLite范围内整数。非字典usage、异常message结构与任意token元数据不会直接进入计数；总token和截断到SQLite整数上限避免溢出。其他调用方既有正文预览开关和安全错误码行为不变。新增私有tracker畸形元数据回归及实际SQLite数值字段写入验证，证明文本不能借动态类型整数列落库。

提炼7组测试232 passed，7.65秒，/tmp/mb-document-r63-tests.log；创作召回和RAG196 passed，7.57秒，2个Qdrant兼容性告警，/tmp/mb-document-r63-consumer-tests.log。两个修改Python文件通过3.9 AST。

共享monitor源码加入三个Python服务的显式加载检测，test-startup-freshness.sh验证通过，bash -n通过。标准加载日志/tmp/mb-document-r63-runtime-start.log。未改动953或新发起模型验收请求；原第六十轮真实创作证据保持。仍须完成剩余诊断、扩展与回滚范围，不据此宣告全方案交付。


## 第六十四轮：直接摘要发布的错配审计与剩余项复核

publish_document_source_summary_guarded的无租约发布路径，在拒绝旧/无效来源时增加summary_write/summary_version_mismatch计数。使用来源ID、正文一致性、完整性、身份与预期updated_at检查；同来源同revision仅因已有摘要而拒绝不计错配。租约调用仍由任务终结入口记录，避免双计；暂停/灰度错误在计数前返回。审计写入异常只输出固定告警，不让旧摘要发布。扩展现有实际SQLite事务回归覆盖旧来源事件、审计失败保护和同revision已有摘要不误计；原有暂停与向量清理失败回归继续通过。

Rust document_相关113 passed、1 ignored，0 failed，51.48秒，/tmp/mb-document-r64-tests.log；ignored为需要外部数据的验收入口，不冒充执行通过。标准本地加载日志/tmp/mb-document-r64-runtime-start.log。扩展重新核对仍connected=false/version0.2.3、active/queued=0，/tmp/mb-document-r64-extension-status.json。

原方案剩余范围继续保留：扩展0.2.4重新获取/物理取消/最终UI（FR001/002/006/007，AC004/006/010，NFR003，ROLL003）；其余文档诊断入口及敏感URL（NFR002）；评估审计字段与全部调用入口、队列前去重统计及摘要调度错配统计（OBS001/002/003）；规则回退、指针恢复、外壳消费隔离和兼容读取的联合运行证明（ROLL005/NFR004）。第六十轮已完成953最终创作采纳，本轮补齐无租约摘要提交错配，不能继续把这两项列作缺失，但不代表同类全部入口完成。


## 第六十五轮：正式正文归档保留摘要绑定及导入映射

确认正式apply_document_source_snapshot此前直接序列化BakeDocumentRecord，而该DTO不含summary_source_snapshot_id/summary_generation_version。因此即使维护CLI已支持显式恢复，正式归档仍缺少摘要绑定证据。现于同一来源切换事务中读取并归档这两个内部字段，不改变公共DTO或增加迁移。

备份导入对新归档字段执行快照ID映射，并校验导入快照归属、历史正文、身份和完整性；失效/缺失映射清空绑定与生成版本，保留历史摘要原文。无绑定字段的旧归档不猜测绑定。扩展原实际SQLite文档发布测试验证第一版正确摘要随第二版切换归档；完整snapshot导入测试覆盖有效绑定、缺失来源和错配正文，分别与本地同/不同身份、占用/未占用快照ID组合，并重复导入验证幂等及本地正文不变。

文档Rust113 passed/1 ignored，54.33秒，/tmp/mb-document-r65-tests.log（扩展导入负例前，生产代码相同）；扩展负例后storage::snapshot::tests 12 passed，29.85秒，/tmp/mb-document-r65-import-tests.log，其中外部DB入口未设置环境变量，不作为真实全库重放证据；Python恢复14 passed，9.84秒，/tmp/mb-document-r65-restore-tests.log。无Python生产代码修改。标准Core加载日志/tmp/mb-document-r65-runtime-start.log。953保持原记录，不用生产来源更新来制造验收数据。

本轮补齐归档→导入→显式恢复所需的绑定契约，仍不替代新版扩展物理采集及全部回滚/审计剩余范围。


## 第六十六轮：摘要调度拒绝的可观察性

claim_document_summary_job在已启用且处于灰度范围内、摘要为空的文档中，记录无效来源的summary_schedule/head_invalid评估事件；正文不符、身份不符、partial或归属错误均拒绝生成租约。计数表示实际调度评估次数，不是去重文档数；暂停/范围外不计数。审计写入异常只留固定告警，不放行失配来源，也不阻塞有效文档获取任务。迁移129增补组件枚举并保留旧事件。

新增四类无效来源与暂停/灰度/审计失败保护组合测试，并核对document_source_health中的聚合结果。首轮113 passed/1 failed/1 ignored，失败为测试构造其他文档漏填created_at，修正夹具后114 passed/1 ignored/0 failed，73.58秒，/tmp/mb-document-r66-final-tests.log。旧失败日志/tmp/mb-document-r66-tests.log保留。不涉及Python修改。

迁移前只读保存8条现有事件到/tmp/mb-document-r66-audit-before.json；标准部署日志/tmp/mb-document-r66-runtime-start.log，待部署后逐条核对。新增调度审计补强OBS003，不代表OBS001/002或全部诊断/物理采集已验收。


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
