-- 091: 数据资产统一以 timeline 为物化入口。
-- capture 只作为 timeline 的来源证据集合，不再承担数据抽取进度游标。
CREATE TABLE IF NOT EXISTS data_timeline_materialization_state (
    timeline_id          INTEGER PRIMARY KEY REFERENCES timelines(id) ON DELETE CASCADE,
    source_updated_at_ms INTEGER NOT NULL,
    materialized_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_data_timeline_materialization_updated
ON data_timeline_materialization_state(source_updated_at_ms DESC, timeline_id DESC);
