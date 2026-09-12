-- Append-only evidence for each actual source check, even if text deduplicates.
CREATE TABLE bake_document_source_checks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
 snapshot_id INTEGER REFERENCES bake_document_source_snapshots(id),
 checked_at INTEGER NOT NULL,
 evidence_json TEXT NOT NULL
);
CREATE INDEX idx_document_source_checks ON bake_document_source_checks(document_id,id DESC);
