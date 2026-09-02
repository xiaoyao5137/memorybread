use rusqlite::{params, Connection, OptionalExtension, Result};
use serde::{Deserialize, Serialize};

const HISTORY_SELECT: &str = "SELECT id, prompt, generated_content, doc_type, audience,
    reference_count, references_json, model, latency_ms, session_id, conversation_json,
    agent_trace_json, goal_json, root_request, parent_history_id, revision_no,
    edit_operation, document_patch_json, evidence_json, created_at, updated_at,
    source_kind, source_ref_id, lifecycle_status,
    creation_mode, creation_brief_json, brainstorm_revision, progress_epoch
    FROM creation_history ch";

const LATEST_SESSION_PREDICATE: &str = "(COALESCE(TRIM(ch.session_id), '') = ''
    OR NOT EXISTS (
        SELECT 1
        FROM creation_history newer
        WHERE newer.session_id = ch.session_id
          AND (
              newer.revision_no > ch.revision_no
              OR (newer.revision_no = ch.revision_no AND newer.updated_at > ch.updated_at)
              OR (
                  newer.revision_no = ch.revision_no
                  AND newer.updated_at = ch.updated_at
                  AND newer.created_at > ch.created_at
              )
              OR (
                  newer.revision_no = ch.revision_no
                  AND newer.updated_at = ch.updated_at
                  AND newer.created_at = ch.created_at
                  AND newer.id > ch.id
              )
          )
    ))";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreationHistory {
    pub id: i64,
    pub prompt: String,
    pub generated_content: String,
    pub doc_type: Option<String>,
    pub audience: Option<String>,
    pub reference_count: i64,
    #[serde(default)]
    pub references_json: Option<String>,
    pub model: Option<String>,
    pub latency_ms: Option<i64>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub conversation_json: Option<String>,
    #[serde(default)]
    pub agent_trace_json: Option<String>,
    #[serde(default)]
    pub goal_json: Option<String>,
    #[serde(default)]
    pub root_request: Option<String>,
    #[serde(default)]
    pub parent_history_id: Option<i64>,
    #[serde(default = "default_revision_no")]
    pub revision_no: i64,
    #[serde(default = "default_edit_operation")]
    pub edit_operation: String,
    #[serde(default)]
    pub document_patch_json: Option<String>,
    #[serde(default)]
    pub evidence_json: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
    /// 记录来源：creation 手动创作（默认）/ scheduled_task 定时任务执行。
    #[serde(default = "default_source_kind")]
    pub source_kind: String,
    #[serde(default)]
    pub source_ref_id: Option<i64>,
    #[serde(default = "default_lifecycle_status")]
    pub lifecycle_status: String,
    #[serde(default = "default_creation_mode")]
    pub creation_mode: String,
    #[serde(default)]
    pub creation_brief_json: Option<String>,
    #[serde(default)]
    pub brainstorm_revision: Option<i64>,
    #[serde(default)]
    pub progress_epoch: i64,
}

#[derive(Debug, Clone)]
pub struct CreationSessionContext {
    pub root_request: String,
    pub latest: CreationHistory,
}

