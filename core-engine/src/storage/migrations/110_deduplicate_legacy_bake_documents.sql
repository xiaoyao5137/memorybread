-- 110: 收敛 062 迁移保留下来的历史同源文档重复项。
--
-- 062 为每个规范化 URL 只保留一个 document_identity，并刻意让历史重复项
-- 保持 NULL。现在运行时已经具备稳定身份、同批 URL 预占和唯一索引，可以安全地
-- 将这些“自动提炼 + NULL identity”的遗留项合入身份主记录。
--
-- 安全边界：
-- - 只处理 llm_bake / auto_created，绝不自动删除手工或审核后的文档；
-- - 必须存在同规范 URL、非空 document_identity 的有效主记录；
-- - 先迁移来源、收藏、Skill 和来源快照，再软删除重复项，保留恢复能力。

DROP TABLE IF EXISTS temp.legacy_document_duplicate_map;
CREATE TEMP TABLE legacy_document_duplicate_map (
    duplicate_id INTEGER PRIMARY KEY,
    survivor_id  INTEGER NOT NULL,
    identity     TEXT    NOT NULL
);

WITH source_urls AS (
    SELECT
        id,
        CASE
            WHEN instr(source_url, '#') > 0
                THEN substr(source_url, 1, instr(source_url, '#') - 1)
            ELSE source_url
        END AS no_fragment
    FROM bake_documents
    WHERE deleted_at IS NULL
      AND document_identity IS NULL
      AND creation_mode = 'llm_bake'
      AND review_status = 'auto_created'
      AND trim(coalesce(source_url, '')) <> ''
      AND (
           lower(source_url) LIKE '%/docs/%'
        OR lower(source_url) LIKE '%docs.google%'
        OR lower(source_url) LIKE '%/document/%'
        OR lower(source_url) LIKE '%yuque.com%'
        OR lower(source_url) LIKE '%feishu.cn/docx%'
        OR lower(source_url) LIKE '%feishu.cn/wiki%'
        OR lower(source_url) LIKE '%larkoffice.com/wiki%'
        OR lower(source_url) LIKE '%notion.so%'
        OR lower(source_url) LIKE '%confluence%'
        OR lower(source_url) LIKE '%/wiki/%'
        OR lower(source_url) LIKE '%shimo.im%'
        OR lower(source_url) LIKE '%/d/home/%'
        OR lower(source_url) LIKE '%/s/home/%'
        OR lower(source_url) LIKE '%/k/home/%'
      )
),
without_query AS (
    SELECT
        id,
        CASE
            WHEN instr(no_fragment, '?') > 0
                THEN substr(no_fragment, 1, instr(no_fragment, '?') - 1)
            ELSE no_fragment
        END AS base_url
    FROM source_urls
),
normalized AS (
    SELECT
        id,
        lower(
            rtrim(
                CASE
                    WHEN lower(base_url) LIKE 'https://%' THEN substr(base_url, 9)
                    WHEN lower(base_url) LIKE 'http://%' THEN substr(base_url, 8)
                    ELSE base_url
                END,
                '/'
            )
        ) AS identity
    FROM without_query
)
INSERT INTO legacy_document_duplicate_map (duplicate_id, survivor_id, identity)
SELECT
    normalized.id,
    survivor.id,
    normalized.identity
FROM normalized
JOIN bake_documents AS survivor
  ON survivor.deleted_at IS NULL
 AND survivor.document_identity = normalized.identity
WHERE normalized.identity <> ''
  AND survivor.id <> normalized.id;

-- 把所有来源 ID 合并到主记录；无效历史 JSON 按空数组处理，不能阻断启动迁移。
UPDATE bake_documents AS survivor
SET source_memory_ids = coalesce((
        SELECT json_group_array(value)
        FROM (
            SELECT DISTINCT CAST(item.value AS TEXT) AS value
            FROM bake_documents AS member
            JOIN json_each(
                CASE WHEN json_valid(member.source_memory_ids)
                     THEN member.source_memory_ids ELSE '[]' END
            ) AS item
            WHERE member.id = survivor.id
               OR member.id IN (
                    SELECT duplicate_id
                    FROM legacy_document_duplicate_map
                    WHERE survivor_id = survivor.id
               )
            ORDER BY CAST(value AS INTEGER), value
        )
    ), '[]'),
    source_capture_ids = coalesce((
        SELECT json_group_array(value)
        FROM (
            SELECT DISTINCT CAST(item.value AS TEXT) AS value
            FROM bake_documents AS member
            JOIN json_each(
                CASE WHEN json_valid(member.source_capture_ids)
                     THEN member.source_capture_ids ELSE '[]' END
            ) AS item
            WHERE member.id = survivor.id
               OR member.id IN (
                    SELECT duplicate_id
                    FROM legacy_document_duplicate_map
                    WHERE survivor_id = survivor.id
               )
            ORDER BY CAST(value AS INTEGER), value
        )
    ), '[]'),
    source_episode_ids = coalesce((
        SELECT json_group_array(value)
        FROM (
            SELECT DISTINCT CAST(item.value AS TEXT) AS value
            FROM bake_documents AS member
            JOIN json_each(
                CASE WHEN json_valid(member.source_episode_ids)
                     THEN member.source_episode_ids ELSE '[]' END
            ) AS item
            WHERE member.id = survivor.id
               OR member.id IN (
                    SELECT duplicate_id
                    FROM legacy_document_duplicate_map
                    WHERE survivor_id = survivor.id
               )
            ORDER BY CAST(value AS INTEGER), value
        )
    ), '[]'),
    linked_knowledge_ids = coalesce((
        SELECT json_group_array(value)
        FROM (
            SELECT DISTINCT CAST(item.value AS TEXT) AS value
            FROM bake_documents AS member
            JOIN json_each(
                CASE WHEN json_valid(member.linked_knowledge_ids)
                     THEN member.linked_knowledge_ids ELSE '[]' END
            ) AS item
            WHERE member.id = survivor.id
               OR member.id IN (
                    SELECT duplicate_id
                    FROM legacy_document_duplicate_map
                    WHERE survivor_id = survivor.id
               )
            ORDER BY CAST(value AS INTEGER), value
        )
    ), '[]'),
    usage_count = usage_count + coalesce((
        SELECT sum(duplicate.usage_count)
        FROM legacy_document_duplicate_map AS mapping
        JOIN bake_documents AS duplicate ON duplicate.id = mapping.duplicate_id
        WHERE mapping.survivor_id = survivor.id
    ), 0),
    updated_at = max(updated_at, coalesce((
        SELECT max(duplicate.updated_at)
        FROM legacy_document_duplicate_map AS mapping
        JOIN bake_documents AS duplicate ON duplicate.id = mapping.duplicate_id
        WHERE mapping.survivor_id = survivor.id
    ), updated_at))
