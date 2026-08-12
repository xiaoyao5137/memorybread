-- 历史版本把文档 activity/content_origin/evidence 完全交给 LLM；
-- 模型漏字段时，真实文档会被 bake 高价值门槛跳过。
-- 仅回填“文档 URL + 至少 200 字可见正文”的缺失记录，并通过 updated_at_ms 重入队。
BEGIN;

CREATE TEMP TABLE _document_timeline_backfill_ids (
    timeline_id INTEGER PRIMARY KEY
);

INSERT INTO _document_timeline_backfill_ids (timeline_id)
SELECT t.id
FROM timelines t
WHERE t.activity_type IS NULL
  AND t.content_origin IS NULL
  AND t.evidence_strength IS NULL
  AND EXISTS (
      SELECT 1
      FROM captures c
      WHERE c.timeline_id = t.id
        AND (
            LOWER(COALESCE(c.url, '')) LIKE '%/k/home/%'
        )
        AND LENGTH(
            REPLACE(REPLACE(
                COALESCE(c.ax_text, '') || COALESCE(c.ocr_text, ''),
                ' ', ''
            ), char(10), '')
        ) >= 200
  );

UPDATE timelines
SET category = '文档',
    activity_type = 'reading',
    content_origin = 'document_reference',
    evidence_strength = 'medium',
    updated_at = CURRENT_TIMESTAMP,
    updated_at_ms = CAST(strftime('%s', 'now') * 1000 AS INTEGER)
WHERE id IN (SELECT timeline_id FROM _document_timeline_backfill_ids);

-- 旧的失败状态可能来自缺失元数据或旧 schema；回填后允许按新规则重新尝试。
DELETE FROM bake_retry_state
WHERE timeline_id IN (SELECT timeline_id FROM _document_timeline_backfill_ids);

DROP TABLE _document_timeline_backfill_ids;

INSERT OR IGNORE INTO schema_migrations (version, applied_at)
VALUES (
    '053_backfill_document_timeline_metadata',
    CAST(strftime('%s', 'now') * 1000 AS INTEGER)
);

COMMIT;
