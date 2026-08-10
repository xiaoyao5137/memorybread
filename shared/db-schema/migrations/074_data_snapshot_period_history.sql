-- 074: 数据快照按自然周阶段保留历史，不再永久覆盖同一数据源。

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_data_snapshots_single_latest;

ALTER TABLE data_snapshots
ADD COLUMN period_granularity TEXT NOT NULL DEFAULT 'week'
    CHECK (period_granularity IN ('week'));
ALTER TABLE data_snapshots
ADD COLUMN period_key TEXT NOT NULL DEFAULT '';
ALTER TABLE data_snapshots
ADD COLUMN period_start_at INTEGER;
ALTER TABLE data_snapshots
ADD COLUMN period_end_at INTEGER;

-- Unix epoch 的首个周一为 1970-01-05 00:00:00 UTC（345600000ms）。
UPDATE data_snapshots
SET period_start_at =
        ((COALESCE(observed_at, collected_at) - 345600000) / 604800000)
        * 604800000 + 345600000,
    period_end_at =
        ((COALESCE(observed_at, collected_at) - 345600000) / 604800000)
        * 604800000 + 950399999,
    period_key = 'week:' || CAST(
        ((COALESCE(observed_at, collected_at) - 345600000) / 604800000)
        * 604800000 + 345600000 AS TEXT
    );

CREATE UNIQUE INDEX idx_data_snapshots_source_period
ON data_snapshots(source_id, period_key);

CREATE INDEX idx_data_snapshots_period
ON data_snapshots(period_start_at DESC, source_id);

COMMIT;
