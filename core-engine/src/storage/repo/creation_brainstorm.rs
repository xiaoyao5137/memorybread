use rusqlite::{params, Connection, OptionalExtension, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// Read-only workflow state for history cards. A chapter operation's terminal
/// state and the overall writing workflow are deliberately separate.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainstormStageSummary {
    pub version: u8,
    pub phase: String,
    pub active_stage: String,
    pub stage_status: String,
    pub completed_chapters: usize,
    pub total_chapters: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub active_chapter_title: Option<String>,
}

/// Project all requested sessions in one query. Question trees and candidate
/// text remain in SQLite and are never attached to the history response.
pub fn stage_summaries(
    conn: &Connection,
    session_ids: &[String],
) -> Result<BTreeMap<String, BrainstormStageSummary>> {
    let ids: BTreeSet<_> = session_ids
        .iter()
        .map(String::as_str)
        .filter(|id| !id.trim().is_empty())
        .collect();
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let placeholders = vec!["?"; ids.len()].join(",");
    let mut stmt = conn.prepare(&format!(
        "WITH sessions AS (
            SELECT session_id, phase,
                CASE WHEN json_valid(state_json) THEN state_json ELSE '{{}}' END AS state
            FROM creation_brainstorm_sessions WHERE session_id IN ({placeholders})
        )
        SELECT session_id, phase,
            CAST(json_extract(state,'$.stage_workflow.active_stage') AS TEXT),
            (SELECT CAST(json_extract(CASE WHEN json_valid(value) THEN value ELSE '{{}}' END,'$.status') AS TEXT)
             FROM json_each(state,'$.stage_workflow.stages')
             WHERE json_extract(CASE WHEN json_valid(value) THEN value ELSE '{{}}' END,'$.id')
                = json_extract(state,'$.stage_workflow.active_stage') LIMIT 1),
            (SELECT COUNT(*) FROM json_each(state,'$.stage_workflow.chapters')
             WHERE json_extract(CASE WHEN json_valid(value) THEN value ELSE '{{}}' END,'$.status')='completed'),
            COALESCE(json_array_length(state,'$.stage_workflow.chapters'),0),
            (SELECT CAST(json_extract(CASE WHEN json_valid(value) THEN value ELSE '{{}}' END,'$.title') AS TEXT)
             FROM json_each(state,'$.stage_workflow.chapters')
             WHERE json_extract(CASE WHEN json_valid(value) THEN value ELSE '{{}}' END,'$.id')
                = json_extract(state,'$.stage_workflow.active_chapter_id') LIMIT 1)
        FROM sessions WHERE json_extract(state,'$.stage_workflow.version')=2"
    ))?;
    let rows = stmt.query_map(rusqlite::params_from_iter(ids), |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, usize>(4)?,
            row.get::<_, usize>(5)?,
            row.get::<_, Option<String>>(6)?,
        ))
    })?;
    let mut summaries = BTreeMap::new();
    for row in rows {
        let (
            session_id,
            phase,
            active_stage,
            stage_status,
            completed_chapters,
            total_chapters,
            active_chapter_title,
        ) = row?;
        if let (Some(active_stage), Some(stage_status)) = (active_stage, stage_status) {
            summaries.insert(
                session_id,
                BrainstormStageSummary {
                    version: 2,
                    phase,
                    active_stage,
                    stage_status,
                    completed_chapters,
                    total_chapters,
                    active_chapter_title,
                },
            );
        }
    }
    Ok(summaries)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreationBrainstormSession {
    pub session_id: String,
    pub root_request: String,
    pub phase: String,
    pub revision: i64,
    pub state_json: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub completed_at: Option<i64>,
}

pub fn get(conn: &Connection, session_id: &str) -> Result<Option<CreationBrainstormSession>> {
    conn.query_row(
        "SELECT session_id, root_request, phase, revision, state_json,
                created_at, updated_at, completed_at
         FROM creation_brainstorm_sessions WHERE session_id = ?1",
        params![session_id],
        |row| {
            Ok(CreationBrainstormSession {
                session_id: row.get(0)?,
                root_request: row.get(1)?,
                phase: row.get(2)?,
                revision: row.get(3)?,
                state_json: row.get(4)?,
                created_at: row.get(5)?,
                updated_at: row.get(6)?,
                completed_at: row.get(7)?,
            })
        },
    )
    .optional()
}