pub fn insert(
    conn: &Connection,
    prompt: &str,
    content: &str,
    doc_type: Option<&str>,
    audience: Option<&str>,
    ref_count: i64,
    references_json: Option<&str>,
    model: Option<&str>,
    latency_ms: Option<i64>,
    session_id: Option<&str>,
    conversation_json: Option<&str>,
    agent_trace_json: Option<&str>,
    goal_json: Option<&str>,
    root_request: Option<&str>,
    parent_history_id: Option<i64>,
    revision_no: i64,
    edit_operation: &str,
    document_patch_json: Option<&str>,
) -> Result<i64> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "INSERT INTO creation_history (
            prompt, generated_content, doc_type, audience, reference_count,
            references_json, model, latency_ms, session_id, conversation_json,
            agent_trace_json, goal_json, root_request, parent_history_id, revision_no,
            edit_operation, document_patch_json, created_at, updated_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params![
            prompt,
            content,
            doc_type,
            audience,
            ref_count,
            references_json,
            model,
            latency_ms,
            session_id,
            conversation_json,
            agent_trace_json,
            goal_json,
            root_request,
            parent_history_id,
            revision_no.max(1),
            edit_operation,
            document_patch_json,
            now,
            now
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

#[allow(clippy::too_many_arguments)]
pub fn update_session(
    conn: &Connection,
    history_id: i64,
    prompt: &str,
    content: &str,
    doc_type: Option<&str>,
    audience: Option<&str>,
    ref_count: i64,
    references_json: Option<&str>,
    model: Option<&str>,
    latency_ms: Option<i64>,
    session_id: &str,
    conversation_json: Option<&str>,
    agent_trace_json: Option<&str>,
    goal_json: Option<&str>,
    root_request: &str,
    revision_no: i64,
    edit_operation: &str,
    document_patch_json: Option<&str>,
) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "UPDATE creation_history
         SET prompt = ?1,
             generated_content = ?2,
             doc_type = ?3,
             audience = ?4,
             reference_count = ?5,
             references_json = ?6,
             model = ?7,
             latency_ms = ?8,
             session_id = ?9,
             conversation_json = ?10,
             agent_trace_json = ?11,
             goal_json = ?12,
             root_request = ?13,
             parent_history_id = NULL,
             revision_no = ?14,
             edit_operation = ?15,
             document_patch_json = ?16,
             updated_at = ?17
         WHERE id = ?18",
        params![
            prompt,
            content,
            doc_type,
            audience,
            ref_count,
            references_json,
            model,
            latency_ms,
            session_id,
            conversation_json,
            agent_trace_json,
            goal_json,
            root_request,
            revision_no.max(1),
            edit_operation,
            document_patch_json,
            now,
            history_id,
        ],
    )?;
    Ok(())
}

pub fn list_recent(conn: &Connection, limit: i64) -> Result<Vec<CreationHistory>> {
    if limit <= 0 {
        return Ok(Vec::new());
    }
    list_page(conn, None, limit as usize, 0).map(|(items, _)| items)
}

pub fn get_by_id(conn: &Connection, history_id: i64) -> Result<Option<CreationHistory>> {
    conn.query_row(
        &format!("{HISTORY_SELECT} WHERE ch.id = ?1 LIMIT 1"),
        params![history_id],
        map_history_row,
    )
    .optional()
}

pub fn list_page(
    conn: &Connection,
    query: Option<&str>,
    limit: usize,
    offset: usize,
) -> Result<(Vec<CreationHistory>, usize)> {
    let query = query.map(str::trim).filter(|value| !value.is_empty());

    if let Some(query) = query {
        let predicate = "(instr(lower(COALESCE(ch.prompt, '')), lower(?1)) > 0
                          OR instr(lower(COALESCE(ch.root_request, '')), lower(?1)) > 0
                          OR instr(lower(COALESCE(ch.conversation_json, '')), lower(?1)) > 0
                          OR instr(lower(COALESCE(ch.generated_content, '')), lower(?1)) > 0
                          OR instr(lower(COALESCE(ch.doc_type, '')), lower(?1)) > 0
                          OR instr(lower(COALESCE(ch.audience, '')), lower(?1)) > 0)";
        let total = conn.query_row(
            &format!(
                "SELECT COUNT(*) FROM creation_history ch
                 WHERE {LATEST_SESSION_PREDICATE} AND {predicate}"
            ),
            params![query],
            |row| row.get::<_, i64>(0),
        )?;
        let mut stmt = conn.prepare(&format!(
            "{HISTORY_SELECT}
             WHERE {LATEST_SESSION_PREDICATE} AND {predicate}
             ORDER BY ch.updated_at DESC, ch.created_at DESC, ch.id DESC
             LIMIT ?2 OFFSET ?3"
        ))?;
        let rows = stmt.query_map(params![query, limit as i64, offset as i64], map_history_row)?;
        Ok((rows.collect::<Result<Vec<_>>>()?, total.max(0) as usize))
    } else {
        let total = conn.query_row(
            &format!("SELECT COUNT(*) FROM creation_history ch WHERE {LATEST_SESSION_PREDICATE}"),
            [],
            |row| row.get::<_, i64>(0),
        )?;
        let mut stmt = conn.prepare(&format!(
            "{HISTORY_SELECT}
             WHERE {LATEST_SESSION_PREDICATE}
             ORDER BY ch.updated_at DESC, ch.created_at DESC, ch.id DESC
             LIMIT ?1 OFFSET ?2"
        ))?;
        let rows = stmt.query_map(params![limit as i64, offset as i64], map_history_row)?;
        Ok((rows.collect::<Result<Vec<_>>>()?, total.max(0) as usize))
    }
}

