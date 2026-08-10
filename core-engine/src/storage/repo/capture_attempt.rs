//! 采集尝试审计记录。

use rusqlite::params;

use crate::storage::{
    db::current_ts_ms, error::StorageError, models::NewCaptureAttempt, StorageManager,
};

impl StorageManager {
    /// 记录一次采集结果。审计失败不得中断主采集链路，因此热路径通常只记录 warning。
    pub fn insert_capture_attempt(&self, attempt: &NewCaptureAttempt) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO capture_attempts (
                    observed_at, event_type, outcome, reason,
                    capture_id, related_capture_id, app_name, win_title,
                    is_private, effective_interval_secs, created_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                params![
                    attempt.observed_at,
                    attempt.event_type,
                    attempt.outcome,
                    attempt.reason,
                    attempt.capture_id,
                    attempt.related_capture_id,
                    attempt.app_name,
                    attempt.win_title,
                    attempt.is_private as i64,
                    attempt.effective_interval_secs.map(|value| value as i64),
                    current_ts_ms(),
                ],
            )?;
            Ok(conn.last_insert_rowid())
        })
    }

    /// 采集尝试与采集记录使用同一保留周期，避免健康审计表无限增长。
    pub fn delete_capture_attempts_before(&self, cutoff_ms: i64) -> Result<usize, StorageError> {
        self.with_conn(|conn| {
            let deleted = conn.execute(
                "DELETE FROM capture_attempts WHERE observed_at < ?1",
                params![cutoff_ms],
            )?;
            Ok(deleted)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn attempt(observed_at: i64) -> NewCaptureAttempt {
        NewCaptureAttempt {
            observed_at,
            event_type: "auto".to_string(),
            outcome: "degraded".to_string(),
            reason: "test".to_string(),
            capture_id: None,
            related_capture_id: None,
            app_name: None,
            win_title: None,
            is_private: false,
            effective_interval_secs: Some(90),
        }
    }

    #[test]
    fn capture_attempt_retention_deletes_only_rows_before_cutoff() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.insert_capture_attempt(&attempt(100)).unwrap();
        storage.insert_capture_attempt(&attempt(200)).unwrap();

        assert_eq!(storage.delete_capture_attempts_before(200).unwrap(), 1);
        let remaining: i64 = storage
            .with_conn(|conn| {
                Ok(
                    conn.query_row("SELECT COUNT(*) FROM capture_attempts", [], |row| {
                        row.get(0)
                    })?,
                )
            })
            .unwrap();
        assert_eq!(remaining, 1);
    }
}
