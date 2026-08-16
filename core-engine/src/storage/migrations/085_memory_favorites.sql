CREATE TABLE IF NOT EXISTS memory_favorites (
    resource_kind TEXT NOT NULL CHECK (
        resource_kind IN ('knowledge', 'operation', 'data', 'document')
    ),
    resource_id INTEGER NOT NULL CHECK (resource_id > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (resource_kind, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_favorites_updated
ON memory_favorites(updated_at DESC, resource_kind, resource_id);
