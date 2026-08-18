-- 020_add_detailed_content.sql
-- 为 bake_knowledge, bake_sops 更新 FTS 触发器以包含 detailed_content 字段
-- 列本身的补全由 031 的 run_ensure_full_schema() 幂等处理；
-- designs/bake_designs 在 021_unify_bake_designs 中整体重建，不在此处理。

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

