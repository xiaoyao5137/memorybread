-- Observations request a current-source check; they are not applied fingerprints.
CREATE TABLE bake_document_refresh_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    source_timeline_id INTEGER,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','completed','blocked')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    lease_id TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    checked_snapshot_id INTEGER REFERENCES bake_document_source_snapshots(id),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(document_id,fingerprint)
);
CREATE INDEX idx_document_refresh_due ON bake_document_refresh_observations(state,next_attempt_at,document_id);
