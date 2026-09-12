-- Old identity values remain auditable and never authorize automatic merging.
CREATE TABLE IF NOT EXISTS bake_document_identity_aliases (
 document_id INTEGER NOT NULL REFERENCES bake_documents(id) ON DELETE CASCADE,
 identity TEXT NOT NULL,
 identity_version TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(document_id,identity)
);
