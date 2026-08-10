-- 071_timelines_fts.sql
-- 为 timelines 表建立 FTS5 全文索引，供时间线/知识/操作等列表页关键词搜索做 FTS 预筛。
-- 同时清理 019_rename_to_timelines 之后遗留的孤立 episodic_memories_fts（内容表已不存在）。

DROP TRIGGER IF EXISTS episodic_memories_fts_insert;
DROP TRIGGER IF EXISTS episodic_memories_fts_update;
DROP TRIGGER IF EXISTS episodic_memories_fts_delete;
DROP TABLE IF EXISTS episodic_memories_fts;

CREATE VIRTUAL TABLE IF NOT EXISTS timelines_fts USING fts5(
    summary,
    overview,
    details,
    entities,
    content='timelines',
    content_rowid='id'
);

-- FTS5 增量同步触发器：external-content 模式下删除必须写 'delete' 指令行
CREATE TRIGGER IF NOT EXISTS timelines_fts_insert AFTER INSERT ON timelines BEGIN
    INSERT INTO timelines_fts(rowid, summary, overview, details, entities)
    VALUES (new.id, new.summary, new.overview, new.details, new.entities);
END;

CREATE TRIGGER IF NOT EXISTS timelines_fts_update AFTER UPDATE ON timelines BEGIN
    INSERT INTO timelines_fts(timelines_fts, rowid, summary, overview, details, entities)
    VALUES ('delete', old.id, old.summary, old.overview, old.details, old.entities);
    INSERT INTO timelines_fts(rowid, summary, overview, details, entities)
    VALUES (new.id, new.summary, new.overview, new.details, new.entities);
END;

CREATE TRIGGER IF NOT EXISTS timelines_fts_delete AFTER DELETE ON timelines BEGIN
    INSERT INTO timelines_fts(timelines_fts, rowid, summary, overview, details, entities)
    VALUES ('delete', old.id, old.summary, old.overview, old.details, old.entities);
END;

-- 回填存量数据
INSERT INTO timelines_fts(timelines_fts) VALUES ('rebuild');
