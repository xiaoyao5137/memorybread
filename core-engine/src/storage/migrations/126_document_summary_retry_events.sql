CREATE TABLE document_summary_retry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    source_snapshot_id INTEGER NOT NULL,
    expected_updated_at INTEGER NOT NULL,
    previous_attempts INTEGER NOT NULL,
    previous_state TEXT NOT NULL,
    previous_error TEXT,
    previous_record_json TEXT NOT NULL,
    requested_at INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason='explicit_retry')
);
