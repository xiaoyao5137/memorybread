-- 面包屑在本机计算、存储和佩戴；云端只下发计算规则。
CREATE TABLE IF NOT EXISTS breadcrumb_definitions (
    id              TEXT PRIMARY KEY,
    breadcrumb_key  TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    tagline         TEXT NOT NULL,
    description     TEXT NOT NULL,
    icon_key        TEXT NOT NULL,
    palette_key     TEXT NOT NULL,
    rarity          TEXT NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS breadcrumb_rules (
    id              TEXT PRIMARY KEY,
    rule_key        TEXT NOT NULL UNIQUE,
    breadcrumb_id   TEXT NOT NULL REFERENCES breadcrumb_definitions(id),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    period          TEXT NOT NULL,
    metric_key      TEXT NOT NULL,
    threshold       TEXT NOT NULL,
    metric_unit     TEXT NOT NULL,
    increment       INTEGER NOT NULL CHECK (increment > 0),
    version         INTEGER NOT NULL,
    starts_at       TEXT,
    expires_at      TEXT,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS breadcrumb_inventory (
    breadcrumb_id   TEXT PRIMARY KEY REFERENCES breadcrumb_definitions(id),
    quantity        INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    first_earned_at INTEGER NOT NULL,
    last_earned_at  INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS breadcrumb_awards (
    rule_id         TEXT NOT NULL REFERENCES breadcrumb_rules(id),
    period_key      TEXT NOT NULL,
    breadcrumb_id   TEXT NOT NULL REFERENCES breadcrumb_definitions(id),
    observed_value  TEXT NOT NULL,
    increment       INTEGER NOT NULL CHECK (increment > 0),
    rule_version    INTEGER NOT NULL,
    awarded_at      INTEGER NOT NULL,
    PRIMARY KEY (rule_id, period_key)
);

CREATE TABLE IF NOT EXISTS breadcrumb_equipment (
    surface         TEXT PRIMARY KEY,
    breadcrumb_id   TEXT NOT NULL REFERENCES breadcrumb_definitions(id),
    equipped_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_breadcrumb_inventory_last_earned
    ON breadcrumb_inventory(last_earned_at DESC);
