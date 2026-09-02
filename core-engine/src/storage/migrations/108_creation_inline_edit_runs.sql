CREATE TABLE IF NOT EXISTS creation_inline_edit_runs (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    history_id INTEGER NOT NULL,
    operation_fingerprint TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    base_revision_no INTEGER NOT NULL,
    base_document_hash TEXT NOT NULL,
    base_content TEXT NOT NULL,
    replacement_markdown TEXT,
    result_content TEXT,
    result_hash TEXT,
    result_revision_no INTEGER,
    document_patch_json TEXT,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (history_id) REFERENCES creation_history(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_creation_inline_edit_runs_session_status
    ON creation_inline_edit_runs(session_id, status, updated_at DESC);
