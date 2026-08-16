use rusqlite::{params, Connection};

use crate::storage::{
    db::current_ts_ms,
    document_identity::{canonical_document_identity, canonical_document_title_identity},
    error::StorageError,
    fts::{
        build_fts_or_query, fts_candidate_ids, render_in_clause, split_query_terms,
        DEFAULT_FTS_CANDIDATE_CAP,
    },
    models_bake::{BakeDocumentRecord, NewBakeDocument},
    StorageManager,
};

const SELECT_COLUMNS: &str =
    "id, title, doc_type, status, tags, applicable_tasks, source_memory_ids,
     source_capture_ids, source_episode_ids, linked_knowledge_ids,
     sections_json, style_phrases, replacement_rules,
     summary, full_content, structured_content, prompt_hint,
     diagram_code, image_assets,
     source_app_name, source_win_title, source_url, content_hash, language,
     usage_count, match_score, match_level, creation_mode, review_status,
     evidence_summary, generation_version, deleted_at,
     created_at, updated_at";

impl StorageManager {
    pub fn insert_bake_document(&self, doc: &NewBakeDocument) -> Result<i64, StorageError> {
        self.with_conn(|conn| insert_bake_document_inner(conn, doc))
    }

    pub fn get_bake_document(&self, id: i64) -> Result<Option<BakeDocumentRecord>, StorageError> {
        self.with_conn(|conn| {
            let sql = format!(
                "SELECT {} FROM bake_documents WHERE id = ?1",
                SELECT_COLUMNS
            );
            let mut stmt = conn.prepare(&sql)?;
            let mut rows = stmt.query(params![id])?;
            if let Some(row) = rows.next()? {
                Ok(Some(row_to_bake_document(row)?))
            } else {
                Ok(None)
            }
        })
    }

