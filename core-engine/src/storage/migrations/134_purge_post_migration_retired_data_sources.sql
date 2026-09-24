-- 133 执行后，后台有界迁移仍可能继续退休已被 v16 完整覆盖的旧源。
-- 清理部署窗口内产生的软删除记录；后续由运行时代码在退休时立即清理。
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