pub fn get_session_context(
    conn: &Connection,
    session_id: &str,
) -> Result<Option<CreationSessionContext>> {
    let session_id = session_id.trim();
    if session_id.is_empty() {
        return Ok(None);
    }
    let latest = conn
        .query_row(
            &format!(
                "{HISTORY_SELECT}
                 WHERE ch.session_id = ?1
                 ORDER BY ch.revision_no DESC, ch.updated_at DESC, ch.created_at DESC, ch.id DESC
                 LIMIT 1"
            ),
            params![session_id],
            map_history_row,
        )
        .optional()?;
    let Some(latest) = latest else {
        return Ok(None);
    };
    let root_request = latest
        .root_request
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .or_else(|| {
            conn.query_row(
                "SELECT prompt
                 FROM creation_history
                 WHERE session_id = ?1
                 ORDER BY revision_no ASC, created_at ASC, id ASC
                 LIMIT 1",
                params![session_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .ok()
            .flatten()
        })
        .unwrap_or_else(|| latest.prompt.clone());
    Ok(Some(CreationSessionContext {
        root_request,
        latest,
    }))
}

pub fn next_revision_no(conn: &Connection, session_id: &str) -> Result<i64> {
    let (max_revision, count): (i64, i64) = conn.query_row(
        "SELECT COALESCE(MAX(revision_no), 0), COUNT(*)
         FROM creation_history
         WHERE session_id = ?1",
        params![session_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    Ok(max_revision.max(count) + 1)
}

pub fn set_evidence_json(conn: &Connection, history_id: i64, evidence_json: &str) -> Result<()> {
    conn.execute(
        "UPDATE creation_history SET evidence_json = ?2 WHERE id = ?1",
        params![history_id, evidence_json],
    )?;
    Ok(())
}

/// 标记创作记录来源（如定时任务执行），供创作页徽标与任务页跳转。
pub fn set_source(
    conn: &Connection,
    history_id: i64,
    source_kind: &str,
    source_ref_id: Option<i64>,
) -> Result<()> {
    conn.execute(
        "UPDATE creation_history SET source_kind = ?2, source_ref_id = ?3 WHERE id = ?1",
        params![history_id, source_kind, source_ref_id],
    )?;
    Ok(())
}

pub fn set_lifecycle_status(
    conn: &Connection,
    history_id: i64,
    lifecycle_status: &str,
) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "UPDATE creation_history
         SET lifecycle_status = ?2, updated_at = ?3
         WHERE id = ?1",
        params![history_id, lifecycle_status, now],
    )?;
    Ok(())
}

pub fn set_brainstorm_metadata(
    conn: &Connection,
    history_id: i64,
    creation_mode: &str,
    creation_brief_json: Option<&str>,
    brainstorm_revision: Option<i64>,
) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "UPDATE creation_history
         SET creation_mode = ?2,
             creation_brief_json = COALESCE(?3, creation_brief_json),
             brainstorm_revision = COALESCE(?4, brainstorm_revision),
             updated_at = ?5
         WHERE id = ?1",
        params![
            history_id,
            creation_mode,
            creation_brief_json,
            brainstorm_revision,
            now,
        ],
    )?;
    Ok(())
}

