-- 手工新建（manual）的创作 Skill 需要持久化：放宽 source_kind 的 CHECK 约束，
-- 与 API 层和 UI 支持的来源类型保持一致。SQLite 不支持直接修改 CHECK，需重建表。
BEGIN;

CREATE TABLE creation_skills_next (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_skill_key    TEXT NOT NULL UNIQUE,
    cloud_skill_id      TEXT,
    source_kind         TEXT NOT NULL CHECK (source_kind IN ('creation_history', 'bake_document', 'market', 'imported', 'manual')),
    source_id           TEXT NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL,
    category_id         TEXT,
    common_titles       TEXT NOT NULL DEFAULT '[]',
    title_style         TEXT NOT NULL DEFAULT '',
    text_style          TEXT NOT NULL DEFAULT '',
    diagram_style       TEXT NOT NULL DEFAULT '',
    structure_pattern   TEXT NOT NULL DEFAULT '[]',
    writing_guidelines  TEXT NOT NULL DEFAULT '[]',
    published           INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    deleted_at          INTEGER,
    status              TEXT NOT NULL DEFAULT 'saved' CHECK (status IN ('draft', 'saved')),
    installed           INTEGER NOT NULL DEFAULT 0 CHECK (installed IN (0, 1)),
    section_headings    TEXT NOT NULL DEFAULT '{}',
    field_examples      TEXT NOT NULL DEFAULT '{}',
    example_document    TEXT NOT NULL DEFAULT '',
    package_files       TEXT NOT NULL DEFAULT '[]',
    distinctive_sections TEXT NOT NULL DEFAULT '[]',
    skill_description   TEXT NOT NULL DEFAULT '{}',
    execution_steps     TEXT NOT NULL DEFAULT '[]'
);

INSERT INTO creation_skills_next (
    id, client_skill_key, cloud_skill_id, source_kind, source_id, title, summary,
    category_id, common_titles, title_style, text_style, diagram_style,
    structure_pattern, writing_guidelines, published, created_at, updated_at,
    deleted_at, status, installed, section_headings, field_examples, example_document,
    package_files, distinctive_sections, skill_description, execution_steps
)
SELECT
    id, client_skill_key, cloud_skill_id, source_kind, source_id, title, summary,
    category_id, common_titles, title_style, text_style, diagram_style,
    structure_pattern, writing_guidelines, published, created_at, updated_at,
    deleted_at, status, installed, section_headings, field_examples, example_document,
    package_files, distinctive_sections, skill_description, execution_steps
FROM creation_skills;

DROP TABLE creation_skills;
ALTER TABLE creation_skills_next RENAME TO creation_skills;

CREATE INDEX idx_creation_skills_updated_at
    ON creation_skills(updated_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_creation_skills_source
    ON creation_skills(source_kind, source_id)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX idx_creation_skills_cloud_id
    ON creation_skills(cloud_skill_id)
    WHERE cloud_skill_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX idx_creation_skills_installed
    ON creation_skills(installed, updated_at DESC)
    WHERE deleted_at IS NULL;

COMMIT;
