-- 历史单指标数据源清理不能在每轮数据物化中无界扫描全库。
-- 游标使清理可恢复、可分批；复合索引覆盖清理时的两种关联方向。
CREATE TABLE IF NOT EXISTS data_legacy_cleanup_state (
    singleton_id     INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    cursor_source_id INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL
);

INSERT OR IGNORE INTO data_legacy_cleanup_state (
    singleton_id, cursor_source_id, updated_at
) VALUES (1, 0, 0);

CREATE INDEX IF NOT EXISTS idx_data_source_links_timeline_kind_source
ON data_source_links(timeline_id, link_kind, source_id);

CREATE INDEX IF NOT EXISTS idx_data_source_links_source_timeline_kind
ON data_source_links(source_id, timeline_id, link_kind);

CREATE INDEX IF NOT EXISTS idx_data_sources_kind_deleted_key_id
ON data_sources(source_kind, deleted_at, canonical_key, id);
