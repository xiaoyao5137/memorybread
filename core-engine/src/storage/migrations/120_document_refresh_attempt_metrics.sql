-- Operational metrics contain no source text, URL, fingerprint or arbitrary error.
ALTER TABLE bake_document_refresh_observations ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 0;
CREATE TABLE bake_document_refresh_attempt_metrics (
    lease_id TEXT PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    queue_wait_ms INTEGER NOT NULL,
    execution_wall_ms INTEGER,
    scheduled_retry_ms INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'running'
        CHECK(outcome IN ('running','completed','pending','blocked','cancelled','interrupted','paused'))
);
CREATE INDEX idx_document_refresh_attempt_time ON bake_document_refresh_attempt_metrics(started_at);
