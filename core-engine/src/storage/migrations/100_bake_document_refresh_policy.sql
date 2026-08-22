-- 100: 烘焙文档的刷新策略与刷新状态。
--
-- refresh_policy 只表达“是否允许浏览器即时刷新”：
--   auto   默认值，由刷新资格判定根据“原地更新证据”决定（见 services/document_refresh.rs）
--   always 显式允许（预留给用户手动开启）
--   never  显式禁止（本地文件类文档、用户手动关闭）
-- last_refresh_checked_at_ms 记录最近一次浏览器新鲜度检查时间，用于检查节流；
-- last_refresh_error 记录最近一次刷新失败原因，PAGE_GONE 会永久阻止后续刷新。

ALTER TABLE bake_documents
    ADD COLUMN refresh_policy TEXT NOT NULL DEFAULT 'auto';

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_checked_at_ms INTEGER NOT NULL DEFAULT 0;

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_error TEXT;
