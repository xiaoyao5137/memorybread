-- 062: 为文档产物增加稳定身份和来源内容指纹。
--
-- 历史库已经存在重复 URL。迁移只给每组最早的一条有效记录保留 identity，
-- 其余记录保持 NULL，避免迁移时隐式删除或错误合并用户数据。

UPDATE bake_documents
SET document_identity = NULL
WHERE deleted_at IS NULL;

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
),
survivors AS (
    SELECT MIN(id) AS id, identity
    FROM normalized
    WHERE identity <> ''
    GROUP BY identity
)
UPDATE bake_documents
SET document_identity = (
    SELECT identity
    FROM survivors
    WHERE survivors.id = bake_documents.id
)
WHERE id IN (SELECT id FROM survivors);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bake_documents_active_identity
ON bake_documents(document_identity)
WHERE deleted_at IS NULL AND document_identity IS NOT NULL;

CREATE TABLE IF NOT EXISTS bake_document_source_fingerprints (
    document_id        INTEGER NOT NULL
                       REFERENCES bake_documents(id) ON DELETE CASCADE,
    fingerprint        TEXT    NOT NULL,
    source_timeline_id INTEGER,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (document_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_bake_document_source_fingerprints_timeline
ON bake_document_source_fingerprints(source_timeline_id);

-- knowledge / SOP 没有 URL 身份，使用确定性的来源正文指纹阻止相同 capture
-- 在不同 timeline 中重复落库；source_links 保留多来源关系而不复制产物。
CREATE TABLE IF NOT EXISTS bake_artifact_source_fingerprints (
    artifact_kind      TEXT    NOT NULL
                               CHECK (artifact_kind IN ('knowledge', 'sop')),
    fingerprint        TEXT    NOT NULL,
    artifact_id        INTEGER NOT NULL,
    first_timeline_id  INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (artifact_kind, fingerprint)
);

CREATE TABLE IF NOT EXISTS bake_artifact_source_links (
    artifact_kind     TEXT    NOT NULL
                              CHECK (artifact_kind IN ('knowledge', 'sop')),
    artifact_id       INTEGER NOT NULL,
    source_timeline_id INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    PRIMARY KEY (artifact_kind, source_timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_bake_artifact_source_links_artifact
ON bake_artifact_source_links(artifact_kind, artifact_id);

INSERT OR IGNORE INTO bake_artifact_source_links (
    artifact_kind, artifact_id, source_timeline_id, created_at
)
SELECT 'knowledge', id, timeline_id, coalesce(created_at_ms, 0)
FROM bake_knowledge;

INSERT OR IGNORE INTO bake_artifact_source_links (
    artifact_kind, artifact_id, source_timeline_id, created_at
)
SELECT 'sop', id, timeline_id, coalesce(created_at_ms, 0)
FROM bake_sops;
