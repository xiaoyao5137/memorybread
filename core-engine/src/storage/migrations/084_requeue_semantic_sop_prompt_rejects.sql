-- The first semantic-workflow replay exposed a prompt conflict: Core accepted
-- the evidence channel, while the model still rejected it for lacking UI
-- clicks. Retry only the latest recent semantic candidates affected by that
-- conflict. On a fresh database there are no mode-tagged historical audits, so
-- this migration is a no-op.
DELETE FROM bake_retry_state
WHERE timeline_id IN (
    SELECT a.timeline_id
    FROM bake_candidate_audits a
    JOIN (
        SELECT timeline_id, MAX(created_at_ms) AS max_created_at_ms
        FROM bake_candidate_audits
        GROUP BY timeline_id
    ) latest
      ON latest.timeline_id = a.timeline_id
     AND latest.max_created_at_ms = a.created_at_ms
    WHERE a.created_at_ms >=
              (CAST(strftime('%s', 'now', '-2 days') AS INTEGER) * 1000)
      AND a.sop_eligible = 1
      AND a.sop_evidence_mode = 'semantic_workflow'
      AND COALESCE(a.sop_model_accepted, 0) = 0
      AND a.persist_status = 'rejected'
      AND NOT EXISTS (
            SELECT 1 FROM bake_sops s WHERE s.timeline_id = a.timeline_id
      )
);

UPDATE timelines
SET updated_at = CURRENT_TIMESTAMP,
    updated_at_ms = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE id IN (
    SELECT a.timeline_id
    FROM bake_candidate_audits a
    JOIN (
        SELECT timeline_id, MAX(created_at_ms) AS max_created_at_ms
        FROM bake_candidate_audits
        GROUP BY timeline_id
    ) latest
      ON latest.timeline_id = a.timeline_id
     AND latest.max_created_at_ms = a.created_at_ms
    WHERE a.created_at_ms >=
              (CAST(strftime('%s', 'now', '-2 days') AS INTEGER) * 1000)
      AND a.sop_eligible = 1
      AND a.sop_evidence_mode = 'semantic_workflow'
      AND COALESCE(a.sop_model_accepted, 0) = 0
      AND a.persist_status = 'rejected'
      AND NOT EXISTS (
            SELECT 1 FROM bake_sops s WHERE s.timeline_id = a.timeline_id
      )
);
