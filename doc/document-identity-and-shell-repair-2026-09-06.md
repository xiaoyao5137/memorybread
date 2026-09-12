# 文档身份关联与导航空壳入库修复

本次补齐 consultation-document-reference-fix-2026-09-06.md 尚未覆盖的两处根因。

## 根因与修改

采集记录 73137 的 URL 分块向量以 source_type=document 入库，未关联之后生成的 bake_documents 945。RAG 在融合前新增正式文档解析：按内部 ID 或与数据库一致的规范化 URL 读取当前文档，替换旧摘要并带回 document_id/artifact_id；保留原通道分数并消除同通道重复票。已删除、导航空壳以及存在歧义的文档不再用原始向量兜底。尚未烘焙的页面以 pending_document 返回，悬浮球显示“文档片段”。

原文档门槛仅检查文档 URL、标题及字符数。H3 的 1144 字符页面是长导航树，生成的 655 字符文档也是导航目录。新增保守正文门禁：加载失败或多种导航/UI 信号同时存在且缺少实质语句时拒绝。应用于采集分块、正式文档分块、原始全文兜底、RAG 上下文、Rust 文档烘焙证据与网页刷新写回。共享样例覆盖空壳、Markdown 导航、正常正文、故障排查文章、普通大纲和代码文档；不将“有目录”本身作为拒绝条件。

旧咨询历史不改写。新增 GET /api/bake/documents?source_url=... 精确身份查找，返回至多一个有效文档；前端旧 URL 引用优先打开该文档详情，未入库或已删除时仍可打开原网页。

## 数据处理

仅隔离经现场确认的错误文档 945，使用 soft-delete，不删除原始 capture、向量和咨询历史。RAG 的身份解析会阻止该 URL 的旧向量复活。

脚本：ai-sidecar/scripts/quarantine_document_shells.py，默认 dry run，必须显式给出 ID，拒绝处理不符合空壳门禁的文档。

本机备份与恢复 SQL：
`/Users/xianjiaqi/.memory-bread/repair-backups/document-shells-1788692050741/`

备份包含原文档行与收藏状态。恢复前应核对该目录的 backup.json；restore.sql 只恢复本次隔离时间戳匹配的文档。

## 验证

- Python：252 项通过（RAG、文档向量、悬浮咨询、后台处理、隔离与恢复）。
- Rust bake_service：63 项通过；相关文档 API：5 项通过。
- 前端 App/悬浮球/咨询历史：72 项通过。
- Python 3.9.25 py_compile、TypeScript/Vite build、Core release build、git diff --check 通过。
- 本机 /references 重放“SMACT的文档”：7 条均为具有内部 ID 的文档；第一条 80《容器云 GPU 指标采集项目》，无 945。结果在 /tmp/mb-document-root-live/smact.json；没有调用生成模型或新增咨询历史。
- 本机精确 URL 接口：GPU URL 返回 80，H3 URL 返回空列表。
- 开发版 localhost:1420 页面操作：打开原咨询 145 的旧 AIGC 引用（无 document_id），成功出现《AIGC 图生视频 RPC 接入文档 - 云文档》详情、ID 263 和正文。此次实际页面验收覆盖咨询历史共享跳转；原生悬浮窗口事件传递由组件测试验证。
- 验收时存在其他启动器执行工作区重启，未反复抢占服务；恢复后完成了上述真实接口及页面验收。未打包 DMG。
