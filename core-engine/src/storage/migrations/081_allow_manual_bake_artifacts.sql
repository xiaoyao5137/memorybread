-- 081: knowledge / SOP 允许没有来源时间线的用户手动记录。
--
-- 部分数据库因 033 的兼容性短路仍保留了 timeline_id NOT NULL 外键，
-- 导致 UI 新建知识或操作时无法持久化。这里统一重建为可空外键：
-- 自动提炼记录继续关联 timelines，手动记录使用 NULL。

PRAGMA foreign_keys = OFF;

DROP TRIGGER IF EXISTS bake_knowledge_fts_insert;
DROP TRIGGER IF EXISTS bake_knowledge_fts_update;
DROP TRIGGER IF EXISTS bake_knowledge_fts_delete;
DROP TABLE IF EXISTS bake_knowledge_manual_new;

CREATE TABLE bake_knowledge_manual_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timeline_id INTEGER REFERENCES timelines(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT,
    entities TEXT,
    importance INTEGER DEFAULT 3,
    user_verified BOOLEAN DEFAULT 0,
    user_edited BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    source_capture_ids TEXT NOT NULL DEFAULT '[]',
    detailed_content TEXT,
    document_id INTEGER,
    section_ids TEXT DEFAULT '[]',
    source_timeline_ids TEXT DEFAULT '[]'
);

INSERT INTO bake_knowledge_manual_new (
    id, timeline_id, title, summary, content, entities, importance,
    user_verified, user_edited, created_at, updated_at, created_at_ms,
    updated_at_ms, source_capture_ids, detailed_content, document_id,
    section_ids, source_timeline_ids
)
SELECT
    id, timeline_id, title, summary, content, entities, importance,
    user_verified, user_edited, created_at, updated_at, created_at_ms,
    updated_at_ms, COALESCE(source_capture_ids, '[]'), detailed_content,
    document_id, section_ids, source_timeline_ids
FROM bake_knowledge;

DROP TABLE bake_knowledge;
ALTER TABLE bake_knowledge_manual_new RENAME TO bake_knowledge;

CREATE INDEX IF NOT EXISTS idx_bake_knowledge_importance ON bake_knowledge(importance);
CREATE INDEX IF NOT EXISTS idx_bake_knowledge_updated_at_ms ON bake_knowledge(updated_at_ms);
CREATE INDEX IF NOT EXISTS idx_bake_knowledge_timeline_id ON bake_knowledge(timeline_id);

CREATE TRIGGER bake_knowledge_fts_insert AFTER INSERT ON bake_knowledge BEGIN
    INSERT INTO bake_knowledge_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_knowledge_fts_update AFTER UPDATE ON bake_knowledge BEGIN
    INSERT INTO bake_knowledge_fts(bake_knowledge_fts, rowid, title, summary, content, entities)
    VALUES ('delete', old.id, old.title, old.summary, COALESCE(old.detailed_content, old.content, ''), old.entities);
    INSERT INTO bake_knowledge_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_knowledge_fts_delete AFTER DELETE ON bake_knowledge BEGIN
    INSERT INTO bake_knowledge_fts(bake_knowledge_fts, rowid, title, summary, content, entities)
    VALUES ('delete', old.id, old.title, old.summary, COALESCE(old.detailed_content, old.content, ''), old.entities);
END;

DROP TRIGGER IF EXISTS bake_sops_fts_insert;
DROP TRIGGER IF EXISTS bake_sops_fts_update;
DROP TRIGGER IF EXISTS bake_sops_fts_delete;
DROP TABLE IF EXISTS bake_sops_manual_new;

CREATE TABLE bake_sops_manual_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timeline_id INTEGER REFERENCES timelines(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT,
    entities TEXT,
    importance INTEGER DEFAULT 3,
    user_verified BOOLEAN DEFAULT 0,
    user_edited BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    source_capture_ids TEXT DEFAULT '[]',
    detailed_content TEXT
);

INSERT INTO bake_sops_manual_new (
    id, timeline_id, title, summary, content, entities, importance,
    user_verified, user_edited, created_at, updated_at, created_at_ms,
    updated_at_ms, source_capture_ids, detailed_content
)
SELECT
    id, timeline_id, title, summary, content, entities, importance,
    user_verified, user_edited, created_at, updated_at, created_at_ms,
    updated_at_ms, COALESCE(source_capture_ids, '[]'), detailed_content
FROM bake_sops;

DROP TABLE bake_sops;
ALTER TABLE bake_sops_manual_new RENAME TO bake_sops;

CREATE INDEX IF NOT EXISTS idx_bake_sops_importance ON bake_sops(importance);
CREATE INDEX IF NOT EXISTS idx_bake_sops_updated_at_ms ON bake_sops(updated_at_ms);
CREATE INDEX IF NOT EXISTS idx_bake_sops_timeline_id ON bake_sops(timeline_id);

CREATE TRIGGER bake_sops_fts_insert AFTER INSERT ON bake_sops BEGIN
    INSERT INTO bake_sops_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_sops_fts_update AFTER UPDATE ON bake_sops BEGIN
    INSERT INTO bake_sops_fts(bake_sops_fts, rowid, title, summary, content, entities)
    VALUES ('delete', old.id, old.title, old.summary, COALESCE(old.detailed_content, old.content, ''), old.entities);
    INSERT INTO bake_sops_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_sops_fts_delete AFTER DELETE ON bake_sops BEGIN
    INSERT INTO bake_sops_fts(bake_sops_fts, rowid, title, summary, content, entities)
    VALUES ('delete', old.id, old.title, old.summary, COALESCE(old.detailed_content, old.content, ''), old.entities);
END;

INSERT INTO bake_knowledge_fts(bake_knowledge_fts) VALUES ('rebuild');
INSERT INTO bake_sops_fts(bake_sops_fts) VALUES ('rebuild');

PRAGMA foreign_keys = ON;
