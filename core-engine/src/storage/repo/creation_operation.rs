//! Durable instruction execution. Checkpoints are owned by Core, not the model.
use rusqlite::{params, Connection, OptionalExtension, Result};
use serde_json::{json, Value};

#[derive(Debug, Clone)]
pub struct Operation {
    pub id: String,
    pub instruction: String,
    pub history_id: i64,
    pub base_revision: i64,
    pub base_document: String,
    pub original_document: String,
    pub checkpoint: Option<Value>,
    pub status: String,
    pub result: Option<Value>,
}

pub fn get(conn: &Connection, session: &str, id: &str) -> Result<Option<Operation>> {
    conn.query_row(
        "SELECT operation_id,instruction,history_id,base_revision,base_document,
         checkpoint_json,status,result_json,original_document FROM creation_operations
         WHERE session_id=?1 AND operation_id=?2",
        params![session, id],
        |row| {
            Ok(Operation {
                id: row.get(0)?,
                instruction: row.get(1)?,
                history_id: row.get(2)?,
                base_revision: row.get(3)?,
                base_document: row.get(4)?,
                checkpoint: row
                    .get::<_, Option<String>>(5)?
                    .and_then(|s| serde_json::from_str(&s).ok()),
                status: row.get(6)?,
                original_document: row.get(8)?,
                result: row
                    .get::<_, Option<String>>(7)?
                    .and_then(|s| serde_json::from_str(&s).ok()),
            })
        },
    )
    .optional()
}

pub fn pending(conn: &Connection, session: &str, exclude: &str) -> Result<Vec<Value>> {
    let mut stmt = conn.prepare(
        "SELECT o.operation_id,o.instruction,o.status FROM creation_operations o
         JOIN creation_history h ON h.id=o.history_id
         WHERE o.session_id=?1 AND o.operation_id<>?2
         AND o.status IN ('running','waiting','failed','partial')
         AND ((o.base_revision=h.revision_no AND o.base_document=h.generated_content)
              OR (o.status='failed' AND h.lifecycle_status='failed'
                  AND h.revision_no=o.base_revision+1
                  AND json_extract(o.checkpoint_json,'$.current_document')=h.generated_content))
         ORDER BY o.updated_at DESC LIMIT 12")?;
    let rows = stmt.query_map(params![session, exclude], |row| {
        Ok(json!({
            "operation_id":row.get::<_,String>(0)?, "instruction":row.get::<_,String>(1)?,
            "status":row.get::<_,String>(2)?
        }))
    })?;
    rows.collect()
}

pub fn undo_candidates(conn: &Connection, session: &str, revision: i64) -> Result<Vec<Value>> {
    let mut stmt = conn.prepare("SELECT operation_id,instruction FROM creation_operations
        WHERE session_id=?1 AND status='completed'
        AND json_extract(result_json,'$.data.revision_no')=?2
        AND original_document<>json_extract(result_json,'$.data.document') ORDER BY updated_at DESC LIMIT 6")?;
    let rows = stmt.query_map(params![session, revision], |row| {
        Ok(json!({
            "operation_id":row.get::<_,String>(0)?,"instruction":row.get::<_,String>(1)?
        }))
    })?;
    rows.collect()
}

/// Legacy conversations may have persisted instructions without execution state.
/// Preserve all unanswered candidates; the interpreter resolves their meaning.
pub fn seed_legacy_pending(
    conn: &Connection,
    session: &str,
    current_instruction: &str,
    history: &super::creation_history::CreationHistory,
) -> Result<()> {
    let has_operations: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM creation_operations WHERE session_id=?1)",
        params![session],
        |row| row.get(0),
    )?;
    if has_operations {
        return Ok(());
    }
    let conversation: Vec<Value> = history
        .conversation_json
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_default();
    let start = conversation
        .iter()
        .rposition(|item| item["role"] == "assistant")
        .map(|i| i + 1)
        .unwrap_or(0);
    for (index, item) in conversation.iter().enumerate().skip(start).take(12) {
        if item["role"] != "user"
            || matches!(item["kind"].as_str(), Some("user_abort" | "session_end"))
        {
            continue;
        }
        let fallback = format!("legacy-{index}");
        let instruction_id = item["id"].as_str().unwrap_or(&fallback);
        if instruction_id == current_instruction {
            continue;
        }
        if let Some(instruction) = item["content"].as_str().filter(|s| !s.trim().is_empty()) {
            let id = format!("legacy-operation-{session}-{instruction_id}");
            begin(conn, session, &id, instruction_id, instruction, history)?;
        }
    }
    Ok(())
}

