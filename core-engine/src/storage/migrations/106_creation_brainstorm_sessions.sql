CREATE TABLE IF NOT EXISTS creation_brainstorm_sessions (
    session_id TEXT PRIMARY KEY,
    root_request TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'exploring',
    revision INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_creation_brainstorm_updated_at
    ON creation_brainstorm_sessions(updated_at DESC);
