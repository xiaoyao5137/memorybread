-- 092: timeline 数据物化状态跟踪完整输入版本。
-- 091 只记录合并更新时间，无法检测同毫秒事实变化或新增关联 capture；
-- 重建状态表并清空历史水位，让所有 timeline 受控重物化一次。
DROP INDEX IF EXISTS idx_data_timeline_materialization_updated;
DROP TABLE IF EXISTS data_timeline_materialization_state;

CREATE TABLE data_timeline_materialization_state (
    timeline_id               INTEGER PRIMARY KEY REFERENCES timelines(id) ON DELETE CASCADE,
    timeline_updated_at_ms    INTEGER NOT NULL DEFAULT 0,
    fact_updated_at_ms        INTEGER NOT NULL DEFAULT 0,
    fact_accepted_count       INTEGER NOT NULL DEFAULT 0,
    fact_contract_version     TEXT    NOT NULL DEFAULT '',
    capture_count             INTEGER NOT NULL DEFAULT 0,
    max_capture_id            INTEGER NOT NULL DEFAULT 0,
    materialized_at           INTEGER NOT NULL
);

CREATE INDEX idx_data_timeline_materialization_updated
ON data_timeline_materialization_state(
    fact_updated_at_ms DESC,
    timeline_updated_at_ms DESC,
    timeline_id DESC
);
