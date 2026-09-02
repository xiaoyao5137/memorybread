use rusqlite::{params, Connection, OptionalExtension, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreationInlineEditRun {
    pub request_id: String,
    pub session_id: String,
    pub history_id: i64,
    pub operation_fingerprint: String,
    pub action: String,
    pub status: String,
    pub base_revision_no: i64,
    pub base_document_hash: String,
    pub base_content: String,
    pub replacement_markdown: Option<String>,
    pub result_content: Option<String>,
    pub result_hash: Option<String>,
    pub result_revision_no: Option<i64>,
    pub document_patch_json: Option<String>,
    pub error_code: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
}

fn map_run(row: &rusqlite::Row<'_>) -> Result<CreationInlineEditRun> {
    Ok(CreationInlineEditRun {
        request_id: row.get(0)?,
        session_id: row.get(1)?,
        history_id: row.get(2)?,
        operation_fingerprint: row.get(3)?,
        action: row.get(4)?,
        status: row.get(5)?,
        base_revision_no: row.get(6)?,
        base_document_hash: row.get(7)?,
        base_content: row.get(8)?,
        replacement_markdown: row.get(9)?,
        result_content: row.get(10)?,
        result_hash: row.get(11)?,
        result_revision_no: row.get(12)?,
        document_patch_json: row.get(13)?,
        error_code: row.get(14)?,
        created_at: row.get(15)?,
        updated_at: row.get(16)?,
    })
}

const RUN_SELECT: &str = "SELECT request_id, session_id, history_id,
    operation_fingerprint, action, status, base_revision_no, base_document_hash,
    base_content, replacement_markdown, result_content, result_hash,
    result_revision_no, document_patch_json, error_code, created_at, updated_at
    FROM creation_inline_edit_runs";

pub fn get_by_request(
    conn: &Connection,
    request_id: &str,
) -> Result<Option<CreationInlineEditRun>> {
    conn.query_row(
        &format!("{RUN_SELECT} WHERE request_id = ?1 LIMIT 1"),
        params![request_id],
        map_run,
    )
    .optional()
}

pub fn get_active_for_session(
    conn: &Connection,
    session_id: &str,
) -> Result<Option<CreationInlineEditRun>> {
    conn.query_row(
        &format!(
            "{RUN_SELECT} WHERE session_id = ?1
             AND status IN ('running', 'paused', 'candidate_ready', 'committing')
             ORDER BY updated_at DESC LIMIT 1"
        ),
        params![session_id],
        map_run,
    )
    .optional()
}

pub fn cancel_stale_precommit_for_session(
    conn: &Connection,
    session_id: &str,
    stale_before_ms: i64,
) -> Result<usize> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "UPDATE creation_inline_edit_runs
         SET status = 'cancelled', error_code = 'CREATION_INLINE_EDIT_EXPIRED',
             updated_at = ?1
         WHERE session_id = ?2
           AND status IN ('running', 'paused', 'candidate_ready')
           AND updated_at <= ?3",
        params![now, session_id, stale_before_ms],
    )
}

#[allow(clippy::too_many_arguments)]
pub fn insert_running(
    conn: &Connection,
    request_id: &str,
    session_id: &str,
    history_id: i64,
    operation_fingerprint: &str,
    action: &str,
    base_revision_no: i64,
    base_document_hash: &str,
    base_content: &str,
) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "INSERT INTO creation_inline_edit_runs (
            request_id, session_id, history_id, operation_fingerprint, action,
            status, base_revision_no, base_document_hash, base_content,
            created_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, 'running', ?6, ?7, ?8, ?9, ?9)",
        params![
            request_id,
            session_id,
            history_id,
            operation_fingerprint,
            action,
            base_revision_no,
            base_document_hash,
            base_content,
            now,
        ],
    )?;
    Ok(())
}

pub fn set_status(
    conn: &Connection,
    request_id: &str,
    status: &str,
    error_code: Option<&str>,
) -> Result<()> {
    conn.execute(
        "UPDATE creation_inline_edit_runs
         SET status = ?1, error_code = ?2, updated_at = ?3
         WHERE request_id = ?4",
        params![
            status,
            error_code,
            chrono::Utc::now().timestamp_millis(),
            request_id
        ],
    )?;
    Ok(())
}

pub fn set_candidate(
    conn: &Connection,
    request_id: &str,
    replacement_markdown: &str,
) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE creation_inline_edit_runs
         SET status = 'candidate_ready', replacement_markdown = ?1, updated_at = ?2
         WHERE request_id = ?3 AND status IN ('running', 'paused')",
        params![
            replacement_markdown,
            chrono::Utc::now().timestamp_millis(),
            request_id
        ],
    )?;
    Ok(changed == 1)
}

