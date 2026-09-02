-- 操作证据门禁由二元值升级为三态，并为预算中性的历史候选回放保留本地队列。
-- 回放队列本身不触发模型；调度器只能用它替换既有 bake 批次中的 fresh 配额。

ALTER TABLE bake_candidate_audits
ADD COLUMN sop_eligibility_state TEXT NOT NULL DEFAULT 'rejected';

UPDATE bake_candidate_audits
SET sop_eligibility_state = CASE
    WHEN sop_eligible = 1 THEN 'eligible'
    WHEN sop_eligibility_reason IN (
        'insufficient_source_capture_count',
        'missing_attributed_result',
        'missing_real_action'
    ) THEN 'needs_enrichment'
    ELSE 'rejected'
END;

CREATE INDEX IF NOT EXISTS idx_bake_candidate_audits_sop_state_created
ON bake_candidate_audits(sop_eligibility_state, created_at_ms DESC);

CREATE TABLE IF NOT EXISTS operation_replay_queue (
    timeline_id INTEGER PRIMARY KEY REFERENCES timelines(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    queued_at_ms INTEGER NOT NULL,
    claimed_at_ms INTEGER,
    completed_at_ms INTEGER,
    last_run_id INTEGER REFERENCES bake_runs(id) ON DELETE SET NULL,
    CHECK (status IN ('pending', 'claimed', 'completed', 'discarded'))
);

CREATE INDEX IF NOT EXISTS idx_operation_replay_queue_status_priority
ON operation_replay_queue(status, priority DESC, queued_at_ms ASC);

INSERT OR IGNORE INTO operation_replay_queue (
    timeline_id, reason, priority, queued_at_ms
)
SELECT DISTINCT a.timeline_id,
       'operation_evidence_contract_recovery',
       CASE
           WHEN a.sop_eligibility_reason = 'missing_real_action' THEN 20
           WHEN a.sop_eligibility_reason = 'missing_attributed_result' THEN 15
           ELSE 10
       END,
       CAST(strftime('%s', 'now') * 1000 AS INTEGER)
FROM bake_candidate_audits a
JOIN timelines t ON t.id = a.timeline_id
WHERE a.created_at_ms >= CAST(strftime('%s', 'now', '-14 days') * 1000 AS INTEGER)
  AND a.sop_eligibility_reason IN (
      'missing_real_action',
      'missing_attributed_result',
      'insufficient_source_capture_count'
  )
  AND EXISTS (
      SELECT 1
      FROM captures c
      WHERE c.timeline_id = a.timeline_id
        AND c.event_type IN ('mouse_click', 'key_pause', 'manual')
  )
  -- 新准入契约至少需要动作和后续结果两条独立证据。单帧候选只能保持
  -- needs_enrichment，不能进入会占用 bake 配额的执行队列。
  AND (
      SELECT COUNT(*)
      FROM captures member
      WHERE member.timeline_id = a.timeline_id
  ) >= 2
  AND NOT EXISTS (
      SELECT 1
      FROM bake_sops existing_sop
      WHERE existing_sop.timeline_id = a.timeline_id
  );
