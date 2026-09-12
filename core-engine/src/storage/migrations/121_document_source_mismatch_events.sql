-- Local diagnostics only; never store source content, URLs or arbitrary errors.
CREATE TABLE document_source_mismatch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    component TEXT NOT NULL CHECK(component IN ('rag','vector_write','creation','vector_schedule')),
    reason TEXT NOT NULL CHECK(reason IN ('head_invalid','snapshot_mismatch','index_version_mismatch','body_mismatch','document_missing')),
    expected_snapshot_id INTEGER,
    observed_snapshot_id INTEGER,
    occurrences INTEGER NOT NULL CHECK(occurrences > 0),
    observed_at INTEGER NOT NULL
);
CREATE INDEX idx_document_source_mismatch_time ON document_source_mismatch_events(observed_at);