pub fn cancel_if_precommit(conn: &Connection, request_id: &str) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE creation_inline_edit_runs
         SET status = 'cancelled', updated_at = ?1
         WHERE request_id = ?2
           AND status IN ('running', 'paused', 'candidate_ready')",
        params![chrono::Utc::now().timestamp_millis(), request_id],
    )?;
    Ok(changed == 1)
}

#[allow(clippy::too_many_arguments)]
pub fn commit_result(
    conn: &Connection,
    request_id: &str,
    history_id: i64,
    expected_session_id: &str,
    expected_revision_no: i64,
    expected_base_content: &str,
    result_content: &str,
    conversation_json: &str,
    agent_trace_json: &str,
    operation: &str,
    document_patch_json: &str,
    result_hash: &str,
) -> Result<i64> {
    let tx = conn.unchecked_transaction()?;
    let current = super::creation_history::get_by_id(&tx, history_id)?
        .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
    if current.session_id.as_deref() != Some(expected_session_id)
        || current.revision_no != expected_revision_no
        || current.generated_content != expected_base_content
    {
        return Err(rusqlite::Error::InvalidQuery);
    }
    let revision_no = current.revision_no + 1;
    let now = chrono::Utc::now().timestamp_millis();
    let changed = tx.execute(
        "UPDATE creation_inline_edit_runs
         SET status = 'committing', updated_at = ?1
         WHERE request_id = ?2 AND status = 'candidate_ready'",
        params![now, request_id],
    )?;
    if changed != 1 {
        return Err(rusqlite::Error::InvalidQuery);
    }
    tx.execute(
        "UPDATE creation_history
         SET generated_content = ?1,
             conversation_json = ?2,
             agent_trace_json = ?3,
             revision_no = ?4,
             edit_operation = ?5,
             document_patch_json = ?6,
             lifecycle_status = 'completed',
             updated_at = ?7
         WHERE id = ?8",
        params![
            result_content,
            conversation_json,
            agent_trace_json,
            revision_no,
            operation,
            document_patch_json,
            now,
            history_id,
        ],
    )?;
    tx.execute(
        "UPDATE creation_inline_edit_runs
         SET status = 'committed', result_content = ?1, result_hash = ?2,
             result_revision_no = ?3, document_patch_json = ?4, updated_at = ?5
         WHERE request_id = ?6",
        params![
            result_content,
            result_hash,
            revision_no,
            document_patch_json,
            now,
            request_id,
        ],
    )?;
    tx.commit()?;
    Ok(revision_no)
}

