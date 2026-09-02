CREATE INDEX IF NOT EXISTS idx_creation_history_lifecycle_updated
ON creation_history(lifecycle_status, updated_at DESC);
