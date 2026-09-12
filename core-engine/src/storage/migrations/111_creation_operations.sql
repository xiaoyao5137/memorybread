CREATE TABLE IF NOT EXISTS creation_operations (
    operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    instruction_id TEXT NOT NULL,
    instruction TEXT NOT NULL,
    history_id INTEGER NOT NULL REFERENCES creation_history(id) ON DELETE CASCADE,
    base_revision INTEGER NOT NULL,
    base_document TEXT NOT NULL,
    original_document TEXT NOT NULL,
    retry_checkpoint_json TEXT,
    checkpoint_json TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    last_sequence INTEGER NOT NULL DEFAULT -1,
    result_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_id, instruction_id)
);
CREATE INDEX IF NOT EXISTS idx_creation_operations_pending
ON creation_operations(session_id, status, updated_at);