pub fn update_progress(
    conn: &Connection,
    history_id: i64,
    lifecycle_status: &str,
    generated_content: Option<&str>,
    conversation_json: Option<&str>,
    agent_trace_json: Option<&str>,
    latency_ms: Option<i64>,
    progress_epoch: Option<i64>,
) -> Result<bool> {
    let now = chrono::Utc::now().timestamp_millis();
    let changed = conn.execute(
        "UPDATE creation_history
         SET lifecycle_status = ?2,
             generated_content = COALESCE(?3, generated_content),
             conversation_json = COALESCE(?4, conversation_json),
             agent_trace_json = COALESCE(?5, agent_trace_json),
             latency_ms = COALESCE(?6, latency_ms),
             updated_at = ?7
         WHERE id = ?1
           AND (?8 IS NULL OR progress_epoch = ?8)
           AND NOT (
             ?2 = 'running'
             AND lifecycle_status IN ('completed', 'failed', 'cancelled')
           )",
        params![
            history_id,
            lifecycle_status,
            generated_content,
            conversation_json,
            agent_trace_json,
            latency_ms,
            now,
            progress_epoch,
        ],
    )?;
    if changed > 0 {
        return Ok(true);
    }

    // A late in-flight snapshot is an accepted no-op after a terminal write. Return true so the
    // API does not misreport an existing record as missing, while preserving the terminal state,
    // document, conversation and trace atomically.
    let exists = conn
        .query_row(
            "SELECT 1 FROM creation_history WHERE id = ?1",
            params![history_id],
            |_| Ok(()),
        )
        .optional()?
        .is_some();
    Ok(exists)
}

/// Explicitly starts a new run on an existing history record.
///
/// Unlike `update_progress`, this operation may reopen a terminal record and increments the
/// persisted epoch. Subsequent snapshots must carry that epoch, so an older run cannot overwrite
/// the new run even if its request arrives after this transition.
pub fn start_progress(
    conn: &Connection,
    history_id: i64,
    generated_content: Option<&str>,
    conversation_json: Option<&str>,
) -> Result<Option<i64>> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.query_row(
        "UPDATE creation_history
         SET lifecycle_status = 'running',
             progress_epoch = progress_epoch + 1,
             generated_content = COALESCE(?2, generated_content),
             conversation_json = COALESCE(?3, conversation_json),
             updated_at = ?4
         WHERE id = ?1
         RETURNING progress_epoch",
        params![history_id, generated_content, conversation_json, now],
        |row| row.get(0),
    )
    .optional()
}

fn map_history_row(row: &rusqlite::Row<'_>) -> Result<CreationHistory> {
    Ok(CreationHistory {
        id: row.get(0)?,
        prompt: row.get(1)?,
        generated_content: row.get(2)?,
        doc_type: row.get(3)?,
        audience: row.get(4)?,
        reference_count: row.get(5)?,
        references_json: row.get(6)?,
        model: row.get(7)?,
        latency_ms: row.get(8)?,
        session_id: row.get(9)?,
        conversation_json: row.get(10)?,
        agent_trace_json: row.get(11)?,
        goal_json: row.get(12)?,
        root_request: row.get(13)?,
        parent_history_id: row.get(14)?,
        revision_no: row.get(15)?,
        edit_operation: row.get(16)?,
        document_patch_json: row.get(17)?,
        evidence_json: row.get(18)?,
        created_at: row.get(19)?,
        updated_at: row.get(20)?,
        source_kind: row
            .get::<_, Option<String>>(21)?
            .unwrap_or_else(default_source_kind),
        source_ref_id: row.get(22)?,
        lifecycle_status: row
            .get::<_, Option<String>>(23)?
            .unwrap_or_else(default_lifecycle_status),
        creation_mode: row
            .get::<_, Option<String>>(24)?
            .unwrap_or_else(default_creation_mode),
        creation_brief_json: row.get(25)?,
        brainstorm_revision: row.get(26)?,
        progress_epoch: row.get(27)?,
    })
}

fn default_revision_no() -> i64 {
    1
}

fn default_source_kind() -> String {
    "creation".to_string()
}

fn default_lifecycle_status() -> String {
    "completed".to_string()
}

fn default_edit_operation() -> String {
    "create_document".to_string()
}

