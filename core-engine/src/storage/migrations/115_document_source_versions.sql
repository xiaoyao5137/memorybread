-- Observed source snapshots are distinct from the version applied to a document.
CREATE TABLE IF NOT EXISTS bake_document_source_heads (
    document_id INTEGER PRIMARY KEY REFERENCES bake_documents(id) ON DELETE CASCADE,
    snapshot_id INTEGER NOT NULL REFERENCES bake_document_source_snapshots(id),
    applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bake_document_body_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
    replaced_by_snapshot_id INTEGER NOT NULL REFERENCES bake_document_source_snapshots(id),
    record_json TEXT NOT NULL,
    saved_at INTEGER NOT NULL,
    UNIQUE(document_id, replaced_by_snapshot_id)
);
