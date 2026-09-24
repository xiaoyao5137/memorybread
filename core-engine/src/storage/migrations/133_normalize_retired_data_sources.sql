-- Capture 保留期允许删除原始 capture；数据源关联仍保留 timeline 溯源。
-- 先将已经不存在的 capture 引用正规化为 NULL，避免长期积累无效外键。
--
-- 已软删除且不再被收藏或 Creation 证据引用的数据源不再承担业务作用；
-- 物理删除后由外键级联清理其快照和关联，data_snapshots 的 FTS 触发器
-- 同步移除索引内容。整个过程在一个事务中完成。
BEGIN IMMEDIATE;

UPDATE data_source_links
SET capture_id = NULL
WHERE capture_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM captures WHERE captures.id = data_source_links.capture_id
  );

DELETE FROM data_sources
WHERE deleted_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM memory_favorites favorite
      WHERE favorite.resource_kind = 'data'
        AND favorite.resource_id = data_sources.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM creation_evidence_assets evidence
      WHERE evidence.source_id = data_sources.id
  );

COMMIT;
