-- 070: 恢复被本地模型输出缺陷误判为终态的 bake 候选。
--
-- 旧版 JSON 解析不容忍字符串值内部的游离半角引号（中文引用文本里开引号全角、
-- 闭引号半角），把大量实际可解析的输出判为 BAKE_OUTPUT_INVALID；紧凑重试的
-- 输出上限也低于首次执行，截断候选重试时会被二次截断。解析容错与重试输出
-- 上限修复上线后，这批已经进入终态的候选应当重新参与有界重试。
--
-- 与 061 相同：历史记录已经推进了 unified watermark，必须先回退水位，
-- 再删除失败标记；只删标记会因为水位已跨过候选而继续漏处理。

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
                WHERE r.last_error LIKE '%BAKE_OUTPUT_INVALID%'
                   OR r.last_error LIKE '%BAKE_OUTPUT_TRUNCATED%'
                   OR r.last_error LIKE '%truncated_json%'
                   OR r.last_error LIKE '%code=INFERENCE_TIMEOUT%'
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
         OR r.last_error LIKE '%code=INFERENCE_TIMEOUT%'
  );

DELETE FROM bake_retry_state
WHERE last_error LIKE '%BAKE_OUTPUT_INVALID%'
   OR last_error LIKE '%BAKE_OUTPUT_TRUNCATED%'
   OR last_error LIKE '%truncated_json%'
   OR last_error LIKE '%code=INFERENCE_TIMEOUT%';
