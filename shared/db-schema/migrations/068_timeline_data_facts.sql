CREATE TABLE IF NOT EXISTS timeline_data_fact_runs (
    timeline_id       INTEGER PRIMARY KEY REFERENCES timelines(id) ON DELETE CASCADE,
    contract_version  TEXT    NOT NULL,
    accepted_count    INTEGER NOT NULL DEFAULT 0,
    rejected_count    INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_data_facts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timeline_id        INTEGER NOT NULL REFERENCES timelines(id) ON DELETE CASCADE,
    fact_key           TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    subject            TEXT    NOT NULL,
    action             TEXT    NOT NULL DEFAULT '',
    target_context     TEXT    NOT NULL DEFAULT '',
    dimension          TEXT    NOT NULL DEFAULT '',
    metric             TEXT    NOT NULL,
    value              TEXT    NOT NULL,
    unit               TEXT    NOT NULL DEFAULT '',
    statement          TEXT    NOT NULL,
    evidence_quote     TEXT    NOT NULL,
    confidence         TEXT    NOT NULL DEFAULT 'medium'
                               CHECK (confidence IN ('low', 'medium', 'high')),
    observed_at        INTEGER,
    source_capture_ids TEXT    NOT NULL DEFAULT '[]',
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    UNIQUE(timeline_id, fact_key, dimension, value, unit)
);

CREATE INDEX IF NOT EXISTS idx_timeline_data_facts_timeline
ON timeline_data_facts(timeline_id, id);
