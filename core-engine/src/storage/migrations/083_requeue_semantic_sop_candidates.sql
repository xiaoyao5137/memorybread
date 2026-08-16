-- Re-enter only recent timelines that the previous direct-operation gate
-- explicitly rejected despite having multiple source captures. The candidate
-- must also carry execution and observable-result semantics. This keeps the
-- one-time replay bounded and reuses the existing unified Bundle inference.
DELETE FROM bake_retry_state
WHERE timeline_id IN (
    SELECT t.id
    FROM timelines t
    WHERE COALESCE(t.updated_at_ms, 0) >=
              (CAST(strftime('%s', 'now', '-2 days') AS INTEGER) * 1000)
      AND NOT EXISTS (SELECT 1 FROM bake_sops s WHERE s.timeline_id = t.id)
      AND EXISTS (
            SELECT 1
            FROM bake_candidate_audits a
            WHERE a.timeline_id = t.id
              AND a.created_at_ms >=
                    (CAST(strftime('%s', 'now', '-2 days') AS INTEGER) * 1000)
              AND a.source_capture_count >= 2
              AND a.sop_eligible = 0
              AND a.sop_eligibility_reason = 'insufficient_operation_evidence_nodes'
      )
      AND (
            lower(COALESCE(t.activity_type, '')) LIKE '%coding%'
         OR lower(COALESCE(t.activity_type, '')) LIKE '%ask_ai%'
         OR COALESCE(t.category, '') LIKE '%代码%'
      )
      AND (
            (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%修复%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%修改%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%实现%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%执行%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%运行%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%完成%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%fixed%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%implemented%'
      )
      AND (
            (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%测试通过%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%验证%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%构建成功%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%编译通过%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%已完成%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%解决%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%passed%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%verified%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%completed%'
      )
);

UPDATE timelines
SET updated_at = CURRENT_TIMESTAMP,
    updated_at_ms = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE id IN (
    SELECT t.id
    FROM timelines t
    WHERE COALESCE(t.updated_at_ms, 0) >=
              (CAST(strftime('%s', 'now', '-2 days') AS INTEGER) * 1000)
      AND NOT EXISTS (SELECT 1 FROM bake_sops s WHERE s.timeline_id = t.id)
      AND EXISTS (
            SELECT 1
            FROM bake_candidate_audits a
            WHERE a.timeline_id = t.id
              AND a.created_at_ms >=
                    (CAST(strftime('%s', 'now', '-2 days') AS INTEGER) * 1000)
              AND a.source_capture_count >= 2
              AND a.sop_eligible = 0
              AND a.sop_eligibility_reason = 'insufficient_operation_evidence_nodes'
      )
      AND (
            lower(COALESCE(t.activity_type, '')) LIKE '%coding%'
         OR lower(COALESCE(t.activity_type, '')) LIKE '%ask_ai%'
         OR COALESCE(t.category, '') LIKE '%代码%'
      )
      AND (
            (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%修复%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%修改%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%实现%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%执行%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%运行%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%完成%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%fixed%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%implemented%'
      )
      AND (
            (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%测试通过%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%验证%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%构建成功%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%编译通过%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%已完成%'
         OR (COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%解决%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%passed%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%verified%'
         OR lower(COALESCE(t.summary, '') || ' ' || COALESCE(t.overview, '') || ' ' || COALESCE(t.details, '')) LIKE '%completed%'
      )
);
