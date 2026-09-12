CREATE TABLE IF NOT EXISTS document_candidate_quality_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    timeline_id INTEGER NOT NULL,
    document_id INTEGER,
    observed_at INTEGER NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('precheck','extraction','persistence')),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json))
);
CREATE INDEX IF NOT EXISTS idx_document_candidate_quality_time
    ON document_candidate_quality_events(observed_at);
CREATE INDEX IF NOT EXISTS idx_document_candidate_quality_timeline
    ON document_candidate_quality_events(timeline_id,run_id);
