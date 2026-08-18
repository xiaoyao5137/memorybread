-- 解析器现已支持从局部损坏、数组包裹和重复退化前缀中恢复独立产物。
-- 仅一次性重放已经耗尽、且属于模型输出兼容问题的候选；超时、5xx、
-- 业务门禁和 payload 契约错误继续保留原失败计数。
CREATE TEMP TABLE IF NOT EXISTS _bake_output_requeue_v3 (
  earliest_candidate_ts INTEGER
);

DELETE FROM _bake_output_requeue_v3;

INSERT INTO _bake_output_requeue_v3 (earliest_candidate_ts)
SELECT MIN(
  MAX(
    COALESCE(t.updated_at_ms, 0),
    COALESCE((SELECT MAX(c.ts) FROM captures c WHERE c.timeline_id = t.id), 0)
  )
)
FROM bake_retry_state r
JOIN timelines t ON t.id = r.timeline_id
WHERE r.failure_count >= 3
  AND r.last_error_code IN (
    'BAKE_OUTPUT_INVALID',
    'BAKE_OUTPUT_TRUNCATED',
    'BAKE_MODEL_RESPONSE_INVALID'
  );

UPDATE bake_watermarks
SET last_processed_ts = MIN(
      last_processed_ts,
      COALESCE(
        (SELECT earliest_candidate_ts - 1 FROM _bake_output_requeue_v3),
        last_processed_ts
      )
    ),
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE pipeline_name = 'unified'
  AND (SELECT earliest_candidate_ts FROM _bake_output_requeue_v3) IS NOT NULL;

DELETE FROM bake_retry_state
WHERE failure_count >= 3
  AND last_error_code IN (
    'BAKE_OUTPUT_INVALID',
    'BAKE_OUTPUT_TRUNCATED',
    'BAKE_MODEL_RESPONSE_INVALID'
  );

DROP TABLE _bake_output_requeue_v3;
