use rusqlite::{params, Connection, OptionalExtension};
use crate::services::document_refresh::{DocumentRefreshConfig, DOCUMENT_REFRESH_CONFIG_KEY};

use crate::storage::{
    db::current_ts_ms,
    document_identity::{canonical_document_identity, canonical_document_title_identity},
    error::StorageError,
    fts::{
        build_fts_or_query, fts_candidate_ids, render_in_clause, split_query_terms,
        DEFAULT_FTS_CANDIDATE_CAP,
    },
    models_bake::{
        BakeDocumentRecord, BakeDocumentSourceSnapshotRecord, NewBakeDocument,
        NewBakeDocumentSourceSnapshot,
    },
    StorageManager,
};

fn require_document_source_writes(conn: &Connection, document_id: i64) -> Result<(), StorageError> {
    let value: Option<String> = conn.query_row(
        "SELECT value FROM user_preferences WHERE key=?1", params![DOCUMENT_REFRESH_CONFIG_KEY],
        |row| row.get(0),
    ).optional()?;
    let enabled = value.as_deref().map(DocumentRefreshConfig::parse)
        .transpose().map(|config| config.unwrap_or_default().permits_source_write(document_id))
        .unwrap_or(false);
    if enabled { Ok(()) } else { Err(StorageError::DocumentSourceWritesPaused) }
}

fn require_automatic_document_writes(conn: &Connection, title: &str, url: Option<&str>) -> Result<(), StorageError> {
    let value: Option<String> = conn.query_row(
        "SELECT value FROM user_preferences WHERE key=?1",params![DOCUMENT_REFRESH_CONFIG_KEY],|row|row.get(0),
    ).optional()?;
    let identity = url.and_then(canonical_document_identity)
        .or_else(|| canonical_document_title_identity(title)).unwrap_or_default();
    let enabled = value.as_deref().map(DocumentRefreshConfig::parse).transpose()
        .map(|config|config.unwrap_or_default().permits_automatic_document_write(&identity)).unwrap_or(false);
    if enabled { Ok(()) } else { Err(StorageError::DocumentAutomaticWritesPaused {
        bucket: crate::services::document_refresh::automatic_document_rollout_bucket(&identity),
    }) }
}

const SELECT_COLUMNS: &str =
    "id, title, doc_type, status, tags, applicable_tasks, source_memory_ids,
     source_capture_ids, source_episode_ids, linked_knowledge_ids,
     sections_json, style_phrases, replacement_rules,
     summary, full_content, structured_content, prompt_hint,
     diagram_code, image_assets,
     source_app_name, source_win_title, source_url, content_hash, language,
     usage_count, match_score, match_level, creation_mode, review_status,
     evidence_summary, generation_version,
     refresh_policy, last_refresh_checked_at_ms, last_refresh_error,
     last_refresh_success_at_ms, last_refresh_status, last_refresh_completeness,
     last_refresh_content_hash, last_refresh_character_count,
     last_refresh_segment_count, last_refresh_truncated, deleted_at,
     created_at, updated_at";

impl StorageManager {
    /// Publish only the summary generated from the exact document revision read.
    /// Callers must carry the original source ID and updated_at through inference.
    pub fn publish_document_source_summary(
        &self, document_id: i64, source_snapshot_id: i64,
        expected_updated_at: i64, summary: &str,
    ) -> Result<bool, StorageError> {
        self.publish_document_source_summary_guarded(document_id,source_snapshot_id,expected_updated_at,summary,None)
    }

    pub fn publish_document_summary_job(
        &self, job: &super::document_summary_jobs::DocumentSummaryJob, summary: &str,
    ) -> Result<bool, StorageError> {
        self.publish_document_source_summary_guarded(job.document_id,job.source_snapshot_id,
            job.expected_updated_at,summary,Some(&job.lease_id))
    }

