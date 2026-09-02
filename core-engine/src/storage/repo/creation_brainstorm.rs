use rusqlite::{params, Connection, OptionalExtension, Result};
use serde::{Deserialize, Serialize};

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
