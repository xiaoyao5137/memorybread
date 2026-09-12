ALTER TABLE bake_documents ADD COLUMN summary_generation_version TEXT;
CREATE TABLE document_summary_jobs (
    document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES bake_document_source_snapshots(id) ON DELETE CASCADE,
    expected_updated_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_id TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK(state IN ('pending','running','completed','blocked','superseded')),
    last_error TEXT,
    PRIMARY KEY(document_id,source_snapshot_id,expected_updated_at)
);