pub fn create(
    conn: &Connection,
    session_id: &str,
    root_request: &str,
    phase: &str,
    state_json: &str,
) -> Result<CreationBrainstormSession> {
    let now = chrono::Utc::now().timestamp_millis();
    conn.execute(
        "INSERT INTO creation_brainstorm_sessions (
            session_id, root_request, phase, revision, state_json, created_at, updated_at
         ) VALUES (?1, ?2, ?3, 0, ?4, ?5, ?5)",
        params![session_id, root_request, phase, state_json, now],
    )?;
    Ok(CreationBrainstormSession {
        session_id: session_id.to_string(),
        root_request: root_request.to_string(),
        phase: phase.to_string(),
        revision: 0,
        state_json: state_json.to_string(),
        created_at: now,
        updated_at: now,
        completed_at: None,
    })
}

pub fn update(
    conn: &Connection,
    session_id: &str,
    expected_revision: i64,
    phase: &str,
    state_json: &str,
) -> Result<bool> {
    let now = chrono::Utc::now().timestamp_millis();
    let completed_at = if matches!(phase, "completed" | "abandoned") {
        Some(now)
    } else {
        None
    };
    let changed = conn.execute(
        "UPDATE creation_brainstorm_sessions
         SET phase = ?1,
             revision = revision + 1,
             state_json = ?2,
             updated_at = ?3,
             completed_at = ?4
         WHERE session_id = ?5 AND revision = ?6",
        params![
            phase,
            state_json,
            now,
            completed_at,
            session_id,
            expected_revision
        ],
    )?;
    Ok(changed > 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stage_summaries_project_requested_v2_sessions_without_candidate_trees() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE creation_brainstorm_sessions (
            session_id TEXT PRIMARY KEY, phase TEXT NOT NULL, state_json TEXT NOT NULL);",
        )
        .unwrap();
        let current = serde_json::json!({"stage_workflow": {
            "version":2,"active_stage":"writing","active_chapter_id":"two",
            "stages":[{"id":"writing","status":"answering","questions":[{"prompt":"unchosen candidate must stay private"}]}],
            "chapters":[{"id":"one","title":"开篇","status":"completed"},
                {"id":"two","title":"结尾","status":"answering"}]}});
        for (id, state) in [
            ("current", current.to_string()),
            ("other", current.to_string()),
            ("legacy", "{}".into()),
            ("broken", "{broken".into()),
        ] {
            conn.execute(
                "INSERT INTO creation_brainstorm_sessions VALUES (?1,'exploring',?2)",
                params![id, state],
            )
            .unwrap();
        }
        assert!(stage_summaries(&conn, &[]).unwrap().is_empty());
        let summaries = stage_summaries(
            &conn,
            &[
                "current".into(),
                "current".into(),
                "".into(),
                "legacy".into(),
                "broken".into(),
                "missing".into(),
            ],
        )
        .unwrap();
        assert_eq!(summaries.len(), 1);
        let summary = &summaries["current"];
        assert_eq!(summary.phase, "exploring");
        assert_eq!(summary.active_stage, "writing");
        assert_eq!(summary.stage_status, "answering");
        assert_eq!((summary.completed_chapters, summary.total_chapters), (1, 2));
        assert_eq!(summary.active_chapter_title.as_deref(), Some("结尾"));
        let serialized = serde_json::to_string(summary).unwrap();
        assert!(!serialized.contains("candidate"));
        assert!(!serialized.contains("questions"));
        assert!(serialized.len() < 300);
        let persisted: String = conn
            .query_row(
                "SELECT state_json FROM creation_brainstorm_sessions WHERE session_id='current'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(persisted, current.to_string());
    }

    #[test]
    fn update_uses_optimistic_revision() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE creation_brainstorm_sessions (
                session_id TEXT PRIMARY KEY,
                root_request TEXT NOT NULL,
                phase TEXT NOT NULL,
                revision INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                completed_at INTEGER
            );",
        )
        .unwrap();
        create(&conn, "session-1", "写方案", "exploring", "{}").unwrap();
        assert!(update(&conn, "session-1", 0, "exploring", "{\"a\":1}").unwrap());
        assert!(!update(&conn, "session-1", 0, "ready", "{}").unwrap());
        assert_eq!(get(&conn, "session-1").unwrap().unwrap().revision, 1);
    }
}
