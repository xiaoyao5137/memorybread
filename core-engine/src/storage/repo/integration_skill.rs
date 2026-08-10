use rusqlite::{params, OptionalExtension};
use serde_json::{json, Value};

use crate::storage::{
    db::current_ts_ms,
    error::StorageError,
    models_integration::{
        ImportWriteOutcome, ImportedKnowledgeItem, IntegrationSkillLogEntry,
        IntegrationSkillRunRecord,
    },
    StorageManager,
};

impl StorageManager {
    pub fn create_integration_skill_run(
        &self,
        id: &str,
        skill_id: &str,
        mode: &str,
        input_summary: &Value,
    ) -> Result<IntegrationSkillRunRecord, StorageError> {
        let now = current_ts_ms();
        let logs = vec![IntegrationSkillLogEntry {
            ts: now,
            level: "info".to_string(),
            message: "执行请求已进入本地队列".to_string(),
        }];
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO integration_skill_runs (
                    id, skill_id, mode, status, input_summary, logs_json, created_at_ms
                 ) VALUES (?1, ?2, ?3, 'queued', ?4, ?5, ?6)",
                params![
                    id,
                    skill_id,
                    mode,
                    serde_json::to_string(input_summary)?,
                    serde_json::to_string(&logs)?,
                    now,
                ],
            )?;
            Ok(IntegrationSkillRunRecord {
                id: id.to_string(),
                skill_id: skill_id.to_string(),
                mode: mode.to_string(),
                status: "queued".to_string(),
                input_summary: input_summary.clone(),
                result: None,
                logs,
                error_code: None,
                error_message: None,
                created_at_ms: now,
                started_at_ms: None,
                finished_at_ms: None,
            })
        })
    }

    pub fn start_integration_skill_run(&self, id: &str) -> Result<(), StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            let mut logs = read_run_logs(conn, id)?;
            logs.push(IntegrationSkillLogEntry {
                ts: now,
                level: "info".to_string(),
                message: "本地执行器已启动".to_string(),
            });
            conn.execute(
                "UPDATE integration_skill_runs
                 SET status = 'running', started_at_ms = ?2, logs_json = ?3
                 WHERE id = ?1",
                params![id, now, serde_json::to_string(&logs)?],
            )?;
            Ok(())
        })
    }

    pub fn append_integration_skill_log(
        &self,
        id: &str,
        level: &str,
        message: &str,
    ) -> Result<(), StorageError> {
        let safe_message = message.chars().take(500).collect::<String>();
        self.with_conn(|conn| {
            let mut logs = read_run_logs(conn, id)?;
            logs.push(IntegrationSkillLogEntry {
                ts: current_ts_ms(),
                level: level.to_string(),
                message: safe_message,
            });
            if logs.len() > 500 {
                logs.drain(..logs.len() - 500);
            }
            conn.execute(
                "UPDATE integration_skill_runs SET logs_json = ?2 WHERE id = ?1",
                params![id, serde_json::to_string(&logs)?],
            )?;
            Ok(())
        })
    }

    pub fn finish_integration_skill_run(
        &self,
        id: &str,
        result: &Value,
    ) -> Result<(), StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            let mut logs = read_run_logs(conn, id)?;
            logs.push(IntegrationSkillLogEntry {
                ts: now,
                level: "success".to_string(),
                message: "执行完成，结果已经保存在本机".to_string(),
            });
            conn.execute(
                "UPDATE integration_skill_runs
                 SET status = 'succeeded', result_json = ?2, logs_json = ?3,
                     error_code = NULL, error_message = NULL, finished_at_ms = ?4
                 WHERE id = ?1",
                params![
                    id,
                    serde_json::to_string(result)?,
                    serde_json::to_string(&logs)?,
                    now,
                ],
            )?;
            Ok(())
        })
    }

    pub fn fail_integration_skill_run(
        &self,
        id: &str,
        error_code: &str,
        error_message: &str,
    ) -> Result<(), StorageError> {
        let now = current_ts_ms();
        let safe_message = error_message.chars().take(500).collect::<String>();
        self.with_conn(|conn| {
            let mut logs = read_run_logs(conn, id)?;
            logs.push(IntegrationSkillLogEntry {
                ts: now,
                level: "error".to_string(),
                message: safe_message.clone(),
            });
            conn.execute(
                "UPDATE integration_skill_runs
                 SET status = 'failed', logs_json = ?2, error_code = ?3,
                     error_message = ?4, finished_at_ms = ?5
                 WHERE id = ?1",
                params![
                    id,
                    serde_json::to_string(&logs)?,
                    error_code,
                    safe_message,
                    now,
                ],
            )?;
            Ok(())
        })
    }

    pub fn get_integration_skill_run(
        &self,
        id: &str,
    ) -> Result<Option<IntegrationSkillRunRecord>, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT id, skill_id, mode, status, input_summary, result_json, logs_json,
                        error_code, error_message, created_at_ms, started_at_ms, finished_at_ms
                 FROM integration_skill_runs WHERE id = ?1",
                [id],
                map_run_row,
            )
            .optional()
            .map_err(Into::into)
        })
    }

    pub fn list_integration_skill_runs(
        &self,
        skill_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<IntegrationSkillRunRecord>, StorageError> {
        self.with_conn(|conn| {
            if let Some(skill_id) = skill_id {
                let mut stmt = conn.prepare(
                    "SELECT id, skill_id, mode, status, input_summary, result_json, logs_json,
                            error_code, error_message, created_at_ms, started_at_ms, finished_at_ms
                     FROM integration_skill_runs WHERE skill_id = ?1
                     ORDER BY created_at_ms DESC LIMIT ?2",
                )?;
                let rows = stmt.query_map(params![skill_id, limit], map_run_row)?;
                return rows.collect::<Result<Vec<_>, _>>().map_err(Into::into);
            }
            let mut stmt = conn.prepare(
                "SELECT id, skill_id, mode, status, input_summary, result_json, logs_json,
                        error_code, error_message, created_at_ms, started_at_ms, finished_at_ms
                 FROM integration_skill_runs ORDER BY created_at_ms DESC LIMIT ?1",
            )?;
            let rows = stmt.query_map([limit], map_run_row)?;
            rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
        })
    }

    pub fn upsert_integration_import_item(
        &self,
        skill_id: &str,
        item: &ImportedKnowledgeItem,
    ) -> Result<ImportWriteOutcome, StorageError> {
        self.with_conn(|conn| {
            let existing = conn
                .query_row(
                    "SELECT i.content_hash, i.capture_id, i.timeline_id, c.win_title
                     FROM integration_import_items i
                     JOIN captures c ON c.id = i.capture_id
                     WHERE i.skill_id = ?1 AND i.source_key = ?2",
                    params![skill_id, item.source_key],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, i64>(2)?,
                            row.get::<_, Option<String>>(3)?,
                        ))
                    },
                )
                .optional()?;
            if let Some((content_hash, _, timeline_id, stored_title)) = &existing {
                if content_hash == &item.content_hash
                    && stored_title.as_deref() == Some(item.title.as_str())
                {
                    return Ok(ImportWriteOutcome::Unchanged(*timeline_id));
                }
            }

            let now = current_ts_ms();
            let overview = content_overview(&item.content);
            let entities_json = serde_json::to_string(&item.entities)?;
            let metadata_json = serde_json::to_string(&item.metadata)?;
            let source_url = format!("memorybread://integration/{}/{}", skill_id, item.source_key);

            if let Some((_, capture_id, timeline_id, _)) = existing {
                conn.execute(
                    "UPDATE captures
                     SET ts = ?2, app_name = 'MemoryBread Integration',
                         win_title = ?3, ax_text = ?4, url = ?5, webpage_title = ?3,
                         timeline_id = ?6
                     WHERE id = ?1",
                    params![
                        capture_id,
                        now,
                        item.title,
                        item.content,
                        source_url,
                        timeline_id,
                    ],
                )?;
                conn.execute(
                    "UPDATE timelines
                     SET summary = ?2, overview = ?3, details = ?4, entities = ?5,
                         observed_at = ?6, event_time_start = ?6, event_time_end = ?6,
                         updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                     WHERE id = ?1",
                    params![
                        timeline_id,
                        item.title,
                        overview,
                        item.content,
                        entities_json,
                        now,
                    ],
                )?;
                conn.execute(
                    "UPDATE integration_import_items
                     SET source_path = ?3, content_hash = ?4, metadata_json = ?5,
                         updated_at_ms = ?6
                     WHERE skill_id = ?1 AND source_key = ?2",
                    params![
                        skill_id,
                        item.source_key,
                        item.source_path,
                        item.content_hash,
                        metadata_json,
                        now,
                    ],
                )?;
                return Ok(ImportWriteOutcome::Updated(timeline_id));
            }

            conn.execute(
                "INSERT INTO captures (
                    ts, app_name, app_bundle_id, win_title, event_type, ax_text,
                    is_sensitive, pii_scrubbed, url, webpage_title
                 ) VALUES (?1, 'MemoryBread Integration', 'com.memorybread.integration',
                           ?2, 'manual', ?3, 0, 1, ?4, ?2)",
                params![now, item.title, item.content, source_url],
            )?;
            let capture_id = conn.last_insert_rowid();
            conn.execute(
                "INSERT INTO timelines (
                    capture_id, summary, overview, details, entities, category, importance,
                    occurrence_count, observed_at, event_time_start, event_time_end,
                    history_view, content_origin, activity_type, is_self_generated,
                    evidence_strength, user_verified, user_edited, created_at, updated_at,
                    created_at_ms, updated_at_ms, capture_ids, start_time, end_time,
                    time_range_start, time_range_end, key_timestamps
                 ) VALUES (
                    ?1, ?2, ?3, ?4, ?5, 'imported_knowledge', 4, 1, ?6, ?6, ?6,
                    1, ?7, 'reading', 0, 'high', 1, 0,
                    datetime(?6 / 1000, 'unixepoch'), datetime(?6 / 1000, 'unixepoch'),
                    ?6, ?6, ?8, ?6, ?6, ?6, ?6, '[]'
                 )",
                params![
                    capture_id,
                    item.title,
                    overview,
                    item.content,
                    entities_json,
                    now,
                    format!("integration_import:{skill_id}"),
                    serde_json::to_string(&vec![capture_id])?,
                ],
            )?;
            let timeline_id = conn.last_insert_rowid();
            conn.execute(
                "UPDATE captures SET timeline_id = ?2 WHERE id = ?1",
                params![capture_id, timeline_id],
            )?;
            conn.execute(
                "INSERT INTO integration_import_items (
                    skill_id, source_key, source_path, content_hash, capture_id,
                    timeline_id, metadata_json, created_at_ms, updated_at_ms
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)",
                params![
                    skill_id,
                    item.source_key,
                    item.source_path,
                    item.content_hash,
                    capture_id,
                    timeline_id,
                    metadata_json,
                    now,
                ],
            )?;
            Ok(ImportWriteOutcome::Created(timeline_id))
        })
    }

    pub fn integration_export_context(
        &self,
        query: &str,
        limit: usize,
    ) -> Result<Vec<Value>, StorageError> {
        self.with_conn(|conn| {
            let escaped_query = query
                .trim()
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_");
            let pattern = format!("%{escaped_query}%");
            let mut stmt = conn.prepare(
                "SELECT id, summary, overview, details, category, observed_at,
                        content_origin, updated_at_ms
                 FROM timelines
                 WHERE (history_view = 0 OR history_view = 1)
                   AND (?1 = '%%' OR summary LIKE ?1 ESCAPE '\\'
                        OR COALESCE(overview, '') LIKE ?1 ESCAPE '\\'
                        OR COALESCE(details, '') LIKE ?1 ESCAPE '\\')
                 ORDER BY
                    CASE WHEN summary LIKE ?1 ESCAPE '\\' THEN 0 ELSE 1 END,
                    updated_at_ms DESC
                 LIMIT ?2",
            )?;
            let rows = stmt.query_map(params![pattern, limit], integration_memory_row)?;
            rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
        })
    }

    pub fn integration_export_memories_by_ids(
        &self,
        ids: &[i64],
    ) -> Result<Vec<Value>, StorageError> {
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        self.with_conn(|conn| {
            let placeholders = vec!["?"; ids.len()].join(",");
            let sql = format!(
                "SELECT id, summary, overview, details, category, observed_at,
                        content_origin, updated_at_ms
                 FROM timelines
                 WHERE (history_view = 0 OR history_view = 1) AND id IN ({placeholders})"
            );
            let mut stmt = conn.prepare(&sql)?;
            let bindings = ids
                .iter()
                .map(|id| id as &dyn rusqlite::ToSql)
                .collect::<Vec<_>>();
            let rows = stmt.query_map(bindings.as_slice(), integration_memory_row)?;
            let mut items = rows.collect::<Result<Vec<_>, _>>()?;
            items.sort_by_key(|item| {
                item.get("id")
                    .and_then(Value::as_i64)
                    .and_then(|id| ids.iter().position(|candidate| *candidate == id))
                    .unwrap_or(usize::MAX)
            });
            Ok(items)
        })
    }

    pub fn integration_list_memory_options(
        &self,
        query: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<Value>, StorageError> {
        self.with_conn(|conn| {
            let escaped_query = query
                .trim()
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_");
            let pattern = format!("%{escaped_query}%");
            let mut stmt = conn.prepare(
                "SELECT id, summary, category, observed_at
                 FROM timelines
                 WHERE (history_view = 0 OR history_view = 1)
                   AND (?1 = '%%' OR summary LIKE ?1 ESCAPE '\\'
                        OR COALESCE(overview, '') LIKE ?1 ESCAPE '\\')
                 ORDER BY
                    CASE WHEN summary LIKE ?1 ESCAPE '\\' THEN 0 ELSE 1 END,
                    updated_at_ms DESC
                 LIMIT ?2 OFFSET ?3",
            )?;
            let rows = stmt.query_map(params![pattern, limit as i64, offset as i64], |row| {
                Ok(json!({
                    "id": row.get::<_, i64>(0)?,
                    "title": row.get::<_, String>(1)?,
                    "category": row.get::<_, Option<String>>(2)?.unwrap_or_else(|| "memory".to_string()),
                    "observedAt": row.get::<_, Option<i64>>(3)?,
                }))
            })?;
            rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
        })
    }
}

