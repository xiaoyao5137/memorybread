CREATE TABLE IF NOT EXISTS bake_candidate_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timeline_id INTEGER NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('fresh', 'retry')),
    source_capture_count INTEGER NOT NULL DEFAULT 0,
    effective_capture_count INTEGER NOT NULL DEFAULT 0,
    sop_eligible INTEGER NOT NULL DEFAULT 0 CHECK (sop_eligible IN (0, 1)),
    sop_eligibility_reason TEXT,
    primary_type TEXT,
    classification_reason TEXT,
    sop_model_accepted INTEGER CHECK (sop_model_accepted IN (0, 1)),
    sop_model_reason TEXT,
    sop_payload_valid INTEGER CHECK (sop_payload_valid IN (0, 1)),
    persist_status TEXT NOT NULL DEFAULT 'queued',
    persist_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES bake_runs(id) ON DELETE CASCADE,
    UNIQUE (run_id, timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_bake_candidate_audits_run
    ON bake_candidate_audits(run_id, id);

CREATE INDEX IF NOT EXISTS idx_bake_candidate_audits_sop_funnel
    ON bake_candidate_audits(sop_eligible, sop_model_accepted, persist_status, created_at_ms DESC);
