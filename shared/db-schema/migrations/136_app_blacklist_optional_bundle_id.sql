-- App exclusions can be identified by their Bundle ID or by their display name.
-- Preserve every existing row and its ID while replacing the legacy NOT NULL constraint.
BEGIN;

CREATE TABLE app_blacklist_optional_bundle (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id  TEXT UNIQUE,
    app_name   TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    reason     TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO app_blacklist_optional_bundle
    (id, bundle_id, app_name, enabled, reason, created_at, updated_at)
-- Only the exact empty-string sentinel is normalized. Legacy whitespace IDs
-- may be distinct rows with the same name and must remain individually intact.
SELECT id, NULLIF(bundle_id, ''), app_name, enabled, reason, created_at, updated_at
FROM app_blacklist;

DROP TABLE app_blacklist;
ALTER TABLE app_blacklist_optional_bundle RENAME TO app_blacklist;

CREATE UNIQUE INDEX idx_app_blacklist_name_only
    ON app_blacklist(app_name COLLATE NOCASE)
    WHERE bundle_id IS NULL;
CREATE INDEX idx_app_blacklist_bundle
    ON app_blacklist(bundle_id) WHERE enabled = 1;

COMMIT;