fn integration_memory_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, i64>(0)?,
        "title": row.get::<_, String>(1)?,
        "overview": row.get::<_, Option<String>>(2)?,
        "content": row.get::<_, Option<String>>(3)?,
        "category": row.get::<_, Option<String>>(4)?.unwrap_or_else(|| "memory".to_string()),
        "observedAt": row.get::<_, Option<i64>>(5)?,
        "contentOrigin": row.get::<_, Option<String>>(6)?,
        "updatedAt": row.get::<_, i64>(7)?,
    }))
}

fn read_run_logs(
    conn: &rusqlite::Connection,
    id: &str,
) -> Result<Vec<IntegrationSkillLogEntry>, StorageError> {
    let raw = conn
        .query_row(
            "SELECT logs_json FROM integration_skill_runs WHERE id = ?1",
            [id],
            |row| row.get::<_, String>(0),
        )
        .optional()?
        .ok_or_else(|| StorageError::NotFound(format!("integration skill run {id}")))?;
    Ok(serde_json::from_str(&raw).unwrap_or_default())
}

fn map_run_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<IntegrationSkillRunRecord> {
    let input_summary = row
        .get::<_, String>(4)
        .ok()
        .and_then(|value| serde_json::from_str(&value).ok())
        .unwrap_or_else(|| json!({}));
    let result = row
        .get::<_, Option<String>>(5)?
        .and_then(|value| serde_json::from_str(&value).ok());
    let logs = row
        .get::<_, String>(6)
        .ok()
        .and_then(|value| serde_json::from_str(&value).ok())
        .unwrap_or_default();
    Ok(IntegrationSkillRunRecord {
        id: row.get(0)?,
        skill_id: row.get(1)?,
        mode: row.get(2)?,
        status: row.get(3)?,
        input_summary,
        result,
        logs,
        error_code: row.get(7)?,
        error_message: row.get(8)?,
        created_at_ms: row.get(9)?,
        started_at_ms: row.get(10)?,
        finished_at_ms: row.get(11)?,
    })
}

