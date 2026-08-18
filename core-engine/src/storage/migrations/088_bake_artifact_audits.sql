CREATE TABLE IF NOT EXISTS bake_artifact_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timeline_id INTEGER NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('knowledge', 'document', 'sop')),
    deterministic_eligible INTEGER CHECK (deterministic_eligible IN (0, 1)),
    deterministic_reason TEXT,
    model_accepted INTEGER CHECK (model_accepted IN (0, 1)),
    model_reason TEXT,
    payload_present INTEGER CHECK (payload_present IN (0, 1)),
    payload_valid INTEGER CHECK (payload_valid IN (0, 1)),
    artifact_shape TEXT,
    compatibility_recovered INTEGER NOT NULL DEFAULT 0 CHECK (compatibility_recovered IN (0, 1)),
    persist_status TEXT NOT NULL DEFAULT 'pending',
    persist_reason TEXT,
    artifact_id INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES bake_runs(id) ON DELETE CASCADE,
    UNIQUE (run_id, timeline_id, artifact_kind)
);

CREATE INDEX IF NOT EXISTS idx_bake_artifact_audits_timeline
    ON bake_artifact_audits(timeline_id, created_at_ms DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_bake_artifact_audits_failures
    ON bake_artifact_audits(artifact_kind, model_accepted, persist_status, created_at_ms DESC);
