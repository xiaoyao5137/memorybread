# 扩展恢复后的真实验收

2026-09-13，用户更新“MemoryBread 浏览器集成”后，本机状态确认connected=true、version=0.2.4；浏览器集成页面也显示已连接。此前连接阻塞已解除。

953在扩展空闲时，真实任务3204abc6-b949-4ee1-91fa-6b1e2a377b2e于约0.5秒被领取，经过loading/reading，30.33秒返回updated/complete。前两次请求遇到5秒未领取，返回BROWSER_EXTENSION_UNRESPONSIVE；同一时段有其他任务执行。忙碌与未响应的判别仍需单独复现，不能把假设写成已定位根因。

真实结果：原ID953保留，正文1139字符，head61，摘要绑定61，正文SHA256为460e3b8427f432cc688a9d176a6ae986d7c5df01497b0d39072b044cac5adcf8。本次读取与此前已核对正文的差异只有排版空白，未强行追加重复内容。详情页面显示已验证完整快照、已完成来源检查、摘要已根据当前原文生成，正文与摘要分开展示。

真实取消：在独立后台MemoryBread页面点击“立即刷新”，任务04695983-2b39-4973-98d0-ed669e9dbc01进入reading后点击“取消本次刷新”。页面提示“已取消本次文档刷新，原正文保留”；Core任务状态cancelled，活动与排队数均0，正文hash和摘要绑定保持。未将任务状态本身当作Chrome标签已关闭的充分证据。

发现并修复审计缺陷：归一化指纹重用旧快照时，输入1177字符而保存的快照为1139字符，旧逻辑将输入的hash/偏移附到了snapshot61。现在成功路径根据实际保存的快照重新构建hash、正文字符数、块偏移，输入计数与执行时间独立保留；取消路径仍不产生快照。119项文档回归通过，Core12512已加载。真实二次读取的audit_hash_matches、audit_characters_match、offsets_in_range全部为true。

二次读取本身为partial，且旧完整快照正确保留；但刷新元数据此前从复用快照取complete，错误提升了本次读取状态。已改为从本次输入取覆盖、字符数、段数和截断状态，原快照质量及正文不降级。新增同指纹complete→partial的真实存储/service回归，验证两个状态分开。最终119项文档回归通过（75.38秒），Core28544已加载并通过健康检查。最终真实重取再次返回partial，DB状态fresh_partial、last_refresh_completeness=partial；正文hash、head61及摘要绑定61保持，审计hash/字符数/偏移三个检查全部通过。证据`/tmp/mb-document-953-final-refresh.json`与`/tmp/mb-document-953-final-check.json`。

证据文件：

- `/tmp/mb-document-953-claim-observation.json`：实际任务领取和执行时间序列。
- `/tmp/mb-document-953-claim-result.json`：第一次成功结果。
- `/tmp/mb-document-953-live-cancel.json`：取消状态及正文/摘要保护。
- `/tmp/mb-document-953-audit-verified.json`：真实审计绑定一致性。
- `/tmp/mb-document-live-reused-audit-tests.log`：119 passed、1 ignored、0 failed。
- `/tmp/mb-document-live-read-status-tests.log`：本次覆盖状态修复回归。

来源链接在后台UI实际点击后打开到953的精确来源URL；独立内置浏览器后续加载内网页面中断，所以不能把它称为已登录Chrome的页面跳转验收。Chrome中的真实读取由扩展完成。

最终重新加载页面并搜索953，详情显示“已验证部分快照”、1177字符/14段/已截断，正文区域明确提示“最近一次仅获取部分正文，未覆盖已有完整版本”；摘要仍显示根据当前原文生成，正文仍为保留的完整版本。与后台最新读取状态和版本绑定一致。

尚不宣告整体交付：准确的物理标签清理、已登录页面引用跳转及并发下领取等待的判别仍有验证边界。保留已通过证据，不要求重复真实模型生成或重做历史恢复。

并发等待补充验收：Core 59038 已加载领取等待修复，121 项文档回归通过（1 ignored，0 failed，139.35 秒），其中 broker 12 项测试覆盖忙碌排队、队列取消、总超时及空闲未领取快速失败。扩展忙碌时不累计空闲领取超时；总执行预算保持不变。

真实并发：953 任务 855df973-1892-497a-92e4-fa9dbb4e3b85 已被领取后提交 942，任务 3097d30b-343d-494e-8661-807cc8a270f5 从观测第 3.04 秒到 23.10 秒持续 queued，无未响应误报。取消 953 后，第 25.10 秒观察到 942 opening，33.12 秒进入 reading。随后取消本次 942 验收读取；两次 HTTP 均以 SOURCE_REFRESH_CANCELLED 收束，最终 active=0、queued=0。

物理标签清理已补齐：Chrome 原生 AX 记录基线 10 个用户标签；953 reading 期间新增标题“快手灵机：面向非 L0 商家的规模化 AIGC 招商方案 - 云文档”，取消后该标签消失，随后排队任务的临时页面出现；第二次取消后恢复同一组 10 个用户标签。所有浏览器操作均为只读观察，未手动关闭页面。953 正文 SHA256、1139 字符、head61、摘要绑定61均保持；取消后的刷新状态为 historical_only，保留之前 partial 覆盖记录，未伪装成一次成功读取。

证据：`/tmp/mb-document-claim-queue-tests.log`、`/tmp/mb-document-claim-regression.log`、`/tmp/mb-document-claim-runtime.log`、`/tmp/mb-document-queue-live-observations.json`、`/tmp/mb-document-queue-live-results.json`、`/tmp/mb-document-queue-live-final-check.json`，以及本轮 CUA 基线、reading、取消、最终 AX 观察。并发领取与取消物理清理边界已关闭；独立内置浏览器的来源链接目标已核对，但已登录 Chrome 中的实际引用跳转仍未单独验收。

最终来源跳转补齐：在用户已登录Chrome选择MemoryBread，搜索953、打开详情并点击实际来源链接，页面从知识库加载为953精确标题及原URL。首屏核对第一至第三章和表格文字，向下滚动核对第四章及末尾“灵机内测反馈收集表”；与SQLite保存的1139字符正文逐段一致。未操作编辑、分享或评论，结束后关闭本次点击创建的来源标签。此前已登录引用跳转的未验收边界已关闭。原方案32项最终核对见[交付记录](document-source-quality-delivery-2026-09-13.md)。