    pub fn list_bake_documents_paginated(
        &self,
        query: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<BakeDocumentRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut sql = format!(
                "SELECT {} FROM bake_documents WHERE deleted_at IS NULL",
                SELECT_COLUMNS
            );
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
            if let Some(q) = query {
                sql.push_str(
                    " AND (title LIKE ? OR doc_type LIKE ? OR COALESCE(prompt_hint, '') LIKE ? OR COALESCE(summary, '') LIKE ?)",
                );
                let pattern = format!("%{}%", q);
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern));
                // FTS5 预筛：bake_documents_fts 候选可用时收窄扫描，否则回退 LIKE 全扫
                append_document_fts_prefilter(conn, &mut sql, &mut bind_values, q);
            }
            sql.push_str(" ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?");
            bind_values.push(Box::new(limit as i64));
            bind_values.push(Box::new(offset as i64));

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> =
                bind_values.iter().map(|b| b.as_ref()).collect();
            let rows = stmt.query_map(params.as_slice(), |row| {
                Ok(row_to_bake_document(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn count_bake_documents_filtered(&self, query: Option<&str>) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let mut sql =
                String::from("SELECT COUNT(*) FROM bake_documents WHERE deleted_at IS NULL");
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
            if let Some(q) = query {
                sql.push_str(
                    " AND (title LIKE ? OR doc_type LIKE ? OR COALESCE(prompt_hint, '') LIKE ? OR COALESCE(summary, '') LIKE ?)",
                );
                let pattern = format!("%{}%", q);
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern));
                // FTS5 预筛（与列表查询保持一致的候选收窄）
                append_document_fts_prefilter(conn, &mut sql, &mut bind_values, q);
            }
            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> =
                bind_values.iter().map(|b| b.as_ref()).collect();
            let count: i64 = stmt.query_row(params.as_slice(), |row| row.get(0))?;
            Ok(count)
        })
    }

    pub fn list_bake_documents(&self) -> Result<Vec<BakeDocumentRecord>, StorageError> {
        self.with_conn(|conn| {
            let sql = format!(
                "SELECT {} FROM bake_documents
                 WHERE deleted_at IS NULL
                 ORDER BY updated_at DESC, id DESC",
                SELECT_COLUMNS
            );
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map([], |row| {
                Ok(row_to_bake_document(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn find_bake_document_by_source_memory_id(
        &self,
        memory_id: i64,
    ) -> Result<Option<BakeDocumentRecord>, StorageError> {
        let memory_id = memory_id.to_string();
        self.with_conn(|conn| {
            let sql = format!(
                "SELECT {} FROM bake_documents
                 WHERE deleted_at IS NULL
                   AND (
                     source_memory_ids = ?1
                     OR source_memory_ids LIKE ?2
                     OR source_memory_ids LIKE ?3
                     OR source_memory_ids LIKE ?4
                   )
                 ORDER BY updated_at DESC, id DESC
                 LIMIT 1",
                SELECT_COLUMNS
            );
            let exact = format!("[\"{}\"]", memory_id);
            let start = format!("[\"{}\",%", memory_id);
            let middle = format!("%,\"{}\",%", memory_id);
            let end = format!("%,\"{}\"]", memory_id);
            let mut stmt = conn.prepare(&sql)?;
            let mut rows = stmt.query(params![exact, start, middle, end])?;
            if let Some(row) = rows.next()? {
                Ok(Some(row_to_bake_document(row)?))
            } else {
                Ok(None)
            }
        })
    }

    pub fn find_bake_document_by_source_title(
        &self,
        source_title: &str,
    ) -> Result<Option<BakeDocumentRecord>, StorageError> {
        let Some(source_identity) = canonical_document_title_identity(source_title) else {
            return Ok(None);
        };
        let document_id = self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, source_win_title
                 FROM bake_documents
                 WHERE deleted_at IS NULL
                   AND trim(coalesce(source_win_title, '')) <> ''
                 ORDER BY updated_at DESC, id DESC",
            )?;
            let rows = stmt.query_map([], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?))
            })?;
            for row in rows {
                let (id, source_win_title) = row?;
                if source_win_title
                    .as_deref()
                    .and_then(canonical_document_title_identity)
                    .as_deref()
                    == Some(source_identity.as_str())
                {
                    return Ok(Some(id));
                }
            }
            Ok(None)
        })?;
        document_id.map_or(Ok(None), |id| self.get_bake_document(id))
    }

    pub fn find_document_by_source_url(
        &self,
        url: &str,
    ) -> Result<Option<BakeDocumentRecord>, StorageError> {
        let Some(identity) = canonical_document_identity(url) else {
            return Ok(None);
        };
        let document_id = self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, source_url, document_identity
                 FROM bake_documents
                 WHERE deleted_at IS NULL
                   AND (document_identity = ?1 OR document_identity IS NULL)
                   AND source_url IS NOT NULL
                 ORDER BY CASE WHEN document_identity = ?1 THEN 0 ELSE 1 END,
                          updated_at DESC,
                          id DESC",
            )?;
            let rows = stmt.query_map(params![identity], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            })?;
            for row in rows {
                let (id, source_url, stored_identity) = row?;
                let matches = stored_identity.as_deref() == Some(identity.as_str())
                    || canonical_document_identity(&source_url).as_deref()
                        == Some(identity.as_str());
                if matches {
                    return Ok(Some(id));
                }
            }
            Ok(None)
        })?;
        document_id.map_or(Ok(None), |id| self.get_bake_document(id))
    }

    /// 按源文本指纹查找未删除的烘焙文档，供 bake 指纹预筛使用。
    pub fn find_bake_document_id_by_source_fingerprint(
        &self,
        fingerprint: &str,
    ) -> Result<Option<i64>, StorageError> {
        self.with_conn(|conn| {
            match conn.query_row(
                "SELECT fd.document_id
                 FROM bake_document_source_fingerprints fd
                 JOIN bake_documents d ON d.id = fd.document_id
                 WHERE fd.fingerprint = ?1 AND d.deleted_at IS NULL
                 LIMIT 1",
                params![fingerprint],
                |row| row.get(0),
            ) {
                Ok(id) => Ok(Some(id)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(error) => Err(StorageError::Sqlite(error)),
            }
        })
    }

    pub fn has_bake_document_source_fingerprint(
        &self,
        document_id: i64,
        fingerprint: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT EXISTS(
                    SELECT 1
                    FROM bake_document_source_fingerprints
                    WHERE document_id = ?1 AND fingerprint = ?2
                 )",
                params![document_id, fingerprint],
                |row| row.get(0),
            )
            .map_err(StorageError::Sqlite)
        })
    }

    pub fn record_bake_document_source_fingerprint(
        &self,
        document_id: i64,
        fingerprint: &str,
        source_timeline_id: i64,
    ) -> Result<bool, StorageError> {
        let created_at = current_ts_ms();
        self.with_conn(|conn| {
            let affected = conn.execute(
                "INSERT OR IGNORE INTO bake_document_source_fingerprints (
                    document_id, fingerprint, source_timeline_id, created_at
                 ) VALUES (?1, ?2, ?3, ?4)",
                params![document_id, fingerprint, source_timeline_id, created_at],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn update_bake_document(
        &self,
        id: i64,
        doc: &NewBakeDocument,
    ) -> Result<bool, StorageError> {
        let updated_at = current_ts_ms();
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_documents
                 SET title = ?1, doc_type = ?2, status = ?3, tags = ?4, applicable_tasks = ?5,
                     source_memory_ids = ?6, source_capture_ids = ?7, source_episode_ids = ?8,
                     linked_knowledge_ids = ?9, sections_json = ?10,
                     style_phrases = ?11, replacement_rules = ?12,
                     summary = ?13, full_content = ?14, structured_content = ?15,
                     prompt_hint = ?16, diagram_code = ?17, image_assets = ?18,
                     source_app_name = ?19, source_win_title = ?20, source_url = ?21,
                     document_identity = ?22, content_hash = ?23, language = ?24,
                     usage_count = ?25, match_score = ?26, match_level = ?27,
                     creation_mode = ?28, review_status = ?29, evidence_summary = ?30,
                     generation_version = ?31, deleted_at = ?32, updated_at = ?33
                 WHERE id = ?34",
                params![
                    doc.title,
                    doc.doc_type,
                    doc.status,
                    doc.tags,
                    doc.applicable_tasks,
                    doc.source_memory_ids,
                    doc.source_capture_ids,
                    doc.source_episode_ids,
                    doc.linked_knowledge_ids,
                    doc.sections_json,
                    doc.style_phrases,
                    doc.replacement_rules,
                    doc.summary,
                    doc.full_content,
                    doc.structured_content,
                    doc.prompt_hint,
                    doc.diagram_code,
                    doc.image_assets,
                    doc.source_app_name,
                    doc.source_win_title,
                    doc.source_url,
                    doc.source_url
                        .as_deref()
                        .and_then(canonical_document_identity),
                    doc.content_hash,
                    doc.language,
                    doc.usage_count,
                    doc.match_score,
                    doc.match_level,
                    doc.creation_mode,
                    doc.review_status,
                    doc.evidence_summary,
                    doc.generation_version,
                    doc.deleted_at,
                    updated_at,
                    id,
                ],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn toggle_bake_document_status(
        &self,
        id: i64,
    ) -> Result<Option<BakeDocumentRecord>, StorageError> {
        let maybe_doc = self.get_bake_document(id)?;
        let Some(doc) = maybe_doc else {
            return Ok(None);
        };

        let next_status = if doc.status == "enabled" {
            "disabled"
        } else {
            "enabled"
        };
        let updated_at = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_documents SET status = ?1, updated_at = ?2 WHERE id = ?3",
                params![next_status, updated_at, id],
            )?;
            Ok(())
        })?;

        self.get_bake_document(id)
    }

    pub fn soft_delete_bake_document(&self, id: i64) -> Result<bool, StorageError> {
        let deleted_at = current_ts_ms();
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_documents SET deleted_at = ?1, updated_at = ?1 WHERE id = ?2 AND deleted_at IS NULL",
                params![deleted_at, id],
            )?;
            if affected > 0 {
                StorageManager::delete_memory_favorite_with_conn(conn, "document", id)?;
            }
            Ok(affected > 0)
        })
    }
}