    fn publish_document_source_summary_guarded(
        &self, document_id:i64,source_snapshot_id:i64,expected_updated_at:i64,
        summary:&str,lease_id:Option<&str>,
    ) -> Result<bool,StorageError> {
        if summary.trim().is_empty() { return Ok(false); }
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            if let Some(lease) = lease_id {
                let capture_enabled:Option<String>=tx.query_row("SELECT value FROM user_preferences WHERE key='runtime.capture_enabled'",
                    [],|r|r.get(0)).optional()?;
                if capture_enabled.as_deref().is_some_and(|v|v.eq_ignore_ascii_case("false")) {
                    return Err(StorageError::DocumentSourceWritesPaused);
                }
                let owns:bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM document_summary_jobs
                    WHERE document_id=?1 AND source_snapshot_id=?2 AND expected_updated_at=?3
                    AND lease_id=?4 AND state='running' AND lease_until>?5)",
                    params![document_id,source_snapshot_id,expected_updated_at,lease,current_ts_ms()],|r|r.get(0))?;
                if !owns { return Ok(false); }
            }
            require_document_source_writes(&tx,document_id)?;
            let source:Option<(String,Option<String>)> = tx.query_row(
                "SELECT title,source_url FROM bake_documents WHERE id=?1 AND deleted_at IS NULL",
                params![document_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            let Some((title,url)) = source else { return Ok(false); };
            require_automatic_document_writes(&tx,&title,url.as_deref())?;
            // Monotonic revision even if two edits occur within one millisecond.
            let updated_at = current_ts_ms().max(expected_updated_at.saturating_add(1));
            let changed = tx.execute("UPDATE bake_documents SET summary=?4,
                summary_source_snapshot_id=?2,summary_generation_version='document-summary.v1',updated_at=?5
                WHERE id=?1 AND updated_at=?3 AND deleted_at IS NULL AND summary IS NULL
                AND EXISTS(SELECT 1 FROM bake_document_source_heads h
                    JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id
                    WHERE h.document_id=bake_documents.id AND s.id=?2
                    AND s.document_id=bake_documents.id AND s.identity_match=1
                    AND s.completeness_status='complete' AND s.content_text=bake_documents.full_content)",
                params![document_id,source_snapshot_id,expected_updated_at,summary.trim(),updated_at])?;
            if changed > 0 {
                tx.execute("INSERT OR IGNORE INTO vector_deletion_queue
                    (qdrant_point_id,source_type,reason,enqueued_at)
                    SELECT qdrant_point_id,'document','document_summary_rebuilt',?2
                    FROM artifact_vector_index WHERE document_id=?1",params![document_id,updated_at])?;
                tx.execute("DELETE FROM artifact_vector_index WHERE document_id=?1",params![document_id])?;
                if let Some(lease) = lease_id {
                    tx.execute("UPDATE document_summary_jobs SET state='completed',lease_id=NULL,lease_until=0,last_error=NULL
                        WHERE document_id=?1 AND source_snapshot_id=?2 AND expected_updated_at=?3 AND lease_id=?4",
                        params![document_id,source_snapshot_id,expected_updated_at,lease])?;
                }
            } else if lease_id.is_none() {
                // Leased failures are audited by the owning job's finish path.
                // An already populated summary on the same revision is not a
                // source mismatch; only a changed/invalid source is counted.
                let source_current:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM bake_documents d
                    JOIN bake_document_source_heads h ON h.document_id=d.id
                    JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id AND s.document_id=d.id
                    WHERE d.id=?1 AND d.updated_at=?2 AND h.snapshot_id=?3 AND d.deleted_at IS NULL
                    AND s.identity_match=1 AND s.completeness_status='complete' AND s.content_text=d.full_content)",
                    params![document_id,expected_updated_at,source_snapshot_id],|r|r.get(0))?;
                if !source_current && tx.execute("INSERT INTO document_source_mismatch_events
                    (document_id,component,reason,expected_snapshot_id,observed_snapshot_id,occurrences,observed_at)
                    VALUES(?1,'summary_write','summary_version_mismatch',?2,
                        (SELECT snapshot_id FROM bake_document_source_heads WHERE document_id=?1),1,?3)",
                    params![document_id,source_snapshot_id,current_ts_ms()]).is_err() {
                    tracing::warn!("document_summary_mismatch_audit_write_failed");
                }
            }
            tx.commit()?;
            Ok(changed > 0)
        })
    }

    /// Source associations cannot overwrite a body changed by a refresh.
    pub fn update_document_source_links(&self, id: i64, doc: &NewBakeDocument) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute("UPDATE bake_documents SET
                source_memory_ids=(SELECT json_group_array(value) FROM (SELECT value FROM json_each(source_memory_ids) UNION SELECT value FROM json_each(?2))),
                source_capture_ids=(SELECT json_group_array(value) FROM (SELECT value FROM json_each(source_capture_ids) UNION SELECT value FROM json_each(?3))),
                source_episode_ids=(SELECT json_group_array(value) FROM (SELECT value FROM json_each(source_episode_ids) UNION SELECT value FROM json_each(?4))),
                linked_knowledge_ids=(SELECT json_group_array(value) FROM (SELECT value FROM json_each(linked_knowledge_ids) UNION SELECT value FROM json_each(?5))) WHERE id=?1",
                params![id,doc.source_memory_ids,doc.source_capture_ids,doc.source_episode_ids,doc.linked_knowledge_ids])?;
            Ok(())
        })
    }
    pub fn insert_bake_document(&self, doc: &NewBakeDocument) -> Result<i64, StorageError> {
        self.with_conn(|conn| insert_bake_document_inner(conn, doc))
    }

    pub fn insert_bake_document_from_observation(&self, doc: &NewBakeDocument) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            require_automatic_document_writes(&tx,&doc.title,doc.source_url.as_deref())?;
            let id=insert_bake_document_inner(&tx,doc)?;
            tx.commit()?;
            Ok(id)
        })
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
            sql.push_str(" ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?");
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
                 ORDER BY created_at DESC, id DESC",
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

    /// 按观察时间升序返回文档的来源正文指纹，供刷新资格判定
    /// 统计“原地更新证据”与更新节奏。
    pub fn list_bake_document_source_fingerprints(
        &self,
        document_id: i64,
    ) -> Result<Vec<(String, i64)>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT fingerprint, created_at
                 FROM bake_document_source_fingerprints
                 WHERE document_id = ?1
                 ORDER BY created_at ASC, rowid ASC",
            )?;
            let rows = stmt.query_map(params![document_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    /// 刷新状态是窄字段更新，不走全列 UPDATE，避免把刷新元数据
    /// 混入内容合并的写路径。last_error 传 None 表示清除历史错误。
    pub fn touch_document_refresh_state(
        &self,
        document_id: i64,
        checked_at_ms: i64,
        last_error: Option<&str>,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_documents
                 SET last_refresh_checked_at_ms = ?1, last_refresh_error = ?2
                 WHERE id = ?3 AND deleted_at IS NULL",
                params![checked_at_ms, last_error, document_id],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn set_bake_document_refresh_policy(
        &self,
        document_id: i64,
        policy: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_documents SET refresh_policy = ?1 WHERE id = ?2 AND deleted_at IS NULL",
                params![policy, document_id],
            )?;
            Ok(affected > 0)
        })
    }

    /// 保存不可变的原始来源快照。相同文档和内容指纹
    /// 只保留一条，重复校验时返回既有记录。
    pub fn upsert_bake_document_source_snapshot(
        &self,
        snapshot: &NewBakeDocumentSourceSnapshot,
    ) -> Result<BakeDocumentSourceSnapshotRecord, StorageError> {
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            require_document_source_writes(&tx, snapshot.document_id)?;
            let conn = &tx;
            conn.execute(
                "INSERT OR IGNORE INTO bake_document_source_snapshots (
                    document_id, source_url, page_title, content_text, content_hash,
                    completeness_status, identity_match, reached_end, stable_passes,
                    segment_count, character_count, truncated, collector, collected_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
                params![
                    snapshot.document_id,
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
                    snapshot.collected_at,
                ],
            )?;
            // Re-observing identical text can establish coverage that was
            // previously unknown. Upgrade evidence only; never downgrade it.
            if snapshot.completeness_status == "complete" && snapshot.identity_match
                && snapshot.reached_end && snapshot.stable_passes >= 2 && !snapshot.truncated {
                conn.execute("UPDATE bake_document_source_snapshots SET
                    completeness_status='complete', identity_match=1, reached_end=1,
                    stable_passes=?3, truncated=0, collected_at=?4, collector=?5
                    WHERE document_id=?1 AND content_hash=?2 AND completeness_status != 'complete'",
                    params![snapshot.document_id, snapshot.content_hash, snapshot.stable_passes,
                        snapshot.collected_at, snapshot.collector])?;
            }
            let record = conn.query_row(
                "SELECT id, document_id, source_url, page_title, content_text,
                        content_hash, completeness_status, identity_match, reached_end,
                        stable_passes, segment_count, character_count, truncated,
                        collector, collected_at
                 FROM bake_document_source_snapshots
                 WHERE document_id = ?1 AND content_hash = ?2
                 LIMIT 1",
                params![snapshot.document_id, snapshot.content_hash],
                row_to_document_source_snapshot,
            )
            .map_err(StorageError::Sqlite)?;
            tx.commit()?;
            Ok(record)
        })
    }

    pub fn get_latest_bake_document_source_snapshot(
        &self,
        document_id: i64,
    ) -> Result<Option<BakeDocumentSourceSnapshotRecord>, StorageError> {
        self.with_conn(|conn| {
            match conn.query_row(
                "SELECT id, document_id, source_url, page_title, content_text,
                        content_hash, completeness_status, identity_match, reached_end,
                        stable_passes, segment_count, character_count, truncated,
                        collector, collected_at
                 FROM bake_document_source_snapshots
                 WHERE document_id = ?1
                 ORDER BY collected_at DESC, id DESC
                 LIMIT 1",
                params![document_id],
                row_to_document_source_snapshot,
            ) {
                Ok(record) => Ok(Some(record)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(error) => Err(StorageError::Sqlite(error)),
            }
        })
    }

    /// Current evidence must describe the current body, not merely be the
    /// most recently collected historical snapshot for this document.
    pub fn get_current_verified_document_source_snapshot(
        &self,
        document_id: i64,
    ) -> Result<Option<BakeDocumentSourceSnapshotRecord>, StorageError> {
        self.with_conn(|conn| Ok(conn.query_row(
            "SELECT s.id,s.document_id,s.source_url,s.page_title,s.content_text,s.content_hash,
                    s.completeness_status,s.identity_match,s.reached_end,s.stable_passes,
                    s.segment_count,s.character_count,s.truncated,s.collector,s.collected_at
             FROM bake_document_source_heads h
             JOIN bake_documents d ON d.id=h.document_id
             JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id AND s.document_id=d.id
             WHERE d.id=?1 AND d.deleted_at IS NULL AND s.identity_match=1
                AND s.completeness_status='complete' AND s.content_text=d.full_content",
            params![document_id],row_to_document_source_snapshot).optional()?))
    }

    /// Apply only a verified full source version, preserving the previous record
    /// in the same transaction. Partial observations never replace a good body.
    pub fn apply_document_source_snapshot(&self, snapshot_id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            let snapshot = tx.query_row(
                "SELECT id, document_id, source_url, page_title, content_text, content_hash,
                 completeness_status, identity_match, reached_end, stable_passes,
                 segment_count, character_count, truncated, collector, collected_at
                 FROM bake_document_source_snapshots WHERE id = ?1",
                params![snapshot_id], row_to_document_source_snapshot)?;
            require_document_source_writes(&tx, snapshot.document_id)?;
            if snapshot.completeness_status != "complete" || !snapshot.identity_match
                || !snapshot.reached_end || snapshot.stable_passes < 2 || snapshot.truncated
                || snapshot.content_text.trim().is_empty() {
                return Ok(false);
            }
            let head_time: Option<i64> = tx.query_row(
                "SELECT s.collected_at FROM bake_document_source_heads h
                 JOIN bake_document_source_snapshots s ON s.id = h.snapshot_id
                 WHERE h.document_id = ?1", params![snapshot.document_id], |r| r.get(0)
            ).optional()?;
            if head_time.is_some_and(|time| time >= snapshot.collected_at) {
                return Ok(false);
            }
            let existing = tx.query_row(
                &format!("SELECT {} FROM bake_documents WHERE id = ?1 AND deleted_at IS NULL", SELECT_COLUMNS),
                params![snapshot.document_id], |row| row_to_bake_document(row).map_err(|_| rusqlite::Error::InvalidQuery))?;
            let existing_url = existing.source_url.as_deref().unwrap_or_default().trim();
            let snapshot_url = snapshot.source_url.trim();
            let identity_matches = match (
                canonical_document_identity(existing_url),
                canonical_document_identity(snapshot_url),
            ) {
                (Some(left), Some(right)) => left == right,
                // Unknown identities are not evidence of equality. Only an exact,
                // valid HTTP(S) source can use the generic collector fallback.
                (None, None) => !existing_url.is_empty() && existing_url == snapshot_url
                    && reqwest::Url::parse(existing_url).is_ok_and(|url|
                        matches!(url.scheme(), "http" | "https") && url.host_str().is_some()
                            && url.username().is_empty() && url.password().is_none()),
                _ => false,
            };
            if !identity_matches {
                return Ok(false);
            }
            let mut previous = serde_json::to_value(&existing)
                .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
            // The public document DTO omits these internal bindings. Archive
            // them explicitly in this same transaction so restore can verify
            // the old summary instead of losing its provenance.
            let (summary_source_id,summary_version):(Option<i64>,Option<String>)=tx.query_row(
                "SELECT summary_source_snapshot_id,summary_generation_version FROM bake_documents WHERE id=?1",
                params![snapshot.document_id],|r|Ok((r.get(0)?,r.get(1)?)))?;
            previous["summary_source_snapshot_id"]=serde_json::json!(summary_source_id);
            previous["summary_generation_version"]=serde_json::json!(summary_version);
            let previous=serde_json::to_string(&previous)
                .map_err(|error|rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
            let applied_at = current_ts_ms();
            tx.execute("INSERT OR IGNORE INTO bake_document_body_versions
                (document_id, replaced_by_snapshot_id, record_json, saved_at) VALUES (?1, ?2, ?3, ?4)",
                params![snapshot.document_id, snapshot.id, previous, applied_at])?;
            // Clear derived content in the same transaction: stale summaries and
            // structured material must not pretend to describe the new source.
            tx.execute("UPDATE bake_documents SET title = ?2, full_content = ?3,
                content_hash = ?4, summary = NULL, summary_source_snapshot_id = NULL, summary_generation_version = NULL, structured_content = '{}',
                sections_json = '[]', tags = '[]', prompt_hint = NULL,
                style_phrases = '[]', replacement_rules = '[]', applicable_tasks = '[]',
                diagram_code = NULL, image_assets = '[]', language = NULL,
                match_score = NULL, match_level = NULL,
                generation_version = 'document-source-v2', evidence_summary = '来自已校验完整原文',
                updated_at = ?5 WHERE id = ?1",
                params![snapshot.document_id, snapshot.page_title, snapshot.content_text,
                    snapshot.content_hash, applied_at])?;
            tx.execute("INSERT INTO bake_document_source_heads (document_id, snapshot_id, applied_at)
                VALUES (?1, ?2, ?3) ON CONFLICT(document_id) DO UPDATE SET
                snapshot_id=excluded.snapshot_id, applied_at=excluded.applied_at",
                params![snapshot.document_id, snapshot.id, applied_at])?;
            // Invalidate derived search material atomically with the source.
            // Qdrant deletion is durable and drained by the existing worker.
            tx.execute("INSERT OR IGNORE INTO vector_deletion_queue
                (qdrant_point_id, source_type, reason, enqueued_at)
                SELECT qdrant_point_id, 'document', 'document_source_replaced', ?2
                FROM artifact_vector_index WHERE document_id = ?1",
                params![snapshot.document_id, applied_at])?;
            tx.execute("DELETE FROM artifact_vector_index WHERE document_id = ?1",
                params![snapshot.document_id])?;
            tx.execute("INSERT OR IGNORE INTO bake_document_source_fingerprints
                (document_id, fingerprint, source_timeline_id, created_at) VALUES (?1, ?2, NULL, ?3)",
                params![snapshot.document_id, snapshot.content_hash, applied_at])?;
            tx.commit()?;
            Ok(true)
        })
    }

    pub fn record_document_refresh_success(
        &self,
        document_id: i64,
        checked_at_ms: i64,
        status: &str,
        snapshot: &BakeDocumentSourceSnapshotRecord,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            require_document_source_writes(&tx, document_id)?;
            let conn = &tx;
            let affected = conn.execute(
                "UPDATE bake_documents
                 SET last_refresh_checked_at_ms = ?1,
                     last_refresh_success_at_ms = ?1,
                     last_refresh_error = NULL,
                     last_refresh_status = ?2,
                     last_refresh_completeness = ?3,
                     last_refresh_content_hash = ?4,
                     last_refresh_character_count = ?5,
                     last_refresh_segment_count = ?6,
                     last_refresh_truncated = ?7
                 WHERE id = ?8 AND deleted_at IS NULL",
                params![
                    checked_at_ms,
                    status,
                    snapshot.completeness_status,
                    snapshot.content_hash,
                    snapshot.character_count,
                    snapshot.segment_count,
                    snapshot.truncated,
                    document_id,
                ],
            )?;
            tx.commit()?;
            Ok(affected > 0)
        })
    }

    pub fn record_document_refresh_failure(
        &self,
        document_id: i64,
        checked_at_ms: i64,
        status: &str,
        error_code: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_documents
                 SET last_refresh_checked_at_ms = ?1,
                     last_refresh_error = ?2,
                     last_refresh_status = ?3
                 WHERE id = ?4 AND deleted_at IS NULL",
                params![checked_at_ms, error_code, status, document_id],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn update_bake_document(
        &self,
        id: i64,
        doc: &NewBakeDocument,
    ) -> Result<bool, StorageError> {
        self.update_bake_document_guarded(id, doc, None)
    }

    /// A delayed model result may only update the unversioned body it read.
    /// The guard and write share one SQLite statement, including source-head protection.
    pub fn update_bake_document_from_observation(
        &self, existing: &BakeDocumentRecord, doc: &NewBakeDocument,
    ) -> Result<bool, StorageError> {
        self.update_bake_document_guarded(existing.id, doc, Some(existing))
    }

    fn update_bake_document_guarded(
        &self, id: i64, doc: &NewBakeDocument, expected: Option<&BakeDocumentRecord>,
    ) -> Result<bool, StorageError> {
        let updated_at = current_ts_ms();
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            let before: Option<(Option<String>, String, Option<String>, bool, Option<String>)> = tx.query_row(
                "SELECT full_content,title,source_url,EXISTS(SELECT 1 FROM bake_document_source_heads WHERE document_id=?1),summary
                 FROM bake_documents WHERE id=?1", params![id],
                |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
            if expected.is_some() {
                if let Some(row)=before.as_ref() {
                    require_automatic_document_writes(&tx,&row.1,row.2.as_deref())?;
                }
            }
            let body_changed = before.as_ref().is_some_and(|row| row.0 != doc.full_content);
            let identity_changed = before.as_ref().is_some_and(|row|
                canonical_document_identity(row.2.as_deref().unwrap_or_default())
                    .or_else(|| row.2.as_deref().map(str::trim).filter(|url| !url.is_empty()).map(str::to_owned))
                    != canonical_document_identity(doc.source_url.as_deref().unwrap_or_default())
                        .or_else(|| doc.source_url.as_deref().map(str::trim).filter(|url| !url.is_empty()).map(str::to_owned)));
            let summary_changed = before.as_ref().is_some_and(|row| row.4 != doc.summary);
            let index_changed = body_changed || identity_changed || summary_changed
                || before.as_ref().is_some_and(|row| row.1 != doc.title) || doc.deleted_at.is_some();
            let source_detached = (body_changed || identity_changed)
                && before.as_ref().is_some_and(|row| row.3);
            let affected = tx.execute(
                "UPDATE bake_documents
                 SET title = ?1, doc_type = ?2, status = ?3, tags = ?4, applicable_tasks = ?5,
                     source_memory_ids = ?6, source_capture_ids = ?7, source_episode_ids = ?8,
                     linked_knowledge_ids = ?9, sections_json = ?10,
                     style_phrases = ?11, replacement_rules = ?12,
                     summary_source_snapshot_id = CASE WHEN summary IS ?13 THEN summary_source_snapshot_id ELSE NULL END,
                     summary_generation_version = CASE WHEN summary IS ?13 THEN summary_generation_version ELSE NULL END,
                     summary = ?13, full_content = ?14, structured_content = ?15,
                     prompt_hint = ?16, diagram_code = ?17, image_assets = ?18,
                     source_app_name = ?19, source_win_title = ?20, source_url = ?21,
                     document_identity = ?22, content_hash = ?23, language = ?24,
                     usage_count = ?25, match_score = ?26, match_level = ?27,
                     creation_mode = ?28, review_status = ?29, evidence_summary = ?30,
                     generation_version = ?31, deleted_at = ?32, updated_at = ?33
                 WHERE id = ?34 AND (?35 IS NULL OR (
                     updated_at = ?35 AND full_content IS ?36 AND deleted_at IS NULL
                     AND NOT EXISTS (SELECT 1 FROM bake_document_source_heads WHERE document_id = ?34)
                 ))",
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
                    expected.map(|record| record.updated_at),
                    expected.and_then(|record| record.full_content.as_deref()),
                ],
            )?;
            if affected > 0 && source_detached {
                // The immutable source snapshot remains available, but it no longer
                // attests to the edited body. Drop that claim and its derived text atomically.
                tx.execute("DELETE FROM bake_document_source_heads WHERE document_id=?1", params![id])?;
                tx.execute("UPDATE bake_documents SET summary=NULL, summary_source_snapshot_id=NULL, summary_generation_version=NULL, structured_content='{}',
                    sections_json='[]', prompt_hint=NULL, content_hash=NULL,
                    generation_version='document-edited-v1', evidence_summary=NULL,
                    last_refresh_status='historical_only', last_refresh_completeness='unverified',
                    last_refresh_success_at_ms=0, last_refresh_content_hash=NULL,
                    last_refresh_character_count=0,last_refresh_segment_count=0,last_refresh_truncated=0
                    WHERE id=?1", params![id])?;
            }
            if affected > 0 && index_changed {
                tx.execute("INSERT OR IGNORE INTO vector_deletion_queue
                    (qdrant_point_id,source_type,reason,enqueued_at)
                    SELECT qdrant_point_id,'document','document_content_edited',?2
                    FROM artifact_vector_index WHERE document_id=?1", params![id,updated_at])?;
                tx.execute("DELETE FROM artifact_vector_index WHERE document_id=?1", params![id])?;
            }
            tx.commit()?;
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
        refresh_policy: row.get(31)?,
        last_refresh_checked_at_ms: row.get(32)?,
        last_refresh_error: row.get(33)?,
        last_refresh_success_at_ms: row.get(34)?,
        last_refresh_status: row.get(35)?,
        last_refresh_completeness: row.get(36)?,
        last_refresh_content_hash: row.get(37)?,
        last_refresh_character_count: row.get(38)?,
        last_refresh_segment_count: row.get(39)?,
        last_refresh_truncated: row.get(40)?,
        deleted_at: row.get(41)?,
        created_at: row.get(42)?,
        updated_at: row.get(43)?,
    })
}

fn row_to_document_source_snapshot(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<BakeDocumentSourceSnapshotRecord> {
    Ok(BakeDocumentSourceSnapshotRecord {
        id: row.get(0)?,
        document_id: row.get(1)?,
        source_url: row.get(2)?,
        page_title: row.get(3)?,
        content_text: row.get(4)?,
        content_hash: row.get(5)?,
        completeness_status: row.get(6)?,
        identity_match: row.get(7)?,
        reached_end: row.get(8)?,
        stable_passes: row.get(9)?,
        segment_count: row.get(10)?,
        character_count: row.get(11)?,
        truncated: row.get(12)?,
        collector: row.get(13)?,
        collected_at: row.get(14)?,
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
    fn automatic_document_write_pause_preserves_old_body_and_allows_manual_edits() {
        let mgr=make_mgr();
        let mut doc=sample_document();
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,
            r#"{"automatic_document_writes_enabled":false}"#,"test",1.0).unwrap();
        assert!(matches!(mgr.insert_bake_document_from_observation(&doc),Err(StorageError::DocumentAutomaticWritesPaused { .. })));
        assert!(mgr.list_bake_documents().unwrap().is_empty());
        let id=mgr.insert_bake_document(&doc).unwrap();
        let before=mgr.get_bake_document(id).unwrap().unwrap();
        doc.full_content=Some("延迟完成的自动合并结果。".into());
        assert!(matches!(mgr.update_bake_document_from_observation(&before,&doc),Err(StorageError::DocumentAutomaticWritesPaused { .. })));
        let after=mgr.get_bake_document(id).unwrap().unwrap();
        assert_eq!(after.full_content,before.full_content);
        assert_eq!(after.updated_at,before.updated_at);
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,
            r#"{"automatic_document_rollout_percent":0}"#,"test",1.0).unwrap();
        assert!(matches!(mgr.update_bake_document_from_observation(&before,&doc),Err(StorageError::DocumentAutomaticWritesPaused { .. })));
        // A user edit is not an automatic observation and remains available.
        assert!(mgr.update_bake_document(id,&doc).unwrap());
        let edited=mgr.get_bake_document(id).unwrap().unwrap();
        doc.full_content=Some("恢复后提交的新结果。".into());
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,"{}","test",1.0).unwrap();
        assert!(mgr.update_bake_document_from_observation(&edited,&doc).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().full_content,doc.full_content);
        doc.title="恢复后新文档".into();
        assert!(mgr.insert_bake_document_from_observation(&doc).is_ok());
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
    fn observation_write_rejects_stale_body_even_with_same_timestamp() {
        let mgr = make_mgr();
        let id = mgr.insert_bake_document(&sample_document()).unwrap();
        let original = mgr.get_bake_document(id).unwrap().unwrap();
        let mut update = sample_document();
        update.full_content = Some("新观察正文".into());
        assert!(mgr.update_bake_document_from_observation(&original, &update).unwrap());
        // Force timestamp equality to exercise body identity, not clock resolution.
        mgr.with_conn(|conn| {
            conn.execute("UPDATE bake_documents SET updated_at=?2 WHERE id=?1",
                params![id, original.updated_at])?;
            Ok(())
        }).unwrap();
        assert!(!mgr.update_bake_document_from_observation(&original, &sample_document()).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().full_content, update.full_content);
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
    fn test_find_document_by_source_url_ignores_declared_view_parameters() {
        let mgr = make_mgr();
        let mut document = sample_document();
        document.source_url =
            Some("https://Docs.Example.Com/d/home/ABC123?section=one#comment".to_string());
        let id = mgr.insert_bake_document(&document).unwrap();

        let found = mgr
            .find_document_by_source_url("https://docs.example.com/d/home/ABC123?section=two")
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
            Some("https://docs.example.com/d/home/abc123#section=two".to_string());
        assert!(mgr.insert_bake_document(&duplicate).is_err());
        duplicate.source_url=Some("https://docs.example.com/d/home/ABC123".into());
        assert!(mgr.insert_bake_document(&duplicate).is_ok());
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
    fn test_document_source_snapshot_is_idempotent_and_updates_refresh_metadata() {
        let mgr = make_mgr();
        let mut document = sample_document();
        document.full_content = Some("历史烘焙正文，不允许被即时抓取覆盖。".to_string());
        let id = mgr.insert_bake_document(&document).unwrap();
        let snapshot = NewBakeDocumentSourceSnapshot {
            document_id: id,
            source_url: "https://docs.example.com/d/home/abc123".to_string(),
            page_title: "即时来源".to_string(),
            content_text: "浏览器抓取的最新正文".to_string(),
            content_hash: "sha256:latest".to_string(),
            completeness_status: "partial".to_string(),
            identity_match: true,
            reached_end: false,
            stable_passes: 2,
            segment_count: 20,
            character_count: 30,
            truncated: true,
            collector: "browser_attach".to_string(),
            collected_at: 1_780_000_000_000,
        };

        let first = mgr.upsert_bake_document_source_snapshot(&snapshot).unwrap();
        let duplicate = mgr.upsert_bake_document_source_snapshot(&snapshot).unwrap();
        assert_eq!(first.id, duplicate.id);
        assert!(mgr
            .record_document_refresh_success(id, snapshot.collected_at, "fresh_partial", &first)
            .unwrap());

        let refreshed = mgr.get_bake_document(id).unwrap().unwrap();
        assert_eq!(
            refreshed.full_content.as_deref(),
            Some("历史烘焙正文，不允许被即时抓取覆盖。")
        );
        assert_eq!(refreshed.last_refresh_status, "fresh_partial");
        assert_eq!(refreshed.last_refresh_completeness, "partial");
        assert!(refreshed.last_refresh_truncated);
        assert_eq!(
            mgr.get_latest_bake_document_source_snapshot(id)
                .unwrap()
                .unwrap()
                .content_text,
            "浏览器抓取的最新正文"
        );
    }

    #[test]
    fn document_source_write_pause_rechecks_pending_publication_and_preserves_body() {
        let mgr = make_mgr();
        let mut doc = sample_document();
        doc.source_url = Some("https://docs.example.com/current".into());
        let id = mgr.insert_bake_document(&doc).unwrap();
        let mut source = NewBakeDocumentSourceSnapshot {
            document_id: id, source_url: doc.source_url.unwrap(),
            page_title: "来源".into(), content_text: "第一版已验证正文".into(),
            content_hash: "version-one".into(), completeness_status: "complete".into(),
            identity_match: true, reached_end: true, stable_passes: 2, segment_count: 1,
            character_count: 9, truncated: false, collector: "document-body.v3".into(), collected_at: 100,
        };
        let first = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(first.id).unwrap());
        source.content_hash = "version-two".into();
        source.content_text = "第二版新采集正文".into();
        source.collected_at = 200;
        let pending = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        let before = serde_json::to_value(mgr.get_bake_document(id).unwrap().unwrap()).unwrap();
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,
            r#"{"source_writes_enabled":false}"#, "user", 1.0).unwrap();
        assert!(matches!(mgr.apply_document_source_snapshot(pending.id),Err(StorageError::DocumentSourceWritesPaused)));
        assert!(matches!(mgr.record_document_refresh_success(id,200,"fresh_complete",&pending),Err(StorageError::DocumentSourceWritesPaused)));
        source.content_hash = "version-three".into();
        assert!(matches!(mgr.upsert_bake_document_source_snapshot(&source),Err(StorageError::DocumentSourceWritesPaused)));
        assert_eq!(serde_json::to_value(mgr.get_bake_document(id).unwrap().unwrap()).unwrap(),before);
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id,first.id);
        mgr.with_conn(|conn| {
            assert_eq!(conn.query_row("SELECT COUNT(*) FROM bake_document_source_snapshots WHERE document_id=?1",params![id],|r|r.get::<_,i64>(0))?,2);
            assert_eq!(conn.query_row("SELECT COUNT(*) FROM bake_document_body_versions WHERE document_id=?1",params![id],|r|r.get::<_,i64>(0))?,1);
            conn.execute("UPDATE user_preferences SET value='{}' WHERE key=?1",params![DOCUMENT_REFRESH_CONFIG_KEY])?;
            Ok(())
        }).unwrap();
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,
            &serde_json::json!({"rollout_document_ids":[id+1]}).to_string(), "user", 1.0).unwrap();
        assert!(matches!(mgr.apply_document_source_snapshot(pending.id),Err(StorageError::DocumentSourceWritesPaused)));
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id,first.id);
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,
            &serde_json::json!({"rollout_document_ids":[id]}).to_string(), "user", 1.0).unwrap();
        assert!(mgr.apply_document_source_snapshot(pending.id).unwrap());
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id,pending.id);
    }

    #[test]
    fn document_source_current_evidence_rejects_stale_or_invalid_heads() {
        let mgr = make_mgr();
        let mut doc = sample_document();
        doc.source_url = Some("https://docs.example.com/current".into());
        let id = mgr.insert_bake_document(&doc).unwrap();
        let other_id = mgr.insert_bake_document(&doc).unwrap();
        let source = NewBakeDocumentSourceSnapshot {
            document_id: id, source_url: "https://docs.example.com/current".into(),
            page_title: "来源".into(), content_text: "经核验的完整正文".into(),
            content_hash: "current-evidence".into(), completeness_status: "complete".into(),
            identity_match: true, reached_end: true, stable_passes: 2, segment_count: 1,
            character_count: 9, truncated: false, collector: "document-body.v3".into(),
            collected_at: 100,
        };
        let snapshot = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(snapshot.id).unwrap());
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id, snapshot.id);

        // A newer partial observation is history, never the currently applied source.
        let mut partial = source.clone();
        partial.content_hash = "new-partial-evidence".into();
        partial.content_text = "新观察片段".into();
        partial.completeness_status = "partial".into();
        partial.collected_at = 200;
        let latest = mgr.upsert_bake_document_source_snapshot(&partial).unwrap();
        assert_eq!(mgr.get_latest_bake_document_source_snapshot(id).unwrap().unwrap().id, latest.id);
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id, snapshot.id);

        for mutation in [
            "UPDATE bake_documents SET full_content='正文已编辑' WHERE id=?1",
            "UPDATE bake_documents SET deleted_at=1 WHERE id=?1",
            "UPDATE bake_document_source_snapshots SET completeness_status='partial' WHERE id=?2",
            "UPDATE bake_document_source_snapshots SET identity_match=0 WHERE id=?2",
            "UPDATE bake_document_source_snapshots SET document_id=?3 WHERE id=?2",
        ] {
            mgr.with_conn(|conn| {
                conn.execute_batch("SAVEPOINT invalid_head")?;
                let mut stmt = conn.prepare(mutation)?;
                let count = stmt.parameter_count();
                let values = [id, snapshot.id, other_id];
                stmt.execute(rusqlite::params_from_iter(values[..count].iter()))?;
                Ok(())
            }).unwrap();
            assert!(mgr.get_current_verified_document_source_snapshot(id).unwrap().is_none(), "{}", mutation);
            mgr.with_conn(|conn| {
                conn.execute_batch("ROLLBACK TO invalid_head; RELEASE invalid_head")?;
                Ok(())
            }).unwrap();
        }
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id, snapshot.id);
    }

    #[test]
    fn document_source_snapshot_apply_is_atomic_and_protects_complete_body() {
        let mgr = make_mgr();
        let mut doc = sample_document();
        doc.source_url = Some("https://docs.example.com/d/home/versioned".into());
        doc.style_phrases = "[\"obsolete source phrase\"]".into();
        doc.replacement_rules = "[\"obsolete source rule\"]".into();
        doc.applicable_tasks = "[\"obsolete source task\"]".into();
        doc.diagram_code = Some("graph TD; Old-->Source".into());
        doc.image_assets = "[\"old-source-image.png\"]".into();
        doc.language = Some("en".into());
        doc.match_score = Some(0.95);
        doc.match_level = Some("strong".into());
        let id = mgr.insert_bake_document(&doc).unwrap();
        let mut source = NewBakeDocumentSourceSnapshot {
            document_id: id, source_url: doc.source_url.clone().unwrap(),
            page_title: "真实来源".into(), content_text: "已验证的完整正文，包含实际业务内容。".into(),
            content_hash: "source-v1:complete".into(), completeness_status: "complete".into(),
            identity_match: true, reached_end: true, stable_passes: 2, segment_count: 1,
            character_count: 24, truncated: false, collector: "document-body.v2".into(), collected_at: 100,
        };
        let first = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        mgr.enqueue_document_refresh_observation(id,"before-check",Some(1),50).unwrap();
        mgr.enqueue_document_refresh_observation(id,"during-check",Some(2),150).unwrap();
        mgr.acknowledge_document_observations(id,first.id,100).unwrap();
        assert_eq!(mgr.document_refresh_observation_state(id,"before-check").unwrap().as_deref(),Some("completed"));
        assert_eq!(mgr.document_refresh_observation_state(id,"during-check").unwrap().as_deref(),Some("pending"));
        mgr.with_conn(|conn| {
            conn.execute("INSERT INTO artifact_vector_index
                (document_id,qdrant_point_id,doc_key,content_hash,chunk_index,chunk_text,indexed_at)
                VALUES (?1,'stale-point','old-key','old-hash',0,'旧外壳摘要',1)", params![id])?;
            Ok(())
        }).unwrap();
        assert!(mgr.apply_document_source_snapshot(first.id).unwrap());
        assert!(!mgr.apply_document_source_snapshot(first.id).unwrap());
        let current = mgr.get_bake_document(id).unwrap().unwrap();
        assert_eq!(current.full_content.as_deref(), Some(source.content_text.as_str()));
        assert!(current.summary.is_none());
        assert_eq!(current.style_phrases,"[]");
        assert_eq!(current.replacement_rules,"[]");
        assert_eq!(current.applicable_tasks,"[]");
        assert!(current.diagram_code.is_none());
        assert_eq!(current.image_assets,"[]");
        assert!(current.language.is_none());
        assert!(current.match_score.is_none());
        assert!(current.match_level.is_none());
        // Even a current read cannot use the legacy model path to overwrite a source head.
        assert!(!mgr.update_bake_document_from_observation(&current, &doc).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().full_content, current.full_content);
        assert_eq!(current.source_capture_ids, doc.source_capture_ids);
        mgr.with_conn(|conn| {
            let stale_count: i64 = conn.query_row("SELECT COUNT(*) FROM artifact_vector_index WHERE document_id=?1", params![id], |r| r.get(0))?;
            assert_eq!(stale_count, 0);
            let queued: i64 = conn.query_row("SELECT COUNT(*) FROM vector_deletion_queue WHERE qdrant_point_id='stale-point' AND reason='document_source_replaced'", [], |r| r.get(0))?;
            assert_eq!(queued, 1);
            let backup: String = conn.query_row("SELECT record_json FROM bake_document_body_versions WHERE document_id=?1", params![id], |r| r.get(0))?;
            let backup: serde_json::Value = serde_json::from_str(&backup).unwrap();
            assert_eq!(backup["full_content"].as_str(), doc.full_content.as_deref());
            assert_eq!(backup["style_phrases"].as_str(),Some(doc.style_phrases.as_str()));
            assert_eq!(backup["replacement_rules"].as_str(),Some(doc.replacement_rules.as_str()));
            assert_eq!(backup["applicable_tasks"].as_str(),Some(doc.applicable_tasks.as_str()));
            assert_eq!(backup["diagram_code"].as_str(),doc.diagram_code.as_deref());
            assert_eq!(backup["image_assets"].as_str(),Some(doc.image_assets.as_str()));
            assert_eq!(backup["language"].as_str(),doc.language.as_deref());
            assert_eq!(backup["match_score"].as_f64(),doc.match_score);
            assert_eq!(backup["match_level"].as_str(),doc.match_level.as_deref());
            Ok(())
        }).unwrap();
        source.content_hash = "source-v1:partial".into();
        // A partial observation cannot discard material added to the current version.
        mgr.with_conn(|conn| {
            conn.execute("UPDATE bake_documents SET diagram_code='current diagram',image_assets='[\"current.png\"]',
                language='zh',match_score=0.7,match_level='current' WHERE id=?1",params![id])?;
            Ok(())
        }).unwrap();
        source.completeness_status = "partial".into();
        source.content_text = "不完整更新".into();
        source.collected_at = 200;
        let partial = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(!mgr.apply_document_source_snapshot(partial.id).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().full_content, current.full_content);
        let preserved=mgr.get_bake_document(id).unwrap().unwrap();
        assert_eq!(preserved.diagram_code.as_deref(),Some("current diagram"));
        assert_eq!(preserved.image_assets,"[\"current.png\"]");
        assert_eq!(preserved.language.as_deref(),Some("zh"));
        assert_eq!(preserved.match_score,Some(0.7));
        assert_eq!(preserved.match_level.as_deref(),Some("current"));
        source.content_hash = "source-v1:shorter".into();
        source.completeness_status = "complete".into();
        source.content_text = "作者删减后的短文。".into();
        source.collected_at = 300;
        let shorter = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(shorter.id).unwrap());
        assert!(!mgr.apply_document_source_snapshot(first.id).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().full_content.as_deref(), Some("作者删减后的短文。"));
    }

    #[test]
    fn document_summary_edit_revokes_binding_and_invalidates_vectors() {
        let mgr = make_mgr();
        let mut doc = sample_document();
        doc.source_url = Some("https://docs.example.com/summary".into());
        let id = mgr.insert_bake_document(&doc).unwrap();
        let source = NewBakeDocumentSourceSnapshot {
            document_id:id, source_url:doc.source_url.clone().unwrap(), page_title:doc.title.clone(),
            content_text:"完整来源正文".into(),content_hash:"summary-source".into(),
            completeness_status:"complete".into(), identity_match:true,reached_end:true,
            stable_passes:2,segment_count:1,character_count:6,truncated:false,
            collector:"document-body.v3".into(),collected_at:100,
        };
        let snapshot = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(snapshot.id).unwrap());
        doc.full_content = Some(source.content_text.clone());
        doc.summary = Some("来源摘要".into());
        let revision = mgr.get_bake_document(id).unwrap().unwrap().updated_at;
        assert!(mgr.publish_document_source_summary(id,snapshot.id,revision,doc.summary.as_deref().unwrap()).unwrap());
        assert!(!mgr.publish_document_source_summary(id,snapshot.id,revision,"迟到的摘要").unwrap());
        doc.status = "archived".into();
        assert!(mgr.update_bake_document(id,&doc).unwrap());
        mgr.with_conn(|conn| {
            let binding:Option<i64> = conn.query_row("SELECT summary_source_snapshot_id FROM bake_documents WHERE id=?1",params![id],|r|r.get(0))?;
            assert_eq!(binding,Some(snapshot.id));
            conn.execute("INSERT INTO artifact_vector_index (document_id,qdrant_point_id,doc_key,content_hash,chunk_index,chunk_text,indexed_at)
                VALUES (?1,'summary-edit-point','summary-key','hash',0,'来源摘要',1)",params![id])?;
            Ok(())
        }).unwrap();
        doc.summary = Some("用户写入的新摘要".into());
        assert!(mgr.update_bake_document(id,&doc).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().summary,doc.summary);
        assert_eq!(mgr.get_current_verified_document_source_snapshot(id).unwrap().unwrap().id,snapshot.id);
        mgr.with_conn(|conn| {
            let binding:Option<i64> = conn.query_row("SELECT summary_source_snapshot_id FROM bake_documents WHERE id=?1",params![id],|r|r.get(0))?;
            assert_eq!(binding,None);
            assert_eq!(conn.query_row("SELECT COUNT(*) FROM artifact_vector_index WHERE document_id=?1",params![id],|r|r.get::<_,i64>(0))?,0);
            assert_eq!(conn.query_row("SELECT COUNT(*) FROM vector_deletion_queue WHERE qdrant_point_id='summary-edit-point'",[],|r|r.get::<_,i64>(0))?,1);
            Ok(())
        }).unwrap();
    }

    #[test]
    fn document_summary_publication_rechecks_source_revision_and_pause() {
        let mgr = make_mgr();
        let mut doc = sample_document();
        doc.source_url = Some("https://docs.example.com/summary-cas".into());
        let id = mgr.insert_bake_document(&doc).unwrap();
        let mut source = NewBakeDocumentSourceSnapshot {
            document_id:id,source_url:doc.source_url.clone().unwrap(),page_title:doc.title.clone(),
            content_text:"第一版完整正文".into(),content_hash:"summary-first".into(),
            completeness_status:"complete".into(),identity_match:true,reached_end:true,
            stable_passes:2,segment_count:1,character_count:7,truncated:false,
            collector:"document-body.v3".into(),collected_at:100,
        };
        let first = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(first.id).unwrap());
        let original = mgr.get_bake_document(id).unwrap().unwrap().updated_at;
        assert!(mgr.publish_document_source_summary(id,first.id,original,"第一版正确摘要").unwrap());
        let original = mgr.get_bake_document(id).unwrap().unwrap().updated_at;
        source.content_text="第二版完整正文".into();
        source.content_hash="summary-second".into();
        source.collected_at=200;
        let second=mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(second.id).unwrap());
        mgr.with_conn(|c| {
            let raw:String=c.query_row("SELECT record_json FROM bake_document_body_versions WHERE document_id=?1 AND replaced_by_snapshot_id=?2",
                params![id,second.id],|r|r.get(0))?;
            let archived:serde_json::Value=serde_json::from_str(&raw).unwrap();
            assert_eq!(archived["summary"],"第一版正确摘要");
            assert_eq!(archived["summary_source_snapshot_id"],first.id);
            assert_eq!(archived["summary_generation_version"],"document-summary.v1");
            Ok(())
        }).unwrap();
        assert!(!mgr.publish_document_source_summary(id,first.id,original,"第一版摘要").unwrap());
        mgr.with_conn(|c| {
            let event:(i64,i64,i64)=c.query_row("SELECT expected_snapshot_id,observed_snapshot_id,occurrences
                FROM document_source_mismatch_events WHERE document_id=?1 AND component='summary_write'",
                params![id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
            assert_eq!(event,(first.id,second.id,1));
            c.execute_batch("CREATE TRIGGER reject_direct_summary_audit BEFORE INSERT ON document_source_mismatch_events
                BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")?;Ok(())
        }).unwrap();
        assert!(!mgr.publish_document_source_summary(id,first.id,original,"旧摘要仍应拒绝").unwrap());
        mgr.with_conn(|c| {c.execute_batch("DROP TRIGGER reject_direct_summary_audit")?;Ok(())}).unwrap();
        let current=mgr.get_bake_document(id).unwrap().unwrap();
        for config in [r#"{"source_writes_enabled":false}"#,
            r#"{"automatic_document_writes_enabled":false}"#,
            r#"{"rollout_document_ids":[]}"#] {
            mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,config,"user",1.0).unwrap();
            assert!(mgr.publish_document_source_summary(id,second.id,current.updated_at,"第二版摘要").is_err());
            assert!(mgr.get_bake_document(id).unwrap().unwrap().summary.is_none());
        }
        mgr.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,"{}","user",1.0).unwrap();
        assert!(!mgr.publish_document_source_summary(id,second.id,current.updated_at,"  ").unwrap());
        mgr.with_conn(|c| {
            c.execute("INSERT INTO artifact_vector_index (document_id,qdrant_point_id,doc_key,content_hash,chunk_index,chunk_text,indexed_at)
                VALUES (?1,'summary-cas-point','key','hash',0,'旧索引',1)",params![id])?;Ok(())
        }).unwrap();
        mgr.with_conn(|c| {
            c.execute_batch("CREATE TRIGGER reject_summary_vector_cleanup BEFORE DELETE ON artifact_vector_index
                BEGIN SELECT RAISE(ABORT,'injected cleanup failure'); END")?;Ok(())
        }).unwrap();
        assert!(mgr.publish_document_source_summary(id,second.id,current.updated_at,"第二版摘要").is_err());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().updated_at,current.updated_at);
        assert!(mgr.get_bake_document(id).unwrap().unwrap().summary.is_none());
        mgr.with_conn(|c| {
            assert_eq!(c.query_row("SELECT COUNT(*) FROM vector_deletion_queue WHERE qdrant_point_id='summary-cas-point'",[],|r|r.get::<_,i64>(0))?,0);
            c.execute_batch("DROP TRIGGER reject_summary_vector_cleanup")?;Ok(())
        }).unwrap();
        assert!(mgr.publish_document_source_summary(id,second.id,current.updated_at,"第二版摘要").unwrap());
        let published=mgr.get_bake_document(id).unwrap().unwrap();
        assert!(!mgr.publish_document_source_summary(id,second.id,published.updated_at,"不覆盖现有摘要").unwrap());
        assert_eq!(published.full_content,current.full_content);
        assert!(published.updated_at>current.updated_at);
        mgr.with_conn(|c| {
            assert_eq!(c.query_row("SELECT summary_source_snapshot_id FROM bake_documents WHERE id=?1",params![id],|r|r.get::<_,i64>(0))?,second.id);
            assert_eq!(c.query_row("SELECT SUM(occurrences) FROM document_source_mismatch_events WHERE document_id=?1 AND component='summary_write'",params![id],|r|r.get::<_,i64>(0))?,1);
            assert_eq!(c.query_row("SELECT COUNT(*) FROM artifact_vector_index WHERE document_id=?1",params![id],|r|r.get::<_,i64>(0))?,0);
            assert_eq!(c.query_row("SELECT COUNT(*) FROM vector_deletion_queue WHERE qdrant_point_id='summary-cas-point'",[],|r|r.get::<_,i64>(0))?,1);
            Ok(())
        }).unwrap();
    }

    #[test]
    fn editing_source_body_or_identity_detaches_provenance_and_indexes_atomically() {
        for change_identity in [false, true] {
            let mgr = make_mgr();
            let mut doc = sample_document();
            doc.source_url = Some("https://docs.example.com/d/original".into());
            let id = mgr.insert_bake_document(&doc).unwrap();
            let source = NewBakeDocumentSourceSnapshot {
                document_id:id, source_url:doc.source_url.clone().unwrap(), page_title:doc.title.clone(),
                content_text:"完整来源正文".into(),content_hash:"complete-source".into(),
                completeness_status:"complete".into(), identity_match:true,reached_end:true,
                stable_passes:2,segment_count:1,character_count:6,truncated:false,
                collector:"document-body.v3".into(),collected_at:100,
            };
            let snapshot = mgr.upsert_bake_document_source_snapshot(&source).unwrap();
            assert!(mgr.apply_document_source_snapshot(snapshot.id).unwrap());
            doc.full_content = Some(source.content_text.clone());
            // Metadata-only edits preserve the source claim.
            doc.title = "用户整理的标题".into();
            assert!(mgr.update_bake_document(id,&doc).unwrap());
            mgr.with_conn(|conn| {
                let count:i64=conn.query_row("SELECT COUNT(*) FROM bake_document_source_heads WHERE document_id=?1",params![id],|r|r.get(0))?;
                assert_eq!(count,1);
                conn.execute("INSERT INTO artifact_vector_index (document_id,qdrant_point_id,doc_key,content_hash,chunk_index,chunk_text,indexed_at)
                    VALUES (?1,'edited-point','old-key','old-hash',0,'旧正文索引',1)",params![id])?;
                Ok(())
            }).unwrap();
            if change_identity { doc.source_url=Some("https://docs.example.com/d/different".into()); }
            else { doc.full_content=Some("用户修改后的正文".into()); }
            doc.summary=Some("旧摘要不应继承".into());
            assert!(mgr.update_bake_document(id,&doc).unwrap());
            let current=mgr.get_bake_document(id).unwrap().unwrap();
            assert_eq!(current.full_content,doc.full_content);
            assert_eq!(current.last_refresh_completeness,"unverified");
            assert!(current.summary.is_none());
            mgr.with_conn(|conn| {
                for table in ["bake_document_source_heads","artifact_vector_index"] {
                    let count:i64=conn.query_row(&format!("SELECT COUNT(*) FROM {table} WHERE document_id=?1"),params![id],|r|r.get(0))?;
                    assert_eq!(count,0);
                }
                let retained:i64=conn.query_row("SELECT COUNT(*) FROM bake_document_source_snapshots WHERE id=?1",params![snapshot.id],|r|r.get(0))?;
                assert_eq!(retained,1);
                let queued:i64=conn.query_row("SELECT COUNT(*) FROM vector_deletion_queue WHERE qdrant_point_id='edited-point' AND reason='document_content_edited'",[],|r|r.get(0))?;
                assert_eq!(queued,1);
                Ok(())
            }).unwrap();
        }
    }

    #[test]
    fn unknown_source_urls_cannot_authorize_a_different_snapshot() {
        let mgr=make_mgr();
        let mut doc=sample_document();
        doc.source_url=Some("https://example.com/resource/a".into());
        let id=mgr.insert_bake_document(&doc).unwrap();
        let mut source=NewBakeDocumentSourceSnapshot {
            document_id:id,source_url:"https://example.com/resource/b".into(),
            page_title:"同名文档".into(),content_text:"来自另一网址的正文".into(),
            content_hash:"different-source".into(),completeness_status:"complete".into(),
            identity_match:true,reached_end:true,stable_passes:2,segment_count:1,
            character_count:10,truncated:false,collector:"document-body.v3".into(),collected_at:100,
        };
        let different=mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(!mgr.apply_document_source_snapshot(different.id).unwrap());
        assert_eq!(mgr.get_bake_document(id).unwrap().unwrap().full_content,doc.full_content);
        source.source_url=doc.source_url.clone().unwrap();
        source.content_hash="same-exact-source".into();
        let exact=mgr.upsert_bake_document_source_snapshot(&source).unwrap();
        assert!(mgr.apply_document_source_snapshot(exact.id).unwrap());
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
    fn test_list_bake_documents_orders_by_created_at_not_updated_at() {
        let mgr = make_mgr();
        let older_id = mgr.insert_bake_document(&sample_document()).unwrap();
        let mut newer = sample_document();
        newer.title = "较新创建的文档".to_string();
        newer.source_url = Some("https://docs.example.com/newer".to_string());
        let newer_id = mgr.insert_bake_document(&newer).unwrap();

        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_documents SET created_at = ?1, updated_at = ?2 WHERE id = ?3",
                params![1_000_i64, 3_000_i64, older_id],
            )?;
            conn.execute(
                "UPDATE bake_documents SET created_at = ?1, updated_at = ?2 WHERE id = ?3",
                params![2_000_i64, 2_000_i64, newer_id],
            )?;
            Ok(())
        })
        .unwrap();

        let paginated = mgr.list_bake_documents_paginated(None, 10, 0).unwrap();
        assert_eq!(
            paginated
                .iter()
                .map(|document| document.id)
                .collect::<Vec<_>>(),
            vec![newer_id, older_id]
        );

        let all = mgr.list_bake_documents().unwrap();
        assert_eq!(
            all.iter().map(|document| document.id).collect::<Vec<_>>(),
            vec![newer_id, older_id]
        );
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