pub fn undo_committed(
    conn: &Connection,
    request_id: &str,
    expected_result_hash: &str,
    conversation_json: &str,
    agent_trace_json: &str,
    document_patch_json: &str,
) -> Result<(String, i64)> {
    let tx = conn.unchecked_transaction()?;
    let run = get_by_request(&tx, request_id)?.ok_or(rusqlite::Error::QueryReturnedNoRows)?;
    if run.status != "committed" || run.result_hash.as_deref() != Some(expected_result_hash) {
        return Err(rusqlite::Error::InvalidQuery);
    }
    let history = super::creation_history::get_by_id(&tx, run.history_id)?
        .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
    if history.generated_content != run.result_content.as_deref().unwrap_or_default() {
        return Err(rusqlite::Error::InvalidQuery);
    }
    let revision_no = history.revision_no + 1;
    let now = chrono::Utc::now().timestamp_millis();
    tx.execute(
        "UPDATE creation_history
         SET generated_content = ?1, conversation_json = ?2, agent_trace_json = ?3,
             revision_no = ?4, edit_operation = 'undo_inline_edit',
             document_patch_json = ?5, updated_at = ?6
         WHERE id = ?7",
        params![
            run.base_content,
            conversation_json,
            agent_trace_json,
            revision_no,
            document_patch_json,
            now,
            run.history_id,
        ],
    )?;
    tx.execute(
        "UPDATE creation_inline_edit_runs SET status = 'undone', updated_at = ?1
         WHERE request_id = ?2",
        params![now, request_id],
    )?;
    tx.commit()?;
    Ok((run.base_content, revision_no))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connection() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE creation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT NOT NULL,
                generated_content TEXT NOT NULL, doc_type TEXT, audience TEXT,
                reference_count INTEGER DEFAULT 0, references_json TEXT, model TEXT,
                latency_ms INTEGER, session_id TEXT, conversation_json TEXT,
                agent_trace_json TEXT, goal_json TEXT, root_request TEXT,
                parent_history_id INTEGER, revision_no INTEGER NOT NULL DEFAULT 1,
                edit_operation TEXT NOT NULL DEFAULT 'create_document',
                document_patch_json TEXT, evidence_json TEXT, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, source_kind TEXT NOT NULL DEFAULT 'creation',
                source_ref_id INTEGER, lifecycle_status TEXT NOT NULL DEFAULT 'completed',
                creation_mode TEXT NOT NULL DEFAULT 'direct', creation_brief_json TEXT,
                brainstorm_revision INTEGER, progress_epoch INTEGER NOT NULL DEFAULT 0
             );
             CREATE TABLE creation_inline_edit_runs (
                request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                history_id INTEGER NOT NULL, operation_fingerprint TEXT NOT NULL,
                action TEXT NOT NULL, status TEXT NOT NULL, base_revision_no INTEGER NOT NULL,
                base_document_hash TEXT NOT NULL, base_content TEXT NOT NULL,
                replacement_markdown TEXT, result_content TEXT, result_hash TEXT,
                result_revision_no INTEGER, document_patch_json TEXT, error_code TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
             );",
        )
        .unwrap();
        conn
    }

    #[test]
    fn persists_and_reads_running_request_without_prompt_fields() {
        let conn = connection();
        insert_running(
            &conn,
            "request-1",
            "session-1",
            1,
            "fingerprint",
            "polish",
            2,
            "base-hash",
            "# 文档",
        )
        .unwrap();

        let stored = get_by_request(&conn, "request-1").unwrap().unwrap();
        assert_eq!(stored.status, "running");
        assert_eq!(stored.base_content, "# 文档");
        assert!(stored.replacement_markdown.is_none());
    }

    #[test]
    fn commits_and_undoes_document_with_revision_compare_and_swap() {
        let conn = connection();
        conn.execute(
            "INSERT INTO creation_history (
                prompt, generated_content, reference_count, session_id, revision_no,
                lifecycle_status, created_at, updated_at
             ) VALUES ('初稿', '原文', 0, 'session-1', 3, 'completed', 1, 1)",
            [],
        )
        .unwrap();
        let history_id = conn.last_insert_rowid();
        insert_running(
            &conn,
            "request-1",
            "session-1",
            history_id,
            "fingerprint",
            "polish",
            3,
            "base-hash",
            "原文",
        )
        .unwrap();
        assert!(set_candidate(&conn, "request-1", "润色后").unwrap());

        let revision = commit_result(
            &conn,
            "request-1",
            history_id,
            "session-1",
            3,
            "原文",
            "润色后",
            "[]",
            "[]",
            "polish_selection",
            "{}",
            "result-hash",
        )
        .unwrap();
        assert_eq!(revision, 4);
        let history = super::super::creation_history::get_by_id(&conn, history_id)
            .unwrap()
            .unwrap();
        assert_eq!(history.generated_content, "润色后");
        assert_eq!(history.prompt, "初稿");
        assert_eq!(history.edit_operation, "polish_selection");
        assert_eq!(
            get_by_request(&conn, "request-1").unwrap().unwrap().status,
            "committed"
        );

        let (restored, undo_revision) =
            undo_committed(&conn, "request-1", "result-hash", "[]", "[]", "{}").unwrap();
        assert_eq!(restored, "原文");
        assert_eq!(undo_revision, 5);
        let history = super::super::creation_history::get_by_id(&conn, history_id)
            .unwrap()
            .unwrap();
        assert_eq!(history.generated_content, "原文");
        assert_eq!(history.edit_operation, "undo_inline_edit");
        assert_eq!(
            get_by_request(&conn, "request-1").unwrap().unwrap().status,
            "undone"
        );
    }

    #[test]
    fn cancels_stale_precommit_runs_without_touching_fresh_or_committing_runs() {
        let conn = connection();
        for (request_id, status, updated_at) in [
            ("stale-running", "running", 100_i64),
            ("stale-paused", "paused", 101_i64),
            ("stale-candidate", "candidate_ready", 102_i64),
            ("fresh-paused", "paused", 300_i64),
            ("stale-committing", "committing", 99_i64),
        ] {
            conn.execute(
                "INSERT INTO creation_inline_edit_runs (
                    request_id, session_id, history_id, operation_fingerprint, action,
                    status, base_revision_no, base_document_hash, base_content,
                    created_at, updated_at
                 ) VALUES (?1, 'session-1', 1, 'fingerprint', 'expand', ?2, 1,
                           'base-hash', '原文', ?3, ?3)",
                params![request_id, status, updated_at],
            )
            .unwrap();
        }

        assert_eq!(
            cancel_stale_precommit_for_session(&conn, "session-1", 200).unwrap(),
            3
        );
        for request_id in ["stale-running", "stale-paused", "stale-candidate"] {
            let run = get_by_request(&conn, request_id).unwrap().unwrap();
            assert_eq!(run.status, "cancelled");
            assert_eq!(
                run.error_code.as_deref(),
                Some("CREATION_INLINE_EDIT_EXPIRED")
            );
        }
        assert_eq!(
            get_by_request(&conn, "fresh-paused")
                .unwrap()
                .unwrap()
                .status,
            "paused"
        );
        assert_eq!(
            get_by_request(&conn, "stale-committing")
                .unwrap()
                .unwrap()
                .status,
            "committing"
        );
    }
}