fn insert_bake_document_inner(
    conn: &Connection,
    doc: &NewBakeDocument,
) -> Result<i64, StorageError> {
    let now = current_ts_ms();
    conn.execute(
        "INSERT INTO bake_documents (
            title, doc_type, status, tags, applicable_tasks, source_memory_ids,
            source_capture_ids, source_episode_ids, linked_knowledge_ids,
            sections_json, style_phrases, replacement_rules,
            summary, full_content, structured_content, prompt_hint,
            diagram_code, image_assets,
            source_app_name, source_win_title, source_url, content_hash, language,
            document_identity,
            usage_count, match_score, match_level, creation_mode, review_status,
            evidence_summary, generation_version, deleted_at, created_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16,
                   ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26, ?27, ?28, ?29, ?30, ?31, ?32, ?33, ?34)",
        params![
            doc.title,
            doc.doc_type,
            doc.status,
            doc.tags,
            doc.applicable_tasks,
            doc.source_memory_ids,
            doc.source_capture_ids,
            doc.source_episode_ids,
            doc.linked_knowledge_ids,
            doc.sections_json,
            doc.style_phrases,
            doc.replacement_rules,
            doc.summary,
            doc.full_content,
            doc.structured_content,
            doc.prompt_hint,
            doc.diagram_code,
            doc.image_assets,
            doc.source_app_name,
            doc.source_win_title,
            doc.source_url,
            doc.content_hash,
            doc.language,
            doc.source_url
                .as_deref()
                .and_then(canonical_document_identity),
            doc.usage_count,
            doc.match_score,
            doc.match_level,
            doc.creation_mode,
            doc.review_status,
            doc.evidence_summary,
            doc.generation_version,
            doc.deleted_at,
            now,
            now,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

/// FTS5 预筛：bake_documents_fts 候选可用时追加 `id IN (...)` 收窄扫描；
/// FTS 表缺失、查询失败、候选为空或被上限截断时不做任何修改，
/// 调用方保留原有 LIKE 子句回退全量扫描。
fn append_document_fts_prefilter(
    conn: &Connection,
    sql: &mut String,
    bind_values: &mut Vec<Box<dyn rusqlite::ToSql>>,
    query: &str,
) {
    let terms = split_query_terms(query);
    let Some(fts_query) = build_fts_or_query(&terms) else {
        return;
    };
    let Some(ids) = fts_candidate_ids(
        conn,
        "bake_documents_fts",
        &fts_query,
        DEFAULT_FTS_CANDIDATE_CAP,
    ) else {
        return;
    };
    let (clause, mut id_binds) = render_in_clause(&ids);
    sql.push_str(" AND id IN ");
    sql.push_str(&clause);
    bind_values.append(&mut id_binds);
}

fn row_to_bake_document(row: &rusqlite::Row<'_>) -> Result<BakeDocumentRecord, StorageError> {
    Ok(BakeDocumentRecord {
        id: row.get(0)?,
        title: row.get(1)?,
        doc_type: row.get(2)?,
        status: row.get(3)?,
        tags: row.get(4)?,
        applicable_tasks: row.get(5)?,
        source_memory_ids: row.get(6)?,
        source_capture_ids: row.get(7)?,
        source_episode_ids: row.get(8)?,
        linked_knowledge_ids: row.get(9)?,
        sections_json: row.get(10)?,
        style_phrases: row.get(11)?,
        replacement_rules: row.get(12)?,
        summary: row.get(13)?,
        full_content: row.get(14)?,
        structured_content: row.get(15)?,
        prompt_hint: row.get(16)?,
        diagram_code: row.get(17)?,
        image_assets: row.get(18)?,
        source_app_name: row.get(19)?,
        source_win_title: row.get(20)?,
        source_url: row.get(21)?,
        content_hash: row.get(22)?,
        language: row.get(23)?,
        usage_count: row.get(24)?,
        match_score: row.get(25)?,
        match_level: row.get(26)?,
        creation_mode: row.get(27)?,
        review_status: row.get(28)?,
        evidence_summary: row.get(29)?,
        generation_version: row.get(30)?,
        deleted_at: row.get(31)?,
        created_at: row.get(32)?,
        updated_at: row.get(33)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_mgr() -> StorageManager {
        StorageManager::open_in_memory().expect("内存数据库初始化失败")
    }

    fn sample_document() -> NewBakeDocument {
        NewBakeDocument {
            title: "技术方案结构版".to_string(),
            doc_type: "技术方案".to_string(),
            status: "draft".to_string(),
            tags: r#"["方案"]"#.to_string(),
            applicable_tasks: r#"["creation"]"#.to_string(),
            source_memory_ids: r#"["1"]"#.to_string(),
            source_capture_ids: r#"["11"]"#.to_string(),
            source_episode_ids: r#"["ep-1"]"#.to_string(),
            linked_knowledge_ids: r#"["1","2"]"#.to_string(),
            sections_json: r#"[{"title":"背景","keywords":["现状"]}]"#.to_string(),
            style_phrases: r#"["整体看"]"#.to_string(),
            replacement_rules: r#"[{"from":"综上","to":"整体看"}]"#.to_string(),
            summary: Some("技术方案模板，覆盖背景/方案/落地。".to_string()),
            full_content: Some("## 模板价值\n用于技术方案写作。".to_string()),
            structured_content: "{}".to_string(),
            prompt_hint: Some("优先输出结构化方案".to_string()),
            diagram_code: None,
            image_assets: "[]".to_string(),
            source_app_name: None,
            source_win_title: None,
            source_url: None,
            content_hash: None,
            language: None,
            usage_count: 0,
            match_score: Some(0.82),
            match_level: Some("high".to_string()),
            creation_mode: "auto".to_string(),
            review_status: "auto_created".to_string(),
            evidence_summary: Some("多次出现稳定结构".to_string()),
            generation_version: Some("bake-v1".to_string()),
            deleted_at: None,
        }
    }

    #[test]
    fn test_insert_and_get_bake_document() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();
        let doc = mgr.get_bake_document(id).unwrap().unwrap();
        assert_eq!(doc.title, "技术方案结构版");
        assert_eq!(doc.doc_type, "技术方案");
        assert_eq!(doc.creation_mode, "auto");
        assert_eq!(doc.review_status, "auto_created");
    }

    #[test]
    fn test_update_bake_document() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();
        let mut updated = sample_document();
        updated.title = "周报模板".to_string();
        updated.status = "enabled".to_string();
        updated.review_status = "accepted".to_string();
        assert!(mgr.update_bake_document(id, &updated).unwrap());
        let doc = mgr.get_bake_document(id).unwrap().unwrap();
        assert_eq!(doc.title, "周报模板");
        assert_eq!(doc.status, "enabled");
        assert_eq!(doc.review_status, "accepted");
    }

    #[test]
    fn test_find_bake_document_by_source_title() {
        let mgr = make_mgr();
        let mut document = sample_document();
        document.source_win_title = Some("商业体系-AI建设资产复用方案 - 云文档".to_string());
        let id = mgr.insert_bake_document(&document).unwrap();

        let found = mgr
            .find_bake_document_by_source_title("商业体系-AI 建设资产复用方案（云文档）")
            .unwrap()
            .unwrap();

        assert_eq!(found.id, id);
        assert!(mgr
            .find_bake_document_by_source_title("另一份方案.docx")
            .unwrap()
            .is_none());
    }

    #[test]
    fn test_source_title_lookup_does_not_trust_generated_title_without_source_evidence() {
        let mgr = make_mgr();
        let mut document = sample_document();
        document.title = "商业体系-AI建设资产复用方案".to_string();
        document.source_app_name = Some("Kim".to_string());
        document.source_win_title = None;
        mgr.insert_bake_document(&document).unwrap();

        assert!(mgr
            .find_bake_document_by_source_title("商业体系-AI 建设资产复用方案")
            .unwrap()
            .is_none());
    }

    #[test]
    fn test_find_document_by_source_url_ignores_query_fragment_and_scheme() {
        let mgr = make_mgr();
        let mut document = sample_document();
        document.source_url =
            Some("https://Docs.Example.Com/d/home/ABC123?section=one#comment".to_string());
        let id = mgr.insert_bake_document(&document).unwrap();

        let found = mgr
            .find_document_by_source_url("http://docs.example.com/d/home/abc123?section=two")
            .unwrap()
            .unwrap();

        assert_eq!(found.id, id);
    }

    #[test]
    fn test_active_document_identity_is_unique() {
        let mgr = make_mgr();
        let mut first = sample_document();
        first.source_url = Some("https://docs.example.com/d/home/abc123?section=one".to_string());
        mgr.insert_bake_document(&first).unwrap();

        let mut duplicate = sample_document();
        duplicate.source_url =
            Some("https://docs.example.com/d/home/ABC123#section=two".to_string());
        assert!(mgr.insert_bake_document(&duplicate).is_err());
    }

    #[test]
    fn test_document_source_fingerprint_is_idempotent() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();

        assert!(!mgr
            .has_bake_document_source_fingerprint(id, "sha256:abc")
            .unwrap());
        assert!(mgr
            .record_bake_document_source_fingerprint(id, "sha256:abc", 42)
            .unwrap());
        assert!(!mgr
            .record_bake_document_source_fingerprint(id, "sha256:abc", 43)
            .unwrap());
        assert!(mgr
            .has_bake_document_source_fingerprint(id, "sha256:abc")
            .unwrap());
    }

    #[test]
    fn test_toggle_bake_document_status() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();
        let toggled = mgr.toggle_bake_document_status(id).unwrap().unwrap();
        assert_eq!(toggled.status, "enabled");
    }

    fn seed_unrelated_document(mgr: &StorageManager) -> i64 {
        let mut other = sample_document();
        other.title = "完全无关条目".to_string();
        other.doc_type = "模板".to_string();
        other.summary = Some("与搜索词毫无关系的内容。".to_string());
        other.full_content = Some("另一份正文。".to_string());
        other.prompt_hint = Some("无关提示".to_string());
        other.source_url = Some("https://docs.example.com/unrelated".to_string());
        mgr.insert_bake_document(&other).unwrap()
    }

    #[test]
    fn test_list_bake_documents_query_prefilter_results_match_like() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();
        seed_unrelated_document(&mgr);

        let results = mgr
            .list_bake_documents_paginated(Some("技术方案"), 10, 0)
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, id);
        assert_eq!(
            mgr.count_bake_documents_filtered(Some("技术方案")).unwrap(),
            1
        );
    }

    #[test]
    fn test_list_bake_documents_query_falls_back_without_fts() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();
        seed_unrelated_document(&mgr);

        mgr.with_conn(|conn| {
            conn.execute_batch(
                "DROP TRIGGER IF EXISTS bake_documents_fts_insert;
                 DROP TRIGGER IF EXISTS bake_documents_fts_update;
                 DROP TRIGGER IF EXISTS bake_documents_fts_delete;
                 DROP TABLE IF EXISTS bake_documents_fts;",
            )?;
            Ok(())
        })
        .unwrap();

        let results = mgr
            .list_bake_documents_paginated(Some("技术方案"), 10, 0)
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, id);
        assert_eq!(
            mgr.count_bake_documents_filtered(Some("技术方案")).unwrap(),
            1
        );
    }
}