pub fn begin(
    conn: &Connection,
    session: &str,
    id: &str,
    instruction_id: &str,
    instruction: &str,
    history: &super::creation_history::CreationHistory,
) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute("INSERT OR IGNORE INTO creation_operations
        (operation_id,session_id,instruction_id,instruction,history_id,base_revision,base_document,original_document,created_at,updated_at)
        VALUES (?1,?2,?3,?4,?5,?6,?7,?7,?8,?8)", params![id,session,instruction_id,instruction,
            history.id,history.revision_no,history.generated_content,now])?;
    let existing = get(conn, session, id)?.ok_or(rusqlite::Error::InvalidQuery)?;
    if existing.instruction != instruction || existing.history_id != history.id {
        return Err(rusqlite::Error::InvalidQuery);
    }
    Ok(())
}

/// Adopt a failed candidate saved by the client only when it is exactly the
/// next revision and matches the durable checkpoint. Preserve the undo base.
pub fn get_for_resume(conn: &Connection, session: &str, id: &str) -> Result<Option<Operation>> {
    let tx = conn.unchecked_transaction()?;
    let mut operation = match get(&tx, session, id)? {
        Some(operation) => operation,
        None => return Ok(None),
    };
    let current = super::creation_history::get_by_id(&tx, operation.history_id)?;
    if let (Some(current), Some(checkpoint)) = (current, operation.checkpoint.as_ref()) {
        if operation.status == "failed"
            && current.lifecycle_status == "failed"
            && current.session_id.as_deref() == Some(session)
            && current.revision_no == operation.base_revision + 1
            && checkpoint["current_document"].as_str() == Some(current.generated_content.as_str())
            && !current.generated_content.is_empty()
        {
            tx.execute("UPDATE creation_operations SET base_revision=?3,base_document=?4
                WHERE session_id=?1 AND operation_id=?2",
                params![session,id,current.revision_no,current.generated_content])?;
            operation.base_revision = current.revision_no;
            operation.base_document = current.generated_content;
        }
    }
    tx.commit()?;
    Ok(Some(operation))
}

pub fn restart_attempt(
    conn: &Connection,
    session: &str,
    id: &str,
    new_attempt: bool,
) -> Result<()> {
    if !new_attempt {
        return Ok(());
    }
    conn.execute("UPDATE creation_operations SET retry_checkpoint_json=NULL,status='running',last_sequence=-1
        WHERE session_id=?1 AND operation_id=?2 AND status IN ('partial','failed','waiting','running')",params![session,id])?;
    Ok(())
}

/// A dropped SSE consumer does not cancel the durable instruction.  Move only
/// the still-running attempt to a resumable state; completed/failed/cancelled
/// operations keep the terminal status written by their own event.
pub fn mark_waiting_if_running(conn: &Connection, session: &str, id: &str) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "UPDATE creation_operations SET status='waiting',updated_at=?3
         WHERE session_id=?1 AND operation_id=?2 AND status='running'",
        params![session, id, now],
    )?;
    Ok(())
}

