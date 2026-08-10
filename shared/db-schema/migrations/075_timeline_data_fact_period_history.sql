-- 075: 为模型结构化数据事实补充自然周阶段标签。
-- 单独立项是为了已应用 074 的开发库也能安全升级。

BEGIN IMMEDIATE;

ALTER TABLE timeline_data_facts
ADD COLUMN period_granularity TEXT NOT NULL DEFAULT 'week'
    CHECK (period_granularity IN ('week'));
ALTER TABLE timeline_data_facts
ADD COLUMN period_key TEXT NOT NULL DEFAULT '';
ALTER TABLE timeline_data_facts
ADD COLUMN period_start_at INTEGER;
ALTER TABLE timeline_data_facts
ADD COLUMN period_end_at INTEGER;

UPDATE timeline_data_facts
SET period_start_at =
        ((COALESCE(observed_at, created_at) - 345600000) / 604800000)
        * 604800000 + 345600000,
    period_end_at =
        ((COALESCE(observed_at, created_at) - 345600000) / 604800000)
        * 604800000 + 950399999,
    period_key = 'week:' || CAST(
        ((COALESCE(observed_at, created_at) - 345600000) / 604800000)
        * 604800000 + 345600000 AS TEXT
    );

CREATE INDEX idx_timeline_data_facts_period
ON timeline_data_facts(period_start_at DESC, timeline_id);

COMMIT;
