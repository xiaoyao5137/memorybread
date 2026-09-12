ALTER TABLE bake_documents ADD COLUMN summary_source_snapshot_id INTEGER
    REFERENCES bake_document_source_snapshots(id) ON DELETE SET NULL;

ALTER TABLE document_source_mismatch_events RENAME TO document_source_mismatch_events_previous;
DROP INDEX idx_document_source_mismatch_time;
CREATE TABLE document_source_mismatch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    component TEXT NOT NULL CHECK(component IN ('rag','vector_write','creation','vector_schedule')),
    reason TEXT NOT NULL CHECK(reason IN ('head_invalid','snapshot_mismatch','index_version_mismatch','body_mismatch','document_missing','summary_version_mismatch')),
    expected_snapshot_id INTEGER,
    observed_snapshot_id INTEGER,
    occurrences INTEGER NOT NULL CHECK(occurrences > 0),
    observed_at INTEGER NOT NULL
);
INSERT INTO document_source_mismatch_events SELECT * FROM document_source_mismatch_events_previous;
DROP TABLE document_source_mismatch_events_previous;
CREATE INDEX idx_document_source_mismatch_time ON document_source_mismatch_events(observed_at);
