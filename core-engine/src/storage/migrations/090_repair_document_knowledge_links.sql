-- 修复历史自动文档把 timeline_id 写入 linked_knowledge_ids 的命名空间污染。
--
-- source_memory_ids 在 bake 文档中实际保存来源 timeline id；旧实现曾将同一数组
-- 复制到 linked_knowledge_ids。知识表自增到相同数值后，这些 timeline id 会被
-- 图谱误解析为无关的 bake_knowledge.id。
--
-- 迁移策略：
-- 1. 仅处理自动提炼文档，保护手工文档的显式引用；
-- 2. 保留不与 source_memory_ids 重合的现有引用；
-- 3. 对每个来源 timeline，通过 durable source link 映射为真实 knowledge id；
-- 4. 没有知识产物映射的来源 timeline 不产生知识引用。
UPDATE bake_documents AS document
SET linked_knowledge_ids = (
    SELECT json_group_array(reference_id)
    FROM (
        SELECT DISTINCT CAST(existing_ref.value AS TEXT) AS reference_id
        FROM json_each(
            CASE
                WHEN json_valid(COALESCE(document.linked_knowledge_ids, '[]'))
                THEN document.linked_knowledge_ids
                ELSE '[]'
            END
        ) AS existing_ref
        WHERE NOT EXISTS (
            SELECT 1
            FROM json_each(
                CASE
                    WHEN json_valid(COALESCE(document.source_memory_ids, '[]'))
                    THEN document.source_memory_ids
                    ELSE '[]'
                END
            ) AS source_ref
            WHERE CAST(source_ref.value AS TEXT) = CAST(existing_ref.value AS TEXT)
        )

        UNION

        SELECT DISTINCT CAST(source_link.artifact_id AS TEXT) AS reference_id
        FROM json_each(
            CASE
                WHEN json_valid(COALESCE(document.source_memory_ids, '[]'))
                THEN document.source_memory_ids
                ELSE '[]'
            END
        ) AS source_ref
        JOIN bake_artifact_source_links AS source_link
          ON source_link.artifact_kind = 'knowledge'
         AND source_link.source_timeline_id = CAST(source_ref.value AS INTEGER)
    ) AS repaired_references
)
WHERE document.deleted_at IS NULL
  AND COALESCE(document.creation_mode, '') IN ('llm_bake', 'auto')
  AND EXISTS (
      SELECT 1
      FROM json_each(
          CASE
              WHEN json_valid(COALESCE(document.linked_knowledge_ids, '[]'))
              THEN document.linked_knowledge_ids
              ELSE '[]'
          END
      ) AS existing_ref
      JOIN json_each(
          CASE
              WHEN json_valid(COALESCE(document.source_memory_ids, '[]'))
              THEN document.source_memory_ids
              ELSE '[]'
          END
      ) AS source_ref
        ON CAST(source_ref.value AS TEXT) = CAST(existing_ref.value AS TEXT)
  );
