-- 采集尝试审计：记录成功、隐私跳过、压力降级、连续性复用和失败原因。
-- 隐私跳过行不写应用名、窗口标题或正文，只保留时间与原因。
CREATE TABLE IF NOT EXISTS capture_attempts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at             INTEGER NOT NULL,
    event_type              TEXT NOT NULL,
    outcome                 TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    capture_id              INTEGER,
    related_capture_id      INTEGER,
    app_name                TEXT,
    win_title               TEXT,
    is_private              INTEGER NOT NULL DEFAULT 0,
    effective_interval_secs INTEGER,
    created_at              INTEGER NOT NULL,
    FOREIGN KEY (capture_id) REFERENCES captures(id) ON DELETE SET NULL,
    FOREIGN KEY (related_capture_id) REFERENCES captures(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_capture_attempts_observed_at
ON capture_attempts(observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_capture_attempts_outcome_reason
ON capture_attempts(outcome, reason, observed_at DESC);

INSERT INTO schema_migrations (version, applied_at)
VALUES ('073_capture_attempt_audit', CAST(strftime('%s', 'now') * 1000 AS INTEGER));