WHERE survivor.id IN (SELECT survivor_id FROM legacy_document_duplicate_map);

-- 来源指纹属于逻辑文档，而不是某次重复提炼产物。
INSERT OR IGNORE INTO bake_document_source_fingerprints (
    document_id, fingerprint, source_timeline_id, created_at
)
SELECT
    mapping.survivor_id,
    fingerprint.fingerprint,
    fingerprint.source_timeline_id,
    fingerprint.created_at
FROM legacy_document_duplicate_map AS mapping
JOIN bake_document_source_fingerprints AS fingerprint
  ON fingerprint.document_id = mapping.duplicate_id;

DELETE FROM bake_document_source_fingerprints
WHERE document_id IN (SELECT duplicate_id FROM legacy_document_duplicate_map);

-- 来源快照去重迁移；相同 content_hash 已存在时保留主记录中的版本。
INSERT OR IGNORE INTO bake_document_source_snapshots (
    document_id, source_url, page_title, content_text, content_hash,
    completeness_status, identity_match, reached_end, stable_passes,
    segment_count, character_count, truncated, collector, collected_at
)
SELECT
    mapping.survivor_id,
    snapshot.source_url,
    snapshot.page_title,
    snapshot.content_text,
    snapshot.content_hash,
    snapshot.completeness_status,
    snapshot.identity_match,
    snapshot.reached_end,
    snapshot.stable_passes,
    snapshot.segment_count,
    snapshot.character_count,
    snapshot.truncated,
    snapshot.collector,
    snapshot.collected_at
FROM legacy_document_duplicate_map AS mapping
JOIN bake_document_source_snapshots AS snapshot
  ON snapshot.document_id = mapping.duplicate_id;

DELETE FROM bake_document_source_snapshots
WHERE document_id IN (SELECT duplicate_id FROM legacy_document_duplicate_map);

-- 收藏指向主记录；若主记录已收藏，保留最早收藏时间和最新更新时间。
INSERT OR REPLACE INTO memory_favorites (
    resource_kind, resource_id, created_at, updated_at
)
SELECT
    'document',
    mapping.survivor_id,
    min(favorite.created_at),
    max(favorite.updated_at)
FROM legacy_document_duplicate_map AS mapping
JOIN memory_favorites AS favorite
  ON favorite.resource_kind = 'document'
 AND (favorite.resource_id = mapping.duplicate_id
      OR favorite.resource_id = mapping.survivor_id)
GROUP BY mapping.survivor_id;

DELETE FROM memory_favorites
WHERE resource_kind = 'document'
  AND resource_id IN (SELECT duplicate_id FROM legacy_document_duplicate_map);

-- 已从重复文档提炼出的创作 Skill 继续保留，只修正来源指向。
UPDATE creation_skills
SET source_id = CAST((
        SELECT survivor_id
        FROM legacy_document_duplicate_map
        WHERE duplicate_id = CAST(creation_skills.source_id AS INTEGER)
    ) AS TEXT)
WHERE source_kind = 'bake_document'
  AND deleted_at IS NULL
  AND CAST(source_id AS INTEGER) IN (
      SELECT duplicate_id FROM legacy_document_duplicate_map
  );

-- 软删除会触发现有向量清理 outbox；正文仍在 SQLite 中，可按 deleted_at 恢复。
UPDATE bake_documents
SET deleted_at = CAST(strftime('%s', 'now') * 1000 AS INTEGER),
    updated_at = max(updated_at, CAST(strftime('%s', 'now') * 1000 AS INTEGER))
WHERE id IN (SELECT duplicate_id FROM legacy_document_duplicate_map);

DROP TABLE legacy_document_duplicate_map;
