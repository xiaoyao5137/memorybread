ALTER TABLE bake_retry_state ADD COLUMN last_error_code TEXT;
ALTER TABLE bake_retry_state ADD COLUMN next_retry_at_ms INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_bake_retry_state_schedule
  ON bake_retry_state(failure_count, next_retry_at_ms);

-- 历史版本可能在产物已经落盘后仍残留 retry 行；先清掉，避免升级后继续
-- 把已完成候选带入重试/死信统计。
DELETE FROM bake_retry_state
WHERE EXISTS (
        SELECT 1 FROM bake_knowledge bk
        WHERE bk.timeline_id = bake_retry_state.timeline_id
      )
   OR EXISTS (
        SELECT 1 FROM bake_sops bs
        WHERE bs.timeline_id = bake_retry_state.timeline_id
      )
   OR EXISTS (
        SELECT 1 FROM bake_documents bd
        WHERE bd.deleted_at IS NULL
          AND (
               (json_valid(COALESCE(bd.source_memory_ids, '[]')) AND EXISTS (
                    SELECT 1 FROM json_each(bd.source_memory_ids)
                    WHERE CAST(json_each.value AS TEXT) = CAST(bake_retry_state.timeline_id AS TEXT)
               ))
            OR (json_valid(COALESCE(bd.source_episode_ids, '[]')) AND EXISTS (
                    SELECT 1 FROM json_each(bd.source_episode_ids)
                    WHERE CAST(json_each.value AS TEXT) = CAST(bake_retry_state.timeline_id AS TEXT)
               ))
          )
      );
