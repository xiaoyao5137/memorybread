-- User-requested replacement preserves the previous text and provenance.
-- UUID identity keeps imported history distinct from local autoincrement IDs.
CREATE TABLE document_summary_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_key TEXT NOT NULL UNIQUE,
    document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
    record_json TEXT NOT NULL,
    saved_at INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason='explicit_regeneration')
);
CREATE INDEX idx_document_summary_versions_document ON document_summary_versions(document_id,saved_at);