fn default_creation_mode() -> String {
    "direct".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connection() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE creation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT NOT NULL,
                generated_content TEXT NOT NULL,
                doc_type TEXT,
                audience TEXT,
                reference_count INTEGER DEFAULT 0,
                references_json TEXT,
                model TEXT,
                latency_ms INTEGER,
                session_id TEXT,
                conversation_json TEXT,
                agent_trace_json TEXT,
                goal_json TEXT,
                root_request TEXT,
                parent_history_id INTEGER,
                revision_no INTEGER NOT NULL DEFAULT 1,
                edit_operation TEXT NOT NULL DEFAULT 'create_document',
                document_patch_json TEXT,
                evidence_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'creation',
                source_ref_id INTEGER
                , lifecycle_status TEXT NOT NULL DEFAULT 'completed'
                , creation_mode TEXT NOT NULL DEFAULT 'direct'
                , creation_brief_json TEXT
                , brainstorm_revision INTEGER
                , progress_epoch INTEGER NOT NULL DEFAULT 0
            );",
        )
        .unwrap();
        conn
    }

    #[test]
    fn search_and_paginate_history() {
        let conn = connection();
        for (index, (prompt, content)) in [
            ("年度方案", "第一版"),
            ("项目复盘", "包含年度目标"),
            ("技术文档", "普通内容"),
        ]
        .into_iter()
        .enumerate()
        {
            conn.execute(
                "INSERT INTO creation_history
                 (prompt, generated_content, reference_count, created_at, updated_at)
                 VALUES (?1, ?2, 0, ?3, ?3)",
                params![prompt, content, index as i64],
            )
            .unwrap();
        }

        let (first_page, total) = list_page(&conn, Some("年度"), 1, 0).unwrap();
        assert_eq!(total, 2);
        assert_eq!(first_page.len(), 1);

        let (second_page, total) = list_page(&conn, Some("年度"), 1, 1).unwrap();
        assert_eq!(total, 2);
        assert_eq!(second_page.len(), 1);
        assert_ne!(first_page[0].id, second_page[0].id);
    }

    #[test]
    fn persists_agent_session_conversation_and_trace() {
        let conn = connection();
        let id = insert(
            &conn,
            "生成架构方案",
            "# 架构方案",
            Some("架构设计方案"),
            Some("研发团队"),
            1,
            Some(r#"[{"id":1}]"#),
            Some("mbcd-std-v1"),
            Some(1200),
            Some("session-1"),
            Some(r#"[{"role":"user","content":"生成架构方案"}]"#),
            Some(r#"[{"type":"agent.completed"}]"#),
            Some(r#"{"status":"complete","revision":6}"#),
            Some("生成架构方案"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();
        set_evidence_json(
            &conn,
            id,
            r#"[{"id":"evidence-1","validation_status":"verified"}]"#,
        )
        .unwrap();

        let (items, total) = list_page(&conn, None, 20, 0).unwrap();
        assert_eq!(total, 1);
        assert_eq!(items[0].id, id);
        assert_eq!(items[0].session_id.as_deref(), Some("session-1"));
        assert!(items[0]
            .conversation_json
            .as_deref()
            .unwrap()
            .contains("生成架构方案"));
        assert!(items[0]
            .agent_trace_json
            .as_deref()
            .unwrap()
            .contains("agent.completed"));
        assert!(items[0]
            .evidence_json
            .as_deref()
            .unwrap()
            .contains("evidence-1"));
    }

    #[test]
    fn persists_brainstorm_mode_brief_and_consumed_revision() {
        let conn = connection();
        let id = insert(
            &conn,
            "设计数据治理平台方案",
            "# 数据治理平台方案",
            None,
            None,
            0,
            Some("[]"),
            None,
            None,
            Some("session-brainstorm-history"),
            None,
            Some("[]"),
            None,
            Some("设计数据治理平台方案"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();

        set_brainstorm_metadata(
            &conn,
            id,
            "brainstorm",
            Some(r#"{"revision":4,"phase":"ready"}"#),
            Some(4),
        )
        .unwrap();

        let (items, total) = list_page(&conn, None, 20, 0).unwrap();
        assert_eq!(total, 1);
        assert_eq!(items[0].creation_mode, "brainstorm");
        assert_eq!(items[0].brainstorm_revision, Some(4));
        assert!(items[0]
            .creation_brief_json
            .as_deref()
            .unwrap()
            .contains("\"phase\":\"ready\""));
    }

    #[test]
    fn restores_root_request_and_latest_revision_for_session() {
        let conn = connection();
        let first_id = insert(
            &conn,
            "生成新能源行业方案",
            "# 新能源行业方案\n\n## 目标\n\n首版",
            None,
            None,
            0,
            Some("[]"),
            None,
            None,
            Some("session-2"),
            Some(r#"[{"role":"user","content":"生成新能源行业方案"}]"#),
            Some("[]"),
            None,
            Some("生成新能源行业方案"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();
        let second_id = insert(
            &conn,
            "补充行业调研",
            "# 新能源行业方案\n\n## 目标\n\n首版\n\n## 行业调研\n\n增量",
            None,
            None,
            0,
            Some("[]"),
            None,
            None,
            Some("session-2"),
            Some(r#"[{"role":"user","content":"补充行业调研"}]"#),
            Some("[]"),
            None,
            Some("生成新能源行业方案"),
            Some(first_id),
            2,
            "append_section",
            Some(r#"{"target_sections":["行业调研"]}"#),
        )
        .unwrap();

        let context = get_session_context(&conn, "session-2").unwrap().unwrap();
        assert_eq!(context.root_request, "生成新能源行业方案");
        assert_eq!(context.latest.id, second_id);
        assert_eq!(context.latest.revision_no, 2);
        assert_eq!(context.latest.parent_history_id, Some(first_id));
        assert_eq!(context.latest.edit_operation, "append_section");
        assert_eq!(next_revision_no(&conn, "session-2").unwrap(), 3);

        let (items, total) = list_page(&conn, None, 20, 0).unwrap();
        assert_eq!(total, 1);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].id, second_id);
        assert!(items[0].generated_content.contains("行业调研"));
    }

    #[test]
    fn updates_the_same_history_row_for_a_complete_session() {
        let conn = connection();
        let id = insert(
            &conn,
            "生成新能源行业方案",
            "# 新能源行业方案",
            Some("行业方案"),
            Some("管理层"),
            0,
            Some("[]"),
            Some("mbcd-std-v1"),
            Some(800),
            Some("session-3"),
            Some(r#"[{"role":"user","content":"生成新能源行业方案"}]"#),
            Some("[]"),
            None,
            Some("生成新能源行业方案"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();

        update_session(
            &conn,
            id,
            "生成新能源行业方案",
            "# 新能源行业方案\n\n## 风险\n\n已补充",
            Some("行业方案"),
            Some("管理层"),
            1,
            Some(r#"[{"id":1}]"#),
            Some("mbcd-plus-v1"),
            Some(1200),
            "session-3",
            Some(
                r#"[{"role":"user","content":"生成新能源行业方案"},{"role":"user","content":"补充风险"}]"#,
            ),
            Some(r#"[{"type":"run.completed"}]"#),
            None,
            "生成新能源行业方案",
            2,
            "append_section",
            Some(r#"{"target_sections":["风险"]}"#),
        )
        .unwrap();

        let row_count = conn
            .query_row("SELECT COUNT(*) FROM creation_history", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        assert_eq!(row_count, 1);

        let (items, total) = list_page(&conn, Some("补充风险"), 20, 0).unwrap();
        assert_eq!(total, 1);
        assert_eq!(items[0].id, id);
        assert_eq!(items[0].prompt, "生成新能源行业方案");
        assert_eq!(items[0].revision_no, 2);
        assert_eq!(items[0].parent_history_id, None);
        assert!(items[0].generated_content.contains("已补充"));
    }

    #[test]
    fn set_source_marks_history_as_scheduled_task() {
        let conn = connection();
        let id = insert(
            &conn,
            "每日晨报",
            "# 晨报",
            None,
            None,
            0,
            None,
            None,
            Some(500),
            Some("session-task-1-9"),
            None,
            Some(r#"[{"type":"run.completed"}]"#),
            None,
            Some("每日晨报"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();
        set_source(&conn, id, "scheduled_task", Some(7)).unwrap();

        let fetched = get_by_id(&conn, id).unwrap().unwrap();
        assert_eq!(fetched.source_kind, "scheduled_task");
        assert_eq!(fetched.source_ref_id, Some(7));

        let (items, total) = list_page(&conn, None, 20, 0).unwrap();
        assert_eq!(total, 1);
        assert_eq!(items[0].source_kind, "scheduled_task");
    }

    #[test]
    fn tracks_running_progress_and_terminal_status_on_the_same_record() {
        let conn = connection();
        let id = insert(
            &conn,
            "生成后台方案",
            "",
            None,
            None,
            0,
            Some("[]"),
            None,
            None,
            Some("running-session"),
            Some(r#"[{"role":"user","content":"生成后台方案"}]"#),
            Some("[]"),
            None,
            Some("生成后台方案"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();

        let first_epoch = start_progress(&conn, id, None, None).unwrap().unwrap();
        assert_eq!(first_epoch, 1);
        update_progress(
            &conn,
            id,
            "running",
            Some("# 已完成的部分"),
            None,
            Some(r#"[{"type":"phase.started","status":"running"}]"#),
            Some(850),
            Some(first_epoch),
        )
        .unwrap();
        let running = get_by_id(&conn, id).unwrap().unwrap();
        assert_eq!(running.lifecycle_status, "running");
        assert_eq!(running.generated_content, "# 已完成的部分");
        assert!(running.agent_trace_json.unwrap().contains("phase.started"));

        update_progress(
            &conn,
            id,
            "completed",
            Some("# 最终方案"),
            None,
            Some(r#"[{"type":"run.completed","status":"completed"}]"#),
            Some(1200),
            Some(first_epoch),
        )
        .unwrap();
        let completed = get_by_id(&conn, id).unwrap().unwrap();
        assert_eq!(completed.lifecycle_status, "completed");
        assert_eq!(completed.generated_content, "# 最终方案");
        assert_eq!(completed.latency_ms, Some(1200));

        let accepted = update_progress(
            &conn,
            id,
            "running",
            Some("# 迟到的中间版本"),
            None,
            Some(r#"[{"type":"phase.started","status":"running"}]"#),
            Some(1800),
            Some(first_epoch),
        )
        .unwrap();
        assert!(accepted);
        let still_completed = get_by_id(&conn, id).unwrap().unwrap();
        assert_eq!(still_completed.lifecycle_status, "completed");
        assert_eq!(still_completed.generated_content, "# 最终方案");
        assert_eq!(still_completed.latency_ms, Some(1200));
        assert!(still_completed
            .agent_trace_json
            .as_deref()
            .unwrap()
            .contains("run.completed"));

        let restarted = start_progress(
            &conn,
            id,
            Some("# 第二轮草稿"),
            Some(r#"[{"role":"user","content":"继续修改"}]"#),
        )
        .unwrap()
        .unwrap();
        assert_eq!(restarted, 2);

        let running_again = get_by_id(&conn, id).unwrap().unwrap();
        assert_eq!(running_again.lifecycle_status, "running");
        assert_eq!(running_again.generated_content, "# 第二轮草稿");
        assert_eq!(
            running_again.conversation_json.as_deref(),
            Some(r#"[{"role":"user","content":"继续修改"}]"#)
        );
        assert!(running_again
            .agent_trace_json
            .as_deref()
            .unwrap()
            .contains("run.completed"));

        let stale_previous_run = update_progress(
            &conn,
            id,
            "running",
            Some("# 第一轮迟到版本"),
            None,
            Some(r#"[{"type":"phase.started","status":"running"}]"#),
            Some(2200),
            Some(first_epoch),
        )
        .unwrap();
        assert!(stale_previous_run);

        let still_second_run = get_by_id(&conn, id).unwrap().unwrap();
        assert_eq!(still_second_run.progress_epoch, 2);
        assert_eq!(still_second_run.lifecycle_status, "running");
        assert_eq!(still_second_run.generated_content, "# 第二轮草稿");
        assert_eq!(still_second_run.latency_ms, Some(1200));
    }
}
