-- 095: 恢复被数据集级覆盖判断误停用、但仍含未覆盖指标行的旧自动数据项。
-- 只处理 memory:semantic 自动项；手工数据和用户删除的其他类型不受影响。
UPDATE data_sources AS legacy
SET status = 'active',
    deleted_at = NULL,
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE legacy.canonical_key LIKE 'memory:semantic:%'
  AND legacy.deleted_at IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM data_snapshots snapshot,
           json_each(snapshot.structured_data, '$.metric_rows') old_row
      WHERE snapshot.source_id = legacy.id
        AND snapshot.id = (
            SELECT latest.id
            FROM data_snapshots latest
            WHERE latest.source_id = legacy.id
            ORDER BY latest.collected_at DESC, latest.id DESC
            LIMIT 1
        )
        AND TRIM(COALESCE(json_extract(old_row.value, '$.metric'), '')) <> ''
        AND TRIM(COALESCE(json_extract(old_row.value, '$.value'), '')) <> ''
        AND NOT EXISTS (
            SELECT 1
            FROM data_source_links legacy_link
            JOIN data_source_links dataset_link
              ON dataset_link.timeline_id = legacy_link.timeline_id
             AND dataset_link.link_kind = 'work_memory'
            JOIN data_sources dataset ON dataset.id = dataset_link.source_id
            JOIN data_snapshots dataset_snapshot ON dataset_snapshot.source_id = dataset.id
            JOIN json_each(dataset_snapshot.structured_data, '$.metric_rows') dataset_row
            WHERE legacy_link.source_id = legacy.id
              AND dataset.deleted_at IS NULL
              AND dataset.canonical_key LIKE 'memory:timeline-dataset:data-memory.v16:%'
              AND LOWER(TRIM(json_extract(dataset_row.value, '$.metric')))
                  = LOWER(TRIM(json_extract(old_row.value, '$.metric')))
              AND LOWER(TRIM(json_extract(dataset_row.value, '$.value')))
                  = LOWER(TRIM(json_extract(old_row.value, '$.value')))
        )
  );

DELETE FROM data_timeline_materialization_state;
