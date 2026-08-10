-- 072: 清理 bake_retry_state 幽灵记录，并重排被输出缺陷误杀的候选。
--
-- 背景：
-- 1. 不可重试的存储错误（如 UNIQUE constraint failed: bake_documents.document_identity）
--    会永久丢弃候选并推进 unified watermark，但 bake_retry_state 行残留，
--    监控页按 failure_count 统计导致"正在退避重试"长期误报。这批候选实际
--    已有烘焙产物，只需删除残留行。
-- 2. 旧版 JSON 解析器无法容忍 think 块重复输出、字符串内字面换行、游离引号、
--    尾逗号、裸数组元素、字符串外 \" 过度转义等交织缺陷，且 temperature=0
--    使有界重试确定性复现同一失败，3 次耗尽后候选被误判终态丢弃。解析容错
--    增强上线后，这批仍无产物的候选应回退水位重新提炼。
--
-- 顺序与 061/070 相同：先回退 unified watermark 到最早重排候选之前，
-- 再删除失败标记；只删标记会因水位已跨过候选而继续漏处理。

UPDATE bake_watermarks
SET last_processed_ts = MIN(
        last_processed_ts,
        COALESCE(
            (
                SELECT MIN(
                    MAX(
                        COALESCE(t.updated_at_ms, 0),
                        COALESCE(
                            (SELECT MAX(c.ts) FROM captures c WHERE c.timeline_id = t.id),
                            0
                        )
                    )
                ) - 1
                FROM bake_retry_state r
                JOIN timelines t ON t.id = r.timeline_id
                WHERE (
                        r.last_error LIKE '%BAKE_OUTPUT_INVALID%'
                     OR r.last_error LIKE '%BAKE_OUTPUT_TRUNCATED%'
                     OR r.last_error LIKE '%truncated_json%'
                      )
                  AND NOT EXISTS (
                        SELECT 1 FROM bake_knowledge bk WHERE bk.timeline_id = t.id
                      )
                  AND NOT EXISTS (
                        SELECT 1 FROM bake_sops bs WHERE bs.timeline_id = t.id
                      )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM bake_documents bd
                        WHERE bd.deleted_at IS NULL
                          AND (
                               (
                                   json_valid(COALESCE(bd.source_memory_ids, '[]'))
                                   AND EXISTS (
                                       SELECT 1 FROM json_each(bd.source_memory_ids)
                                       WHERE CAST(json_each.value AS TEXT) = CAST(t.id AS TEXT)
                                   )
                               )
                            OR (
                                   json_valid(COALESCE(bd.source_episode_ids, '[]'))
                                   AND EXISTS (
                                       SELECT 1 FROM json_each(bd.source_episode_ids)
                                       WHERE CAST(json_each.value AS TEXT) = CAST(t.id AS TEXT)
                                   )
                               )
                          )
                      )
            ),
            last_processed_ts
        )
    ),
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE pipeline_name = 'unified'
  AND EXISTS (
      SELECT 1
      FROM bake_retry_state r
      WHERE r.last_error LIKE '%BAKE_OUTPUT_INVALID%'
         OR r.last_error LIKE '%BAKE_OUTPUT_TRUNCATED%'
         OR r.last_error LIKE '%truncated_json%'
  );

-- 重排仍无产物的输出缺陷候选：删除失败标记后它们会重新进入提炼队列。
DELETE FROM bake_retry_state
WHERE (
        last_error LIKE '%BAKE_OUTPUT_INVALID%'
     OR last_error LIKE '%BAKE_OUTPUT_TRUNCATED%'
     OR last_error LIKE '%truncated_json%'
      )
  AND EXISTS (SELECT 1 FROM timelines t WHERE t.id = bake_retry_state.timeline_id)
  AND NOT EXISTS (
        SELECT 1 FROM bake_knowledge bk
        WHERE bk.timeline_id = bake_retry_state.timeline_id
      )
  AND NOT EXISTS (
        SELECT 1 FROM bake_sops bs
        WHERE bs.timeline_id = bake_retry_state.timeline_id
      )
  AND NOT EXISTS (
        SELECT 1
        FROM bake_documents bd
        WHERE bd.deleted_at IS NULL
          AND (
               (
                   json_valid(COALESCE(bd.source_memory_ids, '[]'))
                   AND EXISTS (
                       SELECT 1 FROM json_each(bd.source_memory_ids)
                       WHERE CAST(json_each.value AS TEXT)
                           = CAST(bake_retry_state.timeline_id AS TEXT)
                   )
               )
            OR (
                   json_valid(COALESCE(bd.source_episode_ids, '[]'))
                   AND EXISTS (
                       SELECT 1 FROM json_each(bd.source_episode_ids)
                       WHERE CAST(json_each.value AS TEXT)
                           = CAST(bake_retry_state.timeline_id AS TEXT)
                   )
               )
          )
      );

-- 清理其余幽灵记录（已有产物或不可重试错误的残留行），不影响水位：
-- 这些候选要么已被成功提炼，要么因不可重试错误永久丢弃，重试标记无意义。
DELETE FROM bake_retry_state
WHERE last_error LIKE '%UNIQUE constraint failed: bake_documents.document_identity%'
   OR EXISTS (
        SELECT 1 FROM bake_knowledge bk
        WHERE bk.timeline_id = bake_retry_state.timeline_id
      )
   OR EXISTS (
        SELECT 1 FROM bake_sops bs
        WHERE bs.timeline_id = bake_retry_state.timeline_id
      )
   OR EXISTS (
        SELECT 1
        FROM bake_documents bd
        WHERE bd.deleted_at IS NULL
          AND (
               (
                   json_valid(COALESCE(bd.source_memory_ids, '[]'))
                   AND EXISTS (
                       SELECT 1 FROM json_each(bd.source_memory_ids)
                       WHERE CAST(json_each.value AS TEXT)
                           = CAST(bake_retry_state.timeline_id AS TEXT)
                   )
               )
            OR (
                   json_valid(COALESCE(bd.source_episode_ids, '[]'))
                   AND EXISTS (
                       SELECT 1 FROM json_each(bd.source_episode_ids)
                       WHERE CAST(json_each.value AS TEXT)
                           = CAST(bake_retry_state.timeline_id AS TEXT)
                   )
               )
          )
      );
