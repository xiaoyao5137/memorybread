# Creation manual document edit v1

创作页对已生成的 Markdown 正文执行手工原地编辑时，调用
`PUT /api/creation/history/:id/document`。`id` 是 `creation_history.id`。
Core 的 `generated_content` 是唯一正式正文；页面草稿在保存成功前不能当作正式正文。
此接口不调用模型，也不写入 Brainstorm 决定或创作简报。

## 请求与响应

请求和响应结构见 `creation-manual-edit.schema.json`。请求字段：

- `session_id`：当前历史记录的会话 ID。旧历史记录没有会话 ID 时传空字符串，
  Core 只凭目标 `history_id`、版本号和正文哈希检查该旧记录，不为其绑定新会话。
- `base_revision_no`：从 `GET /api/creation/history/:id` 读取的 `revision_no`。
- `base_document_hash`：同次读取的原始 `generated_content` 的 UTF-8 字节 SHA-256，
  小写十六进制。不能对渲染后的 HTML、去内部标记的文本或编辑草稿计算。
- `content`：待保存的完整 Markdown 正文，非空白，UTF-8 字节数不超过 1 MiB。

成功返回 `history_id`、`session_id`、`revision_no`、`document_hash`、`content`、
`changed`，发生改动时还返回 `document_patch`。内容与当前正文相同且基线仍有效时
`changed=false`，版本不增加。发生改动时 Core 在一次事务中检查目标是该会话最新
历史行（旧无会话记录按行检查）、终态、版本及当前正文哈希，然后保存正文、
`revision_no+1`、`edit_operation=manual_edit` 和可识别的 Patch 元数据。已有的对话、
执行轨迹、来源、证据、简报及其他历史记录保持原样。对 `failed` 或 `cancelled`
历史的成功保存把该历史行设为 `completed`，不更改独立 Brainstorm 会话状态。

## 冲突与错误

Core 有活跃创作或选区编辑时返回 `409 CREATION_DOCUMENT_EDIT_BUSY`；进行中或无
正式正文时返回 `409 CREATION_DOCUMENT_EDIT_NOT_READY`。版本、哈希、最新行不符
返回 `409 CREATION_BASE_CHANGED`，不得覆盖磁盘正文；页面须保留草稿，重新读取后
让用户核对。空白正文为 `422 CREATION_DOCUMENT_EMPTY`，超过 1 MiB 为
`422 CREATION_DOCUMENT_TOO_LARGE`；无效基线为 `400 CREATION_DOCUMENT_EDIT_INVALID`，
目标记录不存在或会话不符为 `404 CREATION_DOCUMENT_EDIT_NOT_FOUND`。

错误使用 `{ "error": { "code", "message", "retryable", "request_id": null } }`。
旧的 `POST /api/creation/history` 不能用于手工正文保存。兼容生成流程若要在手工编辑
后的正文上继续，必须提供相同 `history_id`、`base_revision_no`、
`base_document_hash`；Core 在同一连接锁内对当前手工编辑版本进行 CAS，缺失或过期
返回 `409 CREATION_BASE_CHANGED`。普通 durable operation 仍按自己的基线 CAS 提交。
`PATCH .../progress` 不提交正式正文。
