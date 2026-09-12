//! Preview images referenced by consultation history are user data, not a cache.
use std::{
    collections::HashSet,
    fs,
    path::Path,
    time::{Duration, SystemTime},
};

use rusqlite::{Connection, OpenFlags};

pub(crate) fn cleanup_unreferenced(
    directory: &Path,
    database: &Path,
    now: SystemTime,
    keep_duration: Duration,
) -> Result<(usize, u64), String> {
    // Fail closed before deleting anything if history cannot be read. In particular,
    // startup cleanup must not create an empty DB before the backend starts.
    let conn = Connection::open_with_flags(database, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|e| e.to_string())?;
    let mut statement = conn
        .prepare("SELECT retrieved_ids FROM rag_sessions WHERE retrieved_ids IS NOT NULL")
        .map_err(|e| e.to_string())?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| e.to_string())?;
    let mut referenced = HashSet::new();
    for row in rows {
        let raw = row.map_err(|e| e.to_string())?;
        let contexts: serde_json::Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
        if let Some(contexts) = contexts.as_array() {
            for context in contexts {
                if let Some(attachments) = context.get("attachments").and_then(|v| v.as_array()) {
                    for attachment in attachments {
                        if let Some(path) = attachment.get("path").and_then(|v| v.as_str()) {
                            let path = Path::new(path);
                            referenced.insert(path.canonicalize().unwrap_or_else(|_| path.to_path_buf()));
                        }
                    }
                }
                if let Some(path) = context.get("screenshot_path").and_then(|v| v.as_str()) {
                    let path = Path::new(path);
                    referenced.insert(path.canonicalize().unwrap_or_else(|_| path.to_path_buf()));
                }
            }
        }
    }
    let mut deleted = 0;
    let mut freed = 0;
    for entry in fs::read_dir(directory).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        if referenced.contains(&path.canonicalize().unwrap_or_else(|_| path.clone())) {
            continue;
        }
        let metadata = entry.metadata().map_err(|e| e.to_string())?;
        if !metadata.is_file() {
            continue;
        }
        let age = metadata
            .modified()
            .ok()
            .and_then(|modified| now.duration_since(modified).ok());
        if age.map_or(true, |age| age < keep_duration) {
            continue;
        }
        if fs::remove_file(&path).is_ok() {
            deleted += 1;
            freed += metadata.len();
        }
    }
    Ok((deleted, freed))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_history_images_but_removes_expired_ocr_and_orphans() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        fs::create_dir_all(&root).unwrap();
        let shots = root.join("floating-screenshots");
        fs::create_dir(&shots).unwrap();
        let database = root.join("history.db");
        let conn = Connection::open(&database).unwrap();
        conn.execute_batch("CREATE TABLE rag_sessions (retrieved_ids TEXT);")
            .unwrap();
        let saved = shots.join("saved.jpg");
        for name in ["saved.jpg", "monitor-ocr.jpg", "abandoned.jpg"] {
            fs::write(shots.join(name), b"image").unwrap();
        }
        conn.execute(
            "INSERT INTO rag_sessions VALUES (?1)",
            [
                serde_json::json!([{"source_type":"floating_assist", "screenshot_path":saved}])
                    .to_string(),
            ],
        )
        .unwrap();
        let retention = Duration::from_secs(86400);
        assert_eq!(
            cleanup_unreferenced(&shots, &database, SystemTime::now(), retention).unwrap(),
            (0, 0)
        );
        let later = SystemTime::now() + retention * 2;
        assert_eq!(
            cleanup_unreferenced(&shots, &database, later, retention).unwrap(),
            (2, 10)
        );
        assert_eq!(fs::read(&saved).unwrap(), b"image");
        conn.execute("DELETE FROM rag_sessions", []).unwrap();
        assert_eq!(
            cleanup_unreferenced(&shots, &database, later, retention).unwrap(),
            (1, 5)
        );
        drop(conn);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn preserves_multiple_uploaded_images_without_a_screenshot() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        fs::create_dir_all(&root).unwrap();
        let shots = root.join("shots");
        fs::create_dir(&shots).unwrap();
        let database = root.join("history.db");
        let conn = Connection::open(&database).unwrap();
        conn.execute_batch("CREATE TABLE rag_sessions (retrieved_ids TEXT)").unwrap();
        let a = shots.join("a.png"); let b = shots.join("b.png");
        fs::write(&a, b"image").unwrap(); fs::write(&b, b"image").unwrap();
        fs::write(shots.join("orphan.png"), b"image").unwrap();
        conn.execute("INSERT INTO rag_sessions VALUES (?1)", [serde_json::json!([{"attachments":[{"path":a},{"path":b}]}]).to_string()]).unwrap();
        assert_eq!(cleanup_unreferenced(&shots, &database, SystemTime::now() + Duration::from_secs(172800), Duration::from_secs(86400)).unwrap(), (1,5));
        assert!(a.exists() && b.exists());
        drop(conn); fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn unreadable_history_never_deletes_images_or_creates_database() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        fs::create_dir_all(&root).unwrap();
        let image = root.join("saved.jpg");
        fs::write(&image, b"image").unwrap();
        let database = root.join("missing.db");
        assert!(cleanup_unreferenced(&root, &database, SystemTime::now(), Duration::ZERO).is_err());
        assert!(!database.exists());
        let conn = Connection::open(&database).unwrap();
        conn.execute_batch("CREATE TABLE rag_sessions (retrieved_ids TEXT); INSERT INTO rag_sessions VALUES ('invalid json');").unwrap();
        assert!(cleanup_unreferenced(&root, &database, SystemTime::now(), Duration::ZERO).is_err());
        assert!(image.exists());
        drop(conn);
        fs::remove_dir_all(root).unwrap();
    }
}
