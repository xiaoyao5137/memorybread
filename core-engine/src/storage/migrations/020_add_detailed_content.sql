-- 020_add_detailed_content.sql
-- 为 bake_knowledge, bake_sops, bake_articles, bake_designs 添加 detailed_content 字段
-- 使用幂等方式（检查字段是否存在）

-- SQLite 不支持 IF NOT EXISTS，所以用 PRAGMA 检查
-- 这个迁移假设在 Rust 代码中处理重复列错误

-- 更新 FTS 触发器以包含 detailed_content

-- bake_knowledge FTS 触发器
DROP TRIGGER IF EXISTS bake_knowledge_fts_insert;
DROP TRIGGER IF EXISTS bake_knowledge_fts_update;

CREATE TRIGGER bake_knowledge_fts_insert AFTER INSERT ON bake_knowledge BEGIN
    INSERT INTO bake_knowledge_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_knowledge_fts_update AFTER UPDATE ON bake_knowledge BEGIN
    DELETE FROM bake_knowledge_fts WHERE rowid = old.id;
    INSERT INTO bake_knowledge_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

-- bake_sops FTS 触发器
DROP TRIGGER IF EXISTS bake_sops_fts_insert;
DROP TRIGGER IF EXISTS bake_sops_fts_update;

CREATE TRIGGER bake_sops_fts_insert AFTER INSERT ON bake_sops BEGIN
    INSERT INTO bake_sops_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_sops_fts_update AFTER UPDATE ON bake_sops BEGIN
    DELETE FROM bake_sops_fts WHERE rowid = old.id;
    INSERT INTO bake_sops_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

-- bake_articles FTS 触发器
DROP TRIGGER IF EXISTS bake_articles_fts_insert;
DROP TRIGGER IF EXISTS bake_articles_fts_update;

CREATE TRIGGER bake_articles_fts_insert AFTER INSERT ON bake_articles BEGIN
    INSERT INTO bake_articles_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_articles_fts_update AFTER UPDATE ON bake_articles BEGIN
    DELETE FROM bake_articles_fts WHERE rowid = old.id;
    INSERT INTO bake_articles_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

-- bake_designs FTS 触发器
DROP TRIGGER IF EXISTS bake_designs_fts_insert;
DROP TRIGGER IF EXISTS bake_designs_fts_update;

CREATE TRIGGER bake_designs_fts_insert AFTER INSERT ON bake_designs BEGIN
    INSERT INTO bake_designs_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;

CREATE TRIGGER bake_designs_fts_update AFTER UPDATE ON bake_designs BEGIN
    DELETE FROM bake_designs_fts WHERE rowid = old.id;
    INSERT INTO bake_designs_fts(rowid, title, summary, content, entities)
    VALUES (new.id, new.title, new.summary, COALESCE(new.detailed_content, new.content, ''), new.entities);
END;
