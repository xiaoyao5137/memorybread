-- 101: 文档即时刷新的版本化来源快照与完整性状态。
--
-- 原始来源快照与 bake_documents 提炼产物分开保存：
-- - 本轮创作可直接消费已验证的当前快照；
-- - 新快照不会未经质量门禁直接覆盖烘焙文档；
-- - 相同文档和内容指纹只保留一份不可变快照。

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_success_at_ms INTEGER NOT NULL DEFAULT 0;

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_status TEXT NOT NULL DEFAULT 'historical_only';

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_completeness TEXT NOT NULL DEFAULT 'unverified';

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_content_hash TEXT;

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_character_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_segment_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE bake_documents
    ADD COLUMN last_refresh_truncated INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS bake_document_source_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    page_title TEXT NOT NULL,
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    completeness_status TEXT NOT NULL,
    identity_match INTEGER NOT NULL DEFAULT 0,
    reached_end INTEGER NOT NULL DEFAULT 0,
    stable_passes INTEGER NOT NULL DEFAULT 0,
    segment_count INTEGER NOT NULL DEFAULT 0,
    character_count INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    collector TEXT NOT NULL DEFAULT 'browser_attach',
    collected_at INTEGER NOT NULL,
    FOREIGN KEY(document_id) REFERENCES bake_documents(id) ON DELETE CASCADE,
    UNIQUE(document_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_bake_document_source_snapshots_latest
    ON bake_document_source_snapshots(document_id, collected_at DESC, id DESC);