/// Persist before forwarding; completion and document revision share one transaction.
pub fn record_event(conn: &Connection, session: &str, id: &str, event: &mut Value) -> Result<()> {
    let tx = conn.unchecked_transaction()?;
    let operation = get(&tx, session, id)?.ok_or(rusqlite::Error::QueryReturnedNoRows)?;
    if operation.status == "completed" {
        if event["type"] == "run.completed" {
            if let Some(result) = operation.result {
                *event = result;
            }
        }
        return Ok(());
    }
    let sequence = event["sequence"].as_i64().unwrap_or(0);
    let now = chrono::Utc::now().timestamp_millis();
    let mut kind = event["type"].as_str().unwrap_or("").to_string();
    if kind == "operation.undo.requested" {
        let target_id = event["data"]["operation_id"]
            .as_str()
            .ok_or(rusqlite::Error::InvalidQuery)?;
        let target = get(&tx, session, target_id)?.ok_or(rusqlite::Error::QueryReturnedNoRows)?;
        let result = target
            .result
            .as_ref()
            .ok_or(rusqlite::Error::InvalidQuery)?;
        if target.status != "completed"
            || result["data"]["revision_no"] != operation.base_revision
            || result["data"]["document"] != operation.base_document
        {
            return Err(rusqlite::Error::InvalidQuery);
        }
        tx.execute(
            "UPDATE creation_operations SET status='undone',updated_at=?2 WHERE operation_id=?1",
            params![target_id, now],
        )?;
        event["type"] = json!("run.completed");
        event["status"] = json!("completed");
        event["data"] = json!({"document":target.original_document,"response":"已撤销上次文档修改。",
            "document_patch":{"operation":"undo_operation","summary":"已撤销上次文档修改"},
            "goal":{"status":"complete","remaining_steps":[],"outcome":"已撤销上次文档修改"}});
        event["goal"] = event["data"]["goal"].clone();
        kind = "run.completed".to_string();
    }
    if kind == "operation.checkpoint"
        || (kind == "run.paused" && event["data"]["continuation"].is_object())
    {
        let mut checkpoint = if kind == "operation.checkpoint" {
            event["data"]["checkpoint"].clone()
        } else {
            event["data"]["continuation"].clone()
        };
        checkpoint["sequence"] = json!(sequence);
        if checkpoint["session_id"] != session {
            return Err(rusqlite::Error::InvalidQuery);
        }
        tx.execute(
            "UPDATE creation_operations SET checkpoint_json=?3,last_sequence=?4,updated_at=?5
            WHERE session_id=?1 AND operation_id=?2 AND last_sequence<?4",
            params![session, id, checkpoint.to_string(), sequence, now],
        )?;
    }
    if kind == "operation.resume.requested" {
        tx.execute("UPDATE creation_operations SET status='delegated',updated_at=?3 WHERE session_id=?1 AND operation_id=?2",params![session,id,now])?;
    }
    if kind == "agent.failed" || kind == "tool.failed" {
        tx.execute("UPDATE creation_operations SET retry_checkpoint_json=COALESCE(retry_checkpoint_json,checkpoint_json)
            WHERE session_id=?1 AND operation_id=?2",params![session,id])?;
    }
    if kind == "run.completed" {
        let partial: bool = tx.query_row("SELECT retry_checkpoint_json IS NOT NULL FROM creation_operations WHERE operation_id=?1",
            params![id],|row|row.get(0))?;
        if partial {
            event["goal"] = json!({"status":"paused","outcome":"已保存部分结果，可继续未完成操作"});
            event["data"]["goal"] = event["goal"].clone();
        }
        let current = super::creation_history::get_by_id(&tx, operation.history_id)?
            .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
        if !super::creation_history::matches_document_base(&current, session, operation.base_revision, &operation.base_document)
        {
            return Err(rusqlite::Error::InvalidQuery);
        }
        let content = event["data"]["document"]
            .as_str()
            .unwrap_or(&operation.base_document)
            .to_string();
        let revision = operation.base_revision + i64::from(content != operation.base_document);
        tx.execute("UPDATE creation_history SET generated_content=?2,revision_no=?3,
            document_patch_json=?4,goal_json=?5,lifecycle_status=?7,updated_at=?6,edit_operation=?8 WHERE id=?1",
            params![operation.history_id,content,revision,event["data"]["document_patch"].to_string(),
                event["data"]["goal"].to_string(),now,if partial {"failed"} else {"completed"},
                event["data"]["edit_intent"]["operation"].as_str().unwrap_or("document_patch")])?;
        event["data"]["committed_operation_id"] = json!(id);
        event["data"]["revision_no"] = json!(revision);
        event["data"]["history_id"] = json!(operation.history_id);
        if partial {
            event["data"]["incomplete"] = json!(true);
            event["data"]["response"] = json!("已保存部分结果，可继续未完成的操作。");
            tx.execute(
                "UPDATE creation_operations SET status='partial',result_json=?3,updated_at=?4,
                checkpoint_json=retry_checkpoint_json,base_revision=?5,base_document=?6
                WHERE session_id=?1 AND operation_id=?2",
                params![session, id, event.to_string(), now, revision, content],
            )?;
        } else {
            tx.execute(
                "UPDATE creation_operations SET status='completed',result_json=?3,updated_at=?4
                WHERE session_id=?1 AND operation_id=?2",
                params![session, id, event.to_string(), now],
            )?;
        }
    } else if kind == "run.failed" || kind == "run.paused" {
        tx.execute("UPDATE creation_operations SET status=?3,updated_at=?4 WHERE session_id=?1 AND operation_id=?2",
            params![session,id,if kind == "run.failed" {"failed"} else {"waiting"},now])?;
    }
    // The checkpoint contains large execution inputs and is stored once, separately.
    // Keep ordinary events append-only by event_id, including failed and completed runs.
    if kind != "operation.checkpoint" && !kind.ends_with(".delta") && kind != "document.preview" {
        let history = super::creation_history::get_by_id(&tx, operation.history_id)?
            .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
        let mut trace: Vec<Value> = history
            .agent_trace_json
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();
        let mut stored = event.clone();
        if kind == "run.paused" {
            stored["data"]
                .as_object_mut()
                .map(|d| d.remove("continuation"));
        }
        if kind == "model.request" {
            stored["data"].as_object_mut().map(|d| d.remove("messages"));
        }
        if !trace
            .iter()
            .any(|item| item["event_id"] == stored["event_id"])
        {
            trace.push(stored);
        }
        tx.execute(
            "UPDATE creation_history SET agent_trace_json=?2 WHERE id=?1",
            params![operation.history_id, serde_json::to_string(&trace).unwrap()],
        )?;
    }
    tx.commit()
}

/// Undo is another conditional commit; never discard intervening edits.
pub fn undo(conn: &Connection, session: &str, id: &str) -> Result<String> {
    let tx = conn.unchecked_transaction()?;
    let operation = get(&tx, session, id)?.ok_or(rusqlite::Error::QueryReturnedNoRows)?;
    let result = operation.result.ok_or(rusqlite::Error::InvalidQuery)?;
    let content = result["data"]["document"]
        .as_str()
        .ok_or(rusqlite::Error::InvalidQuery)?;
    let revision = result["data"]["revision_no"]
        .as_i64()
        .ok_or(rusqlite::Error::InvalidQuery)?;
    let changed = tx.execute(
        "UPDATE creation_history SET generated_content=?2,revision_no=revision_no+1
        WHERE id=?1 AND revision_no=?3 AND generated_content=?4",
        params![
            operation.history_id,
            operation.original_document,
            revision,
            content
        ],
    )?;
    if operation.status != "completed" || changed != 1 {
        return Err(rusqlite::Error::InvalidQuery);
    }
    tx.execute(
        "UPDATE creation_operations SET status='undone' WHERE operation_id=?1",
        params![id],
    )?;
    tx.commit()?;
    Ok(operation.original_document)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::StorageManager;

    #[test]
    fn failed_candidate_save_can_resume_but_intervening_edits_cannot() {
        for (content, revision, lifecycle, expected) in [
            ("saved candidate",2,"failed",2),
            ("user edit",2,"failed",1),
            ("saved candidate",3,"failed",1),
            ("saved candidate",2,"completed",1),
        ] {
            let storage = StorageManager::open_in_memory().unwrap();
            storage.with_conn(|conn| {
                let id = fixture(conn);
                conn.execute("UPDATE creation_operations SET status='failed',checkpoint_json=?1 WHERE operation_id='op'",
                    params![json!({"current_document":"saved candidate","cursor":5}).to_string()])?;
                conn.execute("UPDATE creation_history SET generated_content=?2,revision_no=?3,lifecycle_status=?4 WHERE id=?1",
                    params![id,content,revision,lifecycle])?;
                let operation = get_for_resume(conn,"session","op")?.unwrap();
                assert_eq!(operation.base_revision,expected);
                assert_eq!(operation.original_document,"## A\nbase\n");
                assert_eq!(operation.checkpoint.unwrap()["cursor"],5);
                assert_eq!(get_for_resume(conn,"session","op")?.unwrap().base_revision,expected);
                if expected == 2 {
                    restart_attempt(conn,"session","op",true)?;
                    let mut event = complete();
                    record_event(conn,"session","op",&mut event)?;
                    assert_eq!(event["data"]["revision_no"],3);
                    assert_eq!(undo(conn,"session","op")?,"## A\nbase\n");
                }
                Ok(())
            }).unwrap();
        }
    }

    fn fixture(conn: &Connection) -> i64 {
        let id = super::super::creation_history::insert(
            conn,
            "instruction",
            "## A\nbase\n",
            None,
            None,
            0,
            None,
            None,
            None,
            Some("session"),
            Some("[]"),
            Some("[]"),
            None,
            Some("goal"),
            None,
            1,
            "create_document",
            None,
        )
        .unwrap();
        let history = super::super::creation_history::get_by_id(conn, id)
            .unwrap()
            .unwrap();
        begin(
            conn,
            "session",
            "op",
            "instruction-1",
            "modify document",
            &history,
        )
        .unwrap();
        id
    }

    fn complete() -> Value {
        json!({"type":"run.completed","event_id":"completed","sequence":8,"run_id":"run",
            "data":{"document":"## A\nchanged\n","document_patch":{"operation":"document_patch"},
                    "goal":{"status":"complete"}}})
    }

    #[test]
    fn completion_is_atomic_idempotent_and_conditionally_undoable() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                let id = fixture(conn);
                let mut event = complete();
                record_event(conn, "session", "op", &mut event)?;
                assert_eq!(event["data"]["revision_no"], 2);
                record_event(conn, "session", "op", &mut complete())?;
                let history = super::super::creation_history::get_by_id(conn, id)?.unwrap();
                assert_eq!(history.revision_no, 2);
                assert_eq!(history.generated_content, "## A\nchanged\n");
                assert!(pending(conn, "session", "")?.is_empty());
                assert_eq!(undo(conn, "session", "op")?, "## A\nbase\n");
                assert!(undo(conn, "session", "op").is_err());
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn changed_base_rolls_back_result_and_remains_resumable() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.with_conn(|conn| {
            let id = fixture(conn);
            conn.execute("UPDATE creation_history SET generated_content='newer',revision_no=2 WHERE id=?1",params![id])?;
            assert!(record_event(conn,"session","op",&mut complete()).is_err());
            let operation = get(conn,"session","op")?.unwrap();
            assert_eq!(operation.status,"running");
            assert!(operation.result.is_none());
            assert_eq!(super::super::creation_history::get_by_id(conn,id)?.unwrap().generated_content,"newer");
            Ok(())
        }).unwrap();
    }

    #[test]
    fn checkpoint_roundtrip_keeps_completed_nodes_and_rejects_stale_delivery() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.with_conn(|conn| {
            fixture(conn);
            let mut event = json!({"type":"operation.checkpoint","sequence":5,
                "data":{"checkpoint":{"session_id":"session","cursor":2,"environment":{"result":"saved"}}}});
            record_event(conn,"session","op",&mut event)?;
            event["sequence"] = json!(3);
            event["data"]["checkpoint"]["cursor"] = json!(0);
            record_event(conn,"session","op",&mut event)?;
            let saved = get(conn,"session","op")?.unwrap().checkpoint.unwrap();
            assert_eq!(saved["cursor"],2);
            assert_eq!(saved["environment"]["result"],"saved");
            assert_eq!(saved["sequence"],5);
            assert_eq!(pending(conn,"session","")?[0]["instruction"],"modify document");
            assert!(get(conn,"another-session","op")?.is_none());
            Ok(())
        }).unwrap();
    }

    #[test]
    fn dropped_stream_marks_only_running_operation_waiting() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.with_conn(|conn| {
            fixture(conn);
            mark_waiting_if_running(conn, "session", "op")?;
            assert_eq!(get(conn, "session", "op")?.unwrap().status, "waiting");

            conn.execute(
                "UPDATE creation_operations SET status='completed' WHERE operation_id='op'",
                [],
            )?;
            mark_waiting_if_running(conn, "session", "op")?;
            assert_eq!(get(conn, "session", "op")?.unwrap().status, "completed");
            Ok(())
        }).unwrap();
    }

    #[test]
    fn pending_excludes_operations_bound_to_an_obsolete_document_revision() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.with_conn(|conn| {
            let id = fixture(conn);
            assert_eq!(pending(conn, "session", "")?.len(), 1);
            conn.execute(
                "UPDATE creation_history SET generated_content='## A\nnewer\n',revision_no=2 WHERE id=?1",
                params![id],
            )?;
            assert!(pending(conn, "session", "")?.is_empty());
            Ok(())
        }).unwrap();
    }

    #[test]
    fn model_handoff_checkpoint_is_durable_and_not_copied_into_trace() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.with_conn(|conn| {
            let id = fixture(conn);
            let mut event = json!({"type":"run.paused","event_id":"pause","sequence":9,
                "data":{"continuation":{"session_id":"session","pending_model_step":{"request_id":"model-1"}}}});
            record_event(conn,"session","op",&mut event)?;
            let operation = get(conn,"session","op")?.unwrap();
            assert_eq!(operation.status,"waiting");
            assert_eq!(operation.checkpoint.unwrap()["pending_model_step"]["request_id"],"model-1");
            let trace = super::super::creation_history::get_by_id(conn,id)?.unwrap().agent_trace_json.unwrap();
            assert!(!trace.contains("continuation"));
            Ok(())
        }).unwrap();
    }

    #[test]
    fn partial_result_resumes_at_first_failed_node_and_keeps_original_undo_base() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                let id = fixture(conn);
                let mut checkpoint = json!({"type":"operation.checkpoint","sequence":1,
                "data":{"checkpoint":{"session_id":"session","cursor":1}}});
                record_event(conn, "session", "op", &mut checkpoint)?;
                record_event(
                    conn,
                    "session",
                    "op",
                    &mut json!({"type":"agent.failed","event_id":"failed","sequence":2}),
                )?;
                checkpoint["sequence"] = json!(4);
                checkpoint["data"]["checkpoint"]["cursor"] = json!(3);
                record_event(conn, "session", "op", &mut checkpoint)?;
                let mut partial = complete();
                record_event(conn, "session", "op", &mut partial)?;
                assert_eq!(partial["data"]["incomplete"], true);
                let operation = get(conn, "session", "op")?.unwrap();
                assert_eq!(operation.status, "partial");
                assert_eq!(operation.base_revision, 2);
                assert_eq!(operation.checkpoint.unwrap()["cursor"], 1);
                assert_eq!(pending(conn, "session", "")?.len(), 1);
                restart_attempt(conn, "session", "op", true)?;
                checkpoint["sequence"] = json!(1);
                checkpoint["data"]["checkpoint"]["cursor"] = json!(2);
                record_event(conn, "session", "op", &mut checkpoint)?;
                assert_eq!(
                    get(conn, "session", "op")?.unwrap().checkpoint.unwrap()["cursor"],
                    2
                );
                let mut final_result = complete();
                final_result["event_id"] = json!("final-completed");
                final_result["data"]["document"] = json!("## A\nfully completed\n");
                record_event(conn, "session", "op", &mut final_result)?;
                assert_eq!(get(conn, "session", "op")?.unwrap().status, "completed");
                assert_eq!(
                    super::super::creation_history::get_by_id(conn, id)?
                        .unwrap()
                        .revision_no,
                    3
                );
                assert_eq!(undo(conn, "session", "op")?, "## A\nbase\n");
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn legacy_pending_is_derived_from_unanswered_turns_without_command_keywords() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                let id = fixture(conn);
                conn.execute("DELETE FROM creation_operations", [])?;
                let conversation = json!([
                    {"role":"user","id":"root","content":"initial goal"},
                    {"role":"assistant","content":"done"},
                    {"role":"user","id":"old","content":"Move node B above node A"},
                    {"role":"user","id":"current","content":"finish the previous change"}
                ]);
                conn.execute(
                    "UPDATE creation_history SET conversation_json=?2 WHERE id=?1",
                    params![id, conversation.to_string()],
                )?;
                let history = super::super::creation_history::get_by_id(conn, id)?.unwrap();
                seed_legacy_pending(conn, "session", "current", &history)?;
                let candidates = pending(conn, "session", "")?;
                assert_eq!(candidates.len(), 1);
                assert_eq!(candidates[0]["instruction"], "Move node B above node A");
                seed_legacy_pending(conn, "session", "current", &history)?;
                assert_eq!(pending(conn, "session", "")?.len(), 1);
                Ok(())
            })
            .unwrap();
    }
}
