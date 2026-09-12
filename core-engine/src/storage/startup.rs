//! Content-free startup evidence, readable before the HTTP server is available.
use super::StorageError;
use serde_json::json;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub struct DatabaseStartup {
    path: PathBuf,
    started_at: f64,
    migration: Option<&'static str>,
}
impl DatabaseStartup {
    pub fn new(database: &Path) -> Self {
        let status = Self {
            path: database
                .parent()
                .unwrap_or(Path::new("."))
                .join("state/database-startup.json"),
            started_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64(),
            migration: None,
        };
        status.write("opening", None);
        status
    }
    pub fn migration(&mut self, version: &'static str) {
        self.migration = Some(version);
        self.write("migrating", None);
    }
    pub fn finish(&self, result: &Result<super::StorageManager, StorageError>) {
        match result {
            Ok(_) => self.write("ready", None),
            Err(error) => self.write("failed", Some(classify(error))),
        }
    }
    fn write(&self, phase: &str, code: Option<&str>) {
        let value = json!({"pid": std::process::id(), "started_at": self.started_at,
            "phase": phase, "migration": self.migration, "error_code": code});
        let result = (|| -> std::io::Result<()> {
            std::fs::create_dir_all(self.path.parent().unwrap())?;
            let temporary = self
                .path
                .with_extension(format!("{}.tmp", std::process::id()));
            std::fs::write(&temporary, serde_json::to_vec(&value)?)?;
            std::fs::rename(temporary, &self.path)
        })();
        if result.is_err() {
            tracing::warn!("database startup evidence could not be saved");
        }
    }
}
fn classify(error: &StorageError) -> &'static str {
    match error {
        StorageError::Sqlite(rusqlite::Error::SqliteFailure(code, _)) => {
            match code.extended_code & 255 {
                5 | 6 => "DATABASE_BUSY",
                8 => "DATABASE_READ_ONLY",
                13 => "DATABASE_DISK_FULL",
                11 | 26 => "DATABASE_CORRUPT",
                3 | 14 | 23 => "DATABASE_ACCESS_DENIED",
                10 => "DATABASE_IO_ERROR",
                _ => "DATABASE_INITIALIZATION_FAILED",
            }
        }
        StorageError::Io(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            "DATABASE_ACCESS_DENIED"
        }
        StorageError::Io(error) if error.raw_os_error() == Some(28) => "DATABASE_DISK_FULL",
        StorageError::MigrationFailed { .. } => "DATABASE_MIGRATION_FAILED",
        _ => "DATABASE_INITIALIZATION_FAILED",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::StorageManager;

    #[test]
    fn observed_open_records_ready_and_reopens_existing_database() {
        let root = tempfile::tempdir().unwrap();
        let db = root.path().join("memory-bread.db");
        let storage = StorageManager::open_observed(&db).unwrap();
        storage.with_conn(|conn| {
            conn.execute_batch("CREATE TABLE user_evidence(value TEXT); INSERT INTO user_evidence VALUES ('retained');")?;
            Ok(())
        }).unwrap();
        drop(storage);
        let reopened = StorageManager::open_observed(&db).unwrap();
        reopened
            .with_conn(|conn| {
                let value: String =
                    conn.query_row("SELECT value FROM user_evidence", [], |row| row.get(0))?;
                assert_eq!(value, "retained");
                Ok(())
            })
            .unwrap();
        let status: serde_json::Value = serde_json::from_slice(
            &std::fs::read(root.path().join("state/database-startup.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(status["phase"], "ready");
        assert_eq!(status["pid"], std::process::id());
        assert!(!status.to_string().contains("retained"));
    }

    #[test]
    fn corrupt_database_records_safe_failure_without_replacing_bytes() {
        let root = tempfile::tempdir().unwrap();
        let db = root.path().join("memory-bread.db");
        let original = b"damaged user database - preserve";
        std::fs::write(&db, original).unwrap();
        assert!(StorageManager::open_observed(&db).is_err());
        let text =
            std::fs::read_to_string(root.path().join("state/database-startup.json")).unwrap();
        let status: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(status["phase"], "failed");
        assert_eq!(status["error_code"], "DATABASE_CORRUPT");
        assert!(!text.contains(root.path().to_str().unwrap()));
        assert_eq!(std::fs::read(db).unwrap(), original);
    }
}