fn content_overview(content: &str) -> String {
    let compact = content
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with("---"))
        .collect::<Vec<_>>()
        .join(" ");
    compact.chars().take(280).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    #[test]
    fn run_lifecycle_and_incremental_import_are_persisted() {
        let storage = StorageManager::open_in_memory().expect("storage");
        storage
            .create_integration_skill_run("run-1", "obsidian", "execute", &json!({"files": 1}))
            .expect("create run");
        storage
            .start_integration_skill_run("run-1")
            .expect("start run");
        storage
            .finish_integration_skill_run("run-1", &json!({"created": 1}))
            .expect("finish run");
        let run = storage
            .get_integration_skill_run("run-1")
            .expect("get run")
            .expect("run exists");
        assert_eq!(run.status, "succeeded");
        assert!(run.logs.len() >= 3);
        let serialized = serde_json::to_value(&run).expect("serialize run");
        assert_eq!(
            serialized.get("skillId").and_then(Value::as_str),
            Some("obsidian")
        );
        assert!(serialized.get("createdAtMs").is_some());
        assert!(serialized.get("skill_id").is_none());

        let content = "# Alpha\n\nUnique integration content.";
        let item = ImportedKnowledgeItem {
            source_key: "alpha-md".to_string(),
            source_path: "Alpha.md".to_string(),
            title: "Alpha".to_string(),
            content: content.to_string(),
            entities: vec!["test".to_string()],
            metadata: json!({"path": "Alpha.md"}),
            content_hash: format!("{:x}", Sha256::digest(content.as_bytes())),
        };
        assert_eq!(
            storage
                .upsert_integration_import_item("obsidian", &item)
                .expect("first import"),
            ImportWriteOutcome::Created(1)
        );
        assert_eq!(
            storage
                .upsert_integration_import_item("obsidian", &item)
                .expect("second import"),
            ImportWriteOutcome::Unchanged(1)
        );

        let renamed_item = ImportedKnowledgeItem {
            title: "Renamed Alpha".to_string(),
            ..item
        };
        assert_eq!(
            storage
                .upsert_integration_import_item("obsidian", &renamed_item)
                .expect("title-only update"),
            ImportWriteOutcome::Updated(1)
        );
        let matches = storage
            .integration_export_context("Unique integration content", 1)
            .expect("query renamed import");
        assert_eq!(
            matches[0].get("title").and_then(Value::as_str),
            Some("Renamed Alpha")
        );
    }
}
