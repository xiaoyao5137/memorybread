-- 105 已经应用过的数据库可能保留不满足当前最低证据契约的历史回放项。
-- 这些项目在运行时只会被确定性拒绝，不应继续显示为可执行 bake 库存。
-- 本迁移只收敛确定无资格或已经完成的项目；具备至少两帧证据的候选继续
-- 交给运行时的完整 action/result 归因规则判定。

UPDATE operation_replay_queue
SET status = 'discarded',
    completed_at_ms = CAST(strftime('%s', 'now') * 1000 AS INTEGER),
    claimed_at_ms = NULL
WHERE status IN ('pending', 'claimed')
  AND (
      NOT EXISTS (
          SELECT 1
          FROM timelines t
          WHERE t.id = operation_replay_queue.timeline_id
            AND t.category NOT IN (
                'bake_article', 'bake_knowledge', 'bake_sop', 'legacy_bake_candidate'
            )
      )
      OR EXISTS (
          SELECT 1
          FROM bake_sops existing_sop
          WHERE existing_sop.timeline_id = operation_replay_queue.timeline_id
      )
      OR (
          SELECT COUNT(*)
          FROM captures member
          WHERE member.timeline_id = operation_replay_queue.timeline_id
      ) < 2
      OR COALESCE((
          SELECT retry.failure_count
          FROM bake_retry_state retry
          WHERE retry.timeline_id = operation_replay_queue.timeline_id
      ), 0) >= 3
  );
