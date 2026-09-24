//! 数据清理模块
//!
//! 负责定期清理过期截图文件和历史采集记录，并记录到 data_cleanup_log 表。
//!
//! # 设计策略
//!
//! - 截图文件（screenshot_purge）：按用户配置删除过期且未关联时间线的 JPEG 文件，并更新 captures 中的路径为 NULL
//! - 旧采集记录（old_captures）：按用户配置删除过期采集行及其截图文件；时间线、知识和操作记录不参与删除
//! - VACUUM：整理 SQLite 碎片空间，建议每周运行一次

use std::{
    collections::HashSet,
    path::{Component, Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use rusqlite::params;
use tracing::{info, warn};

use super::{db::current_ts_ms, error::StorageError, StorageManager};

fn safe_capture_file_path(captures_dir: &Path, rel_path: &str) -> Option<PathBuf> {
    let rel = Path::new(rel_path);
    if rel.is_absolute()
        || rel
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return None;
    }
    Some(captures_dir.join(rel))
}

fn capture_relative_path(captures_dir: &Path, path: &Path) -> Option<String> {
    let rel = path.strip_prefix(captures_dir).ok()?;
    let mut parts = Vec::new();
    for part in rel.components() {
        let Component::Normal(value) = part else {
            return None;
        };
        parts.push(value.to_string_lossy().to_string());
    }
    Some(parts.join("/"))
}

fn cutoff_system_time(older_than_ms: i64) -> SystemTime {
    if older_than_ms <= 0 {
        return UNIX_EPOCH;
    }
    UNIX_EPOCH + Duration::from_millis(older_than_ms as u64)
}

fn delete_capture_file(
    captures_dir: &Path,
    rel_path: &str,
    count_missing_as_cleaned: bool,
) -> Result<(usize, u64), StorageError> {
    let Some(full_path) = safe_capture_file_path(captures_dir, rel_path) else {
        warn!("跳过不安全的截图路径: {}", rel_path);
        return Ok((0, 0));
    };

    match std::fs::metadata(&full_path) {
        Ok(meta) => {
            let size = meta.len();
            if let Err(e) = std::fs::remove_file(&full_path) {
                warn!("删除截图文件失败 {:?}: {}", full_path, e);
                return Ok((0, 0));
            }
            Ok((1, size))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Ok((usize::from(count_missing_as_cleaned), 0))
        }
        Err(e) => Err(StorageError::Io(e)),
    }
}

fn delete_capture_file_strict(captures_dir: &Path, rel_path: &str) -> Result<(), StorageError> {
    let full_path = safe_capture_file_path(captures_dir, rel_path).ok_or_else(|| {
        StorageError::Io(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("不安全的截图路径: {rel_path}"),
        ))
    })?;

    match std::fs::remove_file(&full_path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(StorageError::Io(error)),
    }
}

fn cleanup_orphan_capture_files(
    captures_dir: &Path,
    referenced_paths: &HashSet<String>,
    older_than_ms: i64,
) -> Result<(usize, u64), StorageError> {
    let screenshot_dir = captures_dir.join("screenshots");
    if !screenshot_dir.exists() {
        return Ok((0, 0));
    }

    let cutoff = cutoff_system_time(older_than_ms);
    let mut deleted_count = 0usize;
    let mut freed_bytes = 0u64;

    for entry in std::fs::read_dir(&screenshot_dir)? {
        let entry = match entry {
            Ok(entry) => entry,
            Err(e) => {
                warn!("读取截图目录项失败: {}", e);
                continue;
            }
        };
        let path = entry.path();
        let meta = match entry.metadata() {
            Ok(meta) => meta,
            Err(e) => {
                warn!("读取截图文件元信息失败 {:?}: {}", path, e);
                continue;
            }
        };
        if !meta.is_file() {
            continue;
        }

        let Some(rel_path) = capture_relative_path(captures_dir, &path) else {
            continue;
        };
        if referenced_paths.contains(&rel_path) {
            continue;
        }

        let modified = meta.modified().unwrap_or(UNIX_EPOCH);
        if modified > cutoff {
            continue;
        }

        let (deleted, freed) = delete_capture_file(captures_dir, &rel_path, false)?;
        deleted_count += deleted;
        freed_bytes += freed;
    }

    Ok((deleted_count, freed_bytes))
}

// ─────────────────────────────────────────────────────────────────────────────
// 清理入口
// ─────────────────────────────────────────────────────────────────────────────

impl StorageManager {
    /// 用户手动删除单条采集记录，同时清理其截图文件和 SQLite 向量元数据。
    ///
    /// 截图先删除；若文件系统操作失败则保留数据库记录，让用户可以重试。
    pub fn delete_capture_with_assets(
        &self,
        id: i64,
        captures_dir: &Path,
    ) -> Result<bool, StorageError> {
        let Some(capture) = self.get_capture(id)? else {
            return Ok(false);
        };

        if let Some(relative_path) = capture.screenshot_path.as_deref() {
            delete_capture_file_strict(captures_dir, relative_path)?;
        }

        self.delete_capture(id)
    }

    /// 清理过期截图文件。
    ///
    /// 步骤：
    /// 1. 查找没有可提炼文本的孤立旧截图
    /// 2. 尝试删除对应的文件
    /// 3. 将 captures.screenshot_path 置为 NULL
    /// 4. 写入 data_cleanup_log
    ///
    /// 返回 `(deleted_file_count, freed_bytes)`
    pub fn run_screenshot_purge(
        &self,
        older_than_ms: i64,
        captures_dir: &Path,
    ) -> Result<(usize, u64), StorageError> {
        // 有可提炼文本且 timeline_id 仍为空的 capture 属于提炼 backlog，不能因节能限速
        // 而提前删除截图。这里只清理没有任何可提炼文本的孤立旧截图。
        let rows = self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, screenshot_path FROM captures
                 WHERE screenshot_path IS NOT NULL
                   AND ts < ?1
                   AND timeline_id IS NULL
                   AND COALESCE(ax_text, '') = ''
                   AND COALESCE(ocr_text, '') = ''
                   AND COALESCE(input_text, '') = ''
                   AND COALESCE(audio_text, '') = ''
                   AND COALESCE(url, '') = ''
                   AND COALESCE(webpage_title, '') = ''",
            )?;
            let result = stmt
                .query_map(params![older_than_ms], |row| {
                    Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(result)
        })?;

        let mut deleted_count = 0usize;
        let mut freed_bytes = 0u64;

        for (id, rel_path) in &rows {
            let (deleted, freed) = delete_capture_file(captures_dir, rel_path, true)?;
            deleted_count += deleted;
            freed_bytes += freed;

            // 将数据库中的路径置为 NULL
            self.with_conn(|conn| {
                conn.execute(
                    "UPDATE captures
                     SET screenshot_path = NULL,
                         screenshot_source = NULL
                     WHERE id = ?1
                       AND timeline_id IS NULL
                       AND COALESCE(ax_text, '') = ''
                       AND COALESCE(ocr_text, '') = ''
                       AND COALESCE(input_text, '') = ''
                       AND COALESCE(audio_text, '') = ''
                       AND COALESCE(url, '') = ''
                       AND COALESCE(webpage_title, '') = ''",
                    params![id],
                )?;
                Ok(())
            })?;
        }

        // 2. 写清理日志
        let now = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO data_cleanup_log (ts, cleanup_type, affected_count, freed_bytes, detail)
                 VALUES (?1, 'screenshot_purge', ?2, ?3, ?4)",
                params![
                    now,
                    deleted_count as i64,
                    freed_bytes as i64,
                    format!("older_than_ms={older_than_ms}"),
                ],
            )?;
            Ok(())
        })?;

        info!(
            "screenshot_purge 完成: 删除 {} 个文件，释放 {} bytes",
            deleted_count, freed_bytes
        );
        Ok((deleted_count, freed_bytes))
    }

    /// 清理过期采集记录。
    ///
    /// 只删除已经完成时间线提炼，或没有任何可提炼内容的原始 captures、其向量
    /// 索引元数据和对应截图文件；仍在提炼 backlog 中的有效 capture 永不因保留期
    /// 清理而丢失。时间线、知识、
    /// 操作记录等提炼物不会被删除。快照恢复生成的 `snapshot_ref` 只是跨设备
    /// 重建引用占位，不包含原始采集内容，也不应被采集保留期清理掉。历史 schema
    /// 中 timelines.capture_id 是外键，因此这里临时关闭外键检查以允许“原始证据
    /// 过期，提炼物保留”的产品语义。
    ///
    /// 返回 `(deleted_capture_count, deleted_screenshot_count, freed_bytes)`。
    pub fn run_old_captures_cleanup(
        &self,
        older_than_ms: i64,
        captures_dir: &Path,
    ) -> Result<(usize, usize, u64), StorageError> {
        let rows = self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, screenshot_path FROM captures
                 WHERE ts < ?1
                   AND (event_type IS NULL OR event_type <> 'snapshot_ref')
                   AND (
                       timeline_id IS NOT NULL
                       OR EXISTS (
                           SELECT 1 FROM timelines t
                           WHERE t.capture_id = captures.id
                       )
                       OR (
                           COALESCE(ax_text, '') = ''
                           AND COALESCE(ocr_text, '') = ''
                           AND COALESCE(input_text, '') = ''
                           AND COALESCE(audio_text, '') = ''
                           AND COALESCE(url, '') = ''
                           AND COALESCE(webpage_title, '') = ''
                       )
                   )",
            )?;
            let result = stmt
                .query_map(params![older_than_ms], |row| {
                    Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?))
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(result)
        })?;

        let mut deleted_screenshot_count = 0usize;
        let mut freed_bytes = 0u64;

        for (_, rel_path) in &rows {
            let Some(rel_path) = rel_path else {
                continue;
            };
            let (deleted, freed) = delete_capture_file(captures_dir, rel_path, true)?;
            deleted_screenshot_count += deleted;
            freed_bytes += freed;
        }

        let affected = if rows.is_empty() {
            0
        } else {
            self.with_conn(|conn| {
                let vector_tx = conn.unchecked_transaction()?;
                vector_tx.execute(
                    "INSERT OR IGNORE INTO vector_deletion_queue (
                         qdrant_point_id, source_type, reason, enqueued_at
                     )
                     SELECT
                         qdrant_point_id,
                         source_type,
                         'capture_retention_expired',
                         ?2
                     FROM vector_index
                     WHERE capture_id IN (
                        SELECT id FROM captures
                        WHERE ts < ?1
                          AND (event_type IS NULL OR event_type <> 'snapshot_ref')
                          AND (
                              timeline_id IS NOT NULL
                              OR EXISTS (
                                  SELECT 1 FROM timelines t
                                  WHERE t.capture_id = captures.id
                              )
                              OR (
                                  COALESCE(ax_text, '') = ''
                                  AND COALESCE(ocr_text, '') = ''
                                  AND COALESCE(input_text, '') = ''
                                  AND COALESCE(audio_text, '') = ''
                                  AND COALESCE(url, '') = ''
                                  AND COALESCE(webpage_title, '') = ''
                              )
                          )
                     )",
                    params![older_than_ms, current_ts_ms()],
                )?;
                vector_tx.execute(
                    "DELETE FROM vector_index
                 WHERE capture_id IN (
                    SELECT id FROM captures
                    WHERE ts < ?1
                      AND (event_type IS NULL OR event_type <> 'snapshot_ref')
                      AND (
                          timeline_id IS NOT NULL
                          OR EXISTS (
                              SELECT 1 FROM timelines t
                              WHERE t.capture_id = captures.id
                          )
                          OR (
                              COALESCE(ax_text, '') = ''
                              AND COALESCE(ocr_text, '') = ''
                              AND COALESCE(input_text, '') = ''
                              AND COALESCE(audio_text, '') = ''
                              AND COALESCE(url, '') = ''
                              AND COALESCE(webpage_title, '') = ''
                          )
                      )
                 )",
                    params![older_than_ms],
                )?;
                vector_tx.commit()?;

                // 保留 timeline 作为上层溯源，但原始 capture 即将按保留期删除。
                // 删除路径会临时关闭外键，因此必须显式清空直接引用，不能依赖
                // ON DELETE SET NULL。
                conn.execute(
                    "UPDATE data_source_links
                     SET capture_id = NULL
                     WHERE capture_id IN (
                        SELECT id FROM captures
                        WHERE ts < ?1
                          AND (event_type IS NULL OR event_type <> 'snapshot_ref')
                          AND (
                              timeline_id IS NOT NULL
                              OR EXISTS (
                                  SELECT 1 FROM timelines t
                                  WHERE t.capture_id = captures.id
                              )
                              OR (
                                  COALESCE(ax_text, '') = ''
                                  AND COALESCE(ocr_text, '') = ''
                                  AND COALESCE(input_text, '') = ''
                                  AND COALESCE(audio_text, '') = ''
                                  AND COALESCE(url, '') = ''
                                  AND COALESCE(webpage_title, '') = ''
                              )
                          )
                     )",
                    params![older_than_ms],
                )?;

                conn.execute_batch("PRAGMA foreign_keys = OFF;")?;
                let delete_result = conn.execute(
                    "DELETE FROM captures
                 WHERE ts < ?1
                   AND (event_type IS NULL OR event_type <> 'snapshot_ref')
                   AND (
                       timeline_id IS NOT NULL
                       OR EXISTS (
                           SELECT 1 FROM timelines t
                           WHERE t.capture_id = captures.id
                       )
                       OR (
                           COALESCE(ax_text, '') = ''
                           AND COALESCE(ocr_text, '') = ''
                           AND COALESCE(input_text, '') = ''
                           AND COALESCE(audio_text, '') = ''
                           AND COALESCE(url, '') = ''
                           AND COALESCE(webpage_title, '') = ''
                       )
                   )",
                    params![older_than_ms],
                );
                let restore_result = conn.execute_batch("PRAGMA foreign_keys = ON;");

                match (delete_result, restore_result) {
                    (Ok(n), Ok(())) => Ok(n),
                    (Err(err), _) => Err(StorageError::Sqlite(err)),
                    (Ok(_), Err(err)) => Err(StorageError::Sqlite(err)),
                }
            })?
        };

        let referenced_screenshot_paths = self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT screenshot_path FROM captures WHERE screenshot_path IS NOT NULL",
            )?;
            let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
            rows.collect::<Result<HashSet<_>, _>>()
                .map_err(StorageError::Sqlite)
        })?;
        let (orphan_screenshot_count, orphan_freed_bytes) = cleanup_orphan_capture_files(
            captures_dir,
            &referenced_screenshot_paths,
            older_than_ms,
        )?;
        deleted_screenshot_count += orphan_screenshot_count;
        freed_bytes += orphan_freed_bytes;

        let now = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO data_cleanup_log (ts, cleanup_type, affected_count, freed_bytes, detail)
                 VALUES (?1, 'old_captures', ?2, ?3, ?4)",
                params![
                    now,
                    affected as i64,
                    freed_bytes as i64,
                    format!(
                        "older_than_ms={older_than_ms}; deleted_screenshots={deleted_screenshot_count}; orphan_screenshots={orphan_screenshot_count}; freed_bytes={freed_bytes}"
                    ),
                ],
            )?;
            Ok(())
        })?;

        info!(
            "old_captures 清理完成: 删除 {} 行，清理 {} 个截图文件（孤儿 {} 个），释放 {} bytes",
            affected, deleted_screenshot_count, orphan_screenshot_count, freed_bytes
        );
        Ok((affected, deleted_screenshot_count, freed_bytes))
    }

    /// 执行 VACUUM，释放碎片空间。
    ///
    /// 注意：VACUUM 在 WAL 模式下仍可执行，但会产生一定的 I/O 压力。
    /// 建议在业务低峰期（深夜）执行。
    pub fn run_vacuum(&self) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute_batch("VACUUM;")?;
            Ok(())
        })?;

        let now = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO data_cleanup_log (ts, cleanup_type, affected_count, freed_bytes, detail)
                 VALUES (?1, 'vacuum', 0, 0, 'manual vacuum')",
                params![now],
            )?;
            Ok(())
        })?;

        info!("VACUUM 完成");
        Ok(())
    }

    /// 查询清理历史记录，按 ts 倒序。
    pub fn list_cleanup_logs(&self, limit: usize) -> Result<Vec<CleanupLogRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, ts, cleanup_type, affected_count, freed_bytes, detail
                 FROM data_cleanup_log ORDER BY ts DESC LIMIT ?1",
            )?;
            let rows = stmt.query_map(params![limit as i64], |row| {
                Ok(CleanupLogRecord {
                    id: row.get(0)?,
                    ts: row.get(1)?,
                    cleanup_type: row.get(2)?,
                    affected_count: row.get(3)?,
                    freed_bytes: row.get(4)?,
                    detail: row.get(5)?,
                })
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 数据模型
// ─────────────────────────────────────────────────────────────────────────────

/// data_cleanup_log 表的读取模型
#[derive(Debug, Clone)]
pub struct CleanupLogRecord {
    pub id: i64,
    pub ts: i64,
    pub cleanup_type: String,
    pub affected_count: i64,
    pub freed_bytes: i64,
    pub detail: Option<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::models::{EventType, NewCapture};
    use tempfile::tempdir;

    fn make_mgr() -> StorageManager {
        StorageManager::open_in_memory().expect("内存数据库初始化失败")
    }

    fn write_screenshot(captures_dir: &Path, rel_path: &str, bytes: &[u8]) {
        let full_path = captures_dir.join(rel_path);
        if let Some(parent) = full_path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(full_path, bytes).unwrap();
    }

    #[test]
    fn test_old_captures_cleanup() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();

        // 插入一条 "180天前" 的记录
        let old_ts = current_ts_ms() - 180 * 24 * 3600 * 1000 - 1;
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (ts, event_type) VALUES (?1, 'auto')",
                params![old_ts],
            )?;
            Ok(())
        })
        .unwrap();

        // 插入一条 "刚才" 的记录
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (ts, event_type) VALUES (?1, 'auto')",
                params![current_ts_ms()],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() - 180 * 24 * 3600 * 1000;
        let (deleted, deleted_screenshots, freed) =
            mgr.run_old_captures_cleanup(cutoff, dir.path()).unwrap();
        assert_eq!(deleted, 1);
        assert_eq!(deleted_screenshots, 0);
        assert_eq!(freed, 0);

        // 验证清理日志
        let logs = mgr.list_cleanup_logs(10).unwrap();
        assert!(!logs.is_empty());
        assert_eq!(logs[0].cleanup_type, "old_captures");
        assert_eq!(logs[0].affected_count, 1);
    }

    #[test]
    fn old_capture_cleanup_uses_timeline_capture_index() {
        let mgr = make_mgr();
        let plan = mgr
            .with_conn(|conn| {
                let mut stmt = conn.prepare(
                    "EXPLAIN QUERY PLAN
                     SELECT id, screenshot_path FROM captures
                     WHERE ts < ?1
                       AND (event_type IS NULL OR event_type <> 'snapshot_ref')
                       AND (
                           timeline_id IS NOT NULL
                           OR EXISTS (
                               SELECT 1 FROM timelines t
                               WHERE t.capture_id = captures.id
                           )
                       )",
                )?;
                let rows = stmt
                    .query_map([current_ts_ms()], |row| row.get::<_, String>(3))?
                    .collect::<Result<Vec<_>, _>>()?;
                Ok(rows)
            })
            .unwrap();

        assert!(plan
            .iter()
            .any(|detail| detail.contains("idx_timelines_capture_id")));
    }

    #[test]
    fn optional_production_copy_capture_cleanup_finishes_without_full_scans() {
        let Ok(db_path) = std::env::var("MEMORY_BREAD_CAPTURE_CLEANUP_E2E_DB") else {
            return;
        };
        let mgr = StorageManager::open(std::path::Path::new(&db_path)).unwrap();
        let captures_dir = tempdir().unwrap();
        // 生产环境当前保留期为 14 天；副本基准必须覆盖真实候选规模。
        let cutoff = current_ts_ms() - 14 * 24 * 60 * 60 * 1000;
        let started = std::time::Instant::now();
        let (deleted, deleted_screenshots, freed_bytes) = mgr
            .run_old_captures_cleanup(cutoff, captures_dir.path())
            .unwrap();
        let elapsed = started.elapsed();

        eprintln!(
            "capture_cleanup_copy elapsed_ms={} deleted={} screenshots={} freed_bytes={}",
            elapsed.as_millis(),
            deleted,
            deleted_screenshots,
            freed_bytes
        );
        assert!(elapsed <= Duration::from_secs(3));
    }

    #[test]
    fn test_old_captures_cleanup_deletes_associated_screenshot_file() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let rel_path = "screenshots/old-associated.jpg";
        let bytes = b"associated screenshot";
        write_screenshot(dir.path(), rel_path, bytes);

        let old_ts = current_ts_ms() - 15 * 24 * 3600 * 1000;
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (ts, event_type, screenshot_path)
                 VALUES (?1, 'auto', ?2)",
                params![old_ts, rel_path],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() - 14 * 24 * 3600 * 1000;
        let (deleted, deleted_screenshots, freed) =
            mgr.run_old_captures_cleanup(cutoff, dir.path()).unwrap();

        assert_eq!(deleted, 1);
        assert_eq!(deleted_screenshots, 1);
        assert_eq!(freed, bytes.len() as u64);
        assert!(!dir.path().join(rel_path).exists());
    }

    #[test]
    fn test_old_captures_cleanup_keeps_meaningful_pending_backlog() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let rel_path = "screenshots/pending-backlog.jpg";
        let bytes = b"pending backlog screenshot";
        write_screenshot(dir.path(), rel_path, bytes);

        let old_ts = current_ts_ms() - 30 * 24 * 3600 * 1000;
        let capture_id = mgr
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        ts, event_type, ax_text, screenshot_path, is_sensitive, pii_scrubbed
                     ) VALUES (?1, 'auto', '仍待时间线提炼的有效内容', ?2, 0, 0)",
                    params![old_ts, rel_path],
                )?;
                Ok(conn.last_insert_rowid())
            })
            .unwrap();

        let cutoff = current_ts_ms() - 14 * 24 * 3600 * 1000;
        let (deleted, deleted_screenshots, freed) =
            mgr.run_old_captures_cleanup(cutoff, dir.path()).unwrap();

        assert_eq!(deleted, 0);
        assert_eq!(deleted_screenshots, 0);
        assert_eq!(freed, 0);
        assert!(dir.path().join(rel_path).exists());
        let remaining: i64 = mgr
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT COUNT(*) FROM captures WHERE id = ?1 AND timeline_id IS NULL",
                    params![capture_id],
                    |row| row.get(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        assert_eq!(remaining, 1);
    }

    #[test]
    fn test_screenshot_purge_keeps_meaningful_pending_backlog() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let rel_path = "screenshots/pending-evidence.jpg";
        write_screenshot(dir.path(), rel_path, b"pending evidence");

        let old_ts = current_ts_ms() - 120 * 24 * 3600 * 1000;
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (
                    ts, event_type, ocr_text, screenshot_path, is_sensitive, pii_scrubbed
                 ) VALUES (?1, 'auto', '等待提炼的 OCR 文本', ?2, 0, 0)",
                params![old_ts, rel_path],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() - 90 * 24 * 3600 * 1000;
        let (deleted, freed) = mgr.run_screenshot_purge(cutoff, dir.path()).unwrap();

        assert_eq!(deleted, 0);
        assert_eq!(freed, 0);
        assert!(dir.path().join(rel_path).exists());
    }

    #[test]
    fn test_old_captures_cleanup_deletes_orphan_screenshot_files() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let orphan_path = "screenshots/orphan.jpg";
        let referenced_path = "screenshots/still-referenced.jpg";
        let orphan_bytes = b"orphan screenshot";
        write_screenshot(dir.path(), orphan_path, orphan_bytes);
        write_screenshot(dir.path(), referenced_path, b"keep me");

        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (ts, event_type, screenshot_path, is_sensitive, pii_scrubbed)
                 VALUES (?1, 'snapshot_ref', ?2, 1, 1)",
                params![current_ts_ms(), referenced_path],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() + 1_000;
        let (deleted, deleted_screenshots, freed) =
            mgr.run_old_captures_cleanup(cutoff, dir.path()).unwrap();

        assert_eq!(deleted, 0);
        assert_eq!(deleted_screenshots, 1);
        assert_eq!(freed, orphan_bytes.len() as u64);
        assert!(!dir.path().join(orphan_path).exists());
        assert!(dir.path().join(referenced_path).exists());

        let logs = mgr.list_cleanup_logs(1).unwrap();
        assert!(logs[0]
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("orphan_screenshots=1"));
    }

    #[test]
    fn test_old_captures_cleanup_keeps_snapshot_refs() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let old_ts = current_ts_ms() - 180 * 24 * 3600 * 1000 - 1;

        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (ts, event_type, is_sensitive, pii_scrubbed)
                 VALUES (?1, 'snapshot_ref', 1, 1)",
                params![old_ts],
            )?;
            conn.execute(
                "INSERT INTO captures (ts, event_type)
                 VALUES (?1, 'auto')",
                params![old_ts],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() - 180 * 24 * 3600 * 1000;
        let (deleted, deleted_screenshots, freed) =
            mgr.run_old_captures_cleanup(cutoff, dir.path()).unwrap();
        assert_eq!(deleted, 1);
        assert_eq!(deleted_screenshots, 0);
        assert_eq!(freed, 0);

        let snapshot_refs: i64 = mgr
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT COUNT(*) FROM captures WHERE event_type = 'snapshot_ref'",
                    [],
                    |row| row.get(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        assert_eq!(snapshot_refs, 1);
    }

    #[test]
    fn test_screenshot_purge_missing_file() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();

        // 插入一条带截图路径的旧记录
        let old_ts = current_ts_ms() - 91 * 24 * 3600 * 1000;
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (ts, event_type, screenshot_path) VALUES (?1, 'auto', ?2)",
                params![old_ts, "nonexistent/shot.jpg"],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() - 90 * 24 * 3600 * 1000;
        let (deleted, freed) = mgr.run_screenshot_purge(cutoff, dir.path()).unwrap();
        assert_eq!(deleted, 1); // 文件不存在，但仍计为"已清理"
        assert_eq!(freed, 0); // 没有实际文件，释放 0 字节

        // 验证数据库路径已清除
        let row: Option<String> = mgr
            .with_conn(|conn| {
                conn.query_row("SELECT screenshot_path FROM captures LIMIT 1", [], |r| {
                    r.get(0)
                })
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        assert!(row.is_none());
    }

    #[test]
    fn test_vacuum() {
        let mgr = make_mgr();
        mgr.run_vacuum().unwrap();

        let logs = mgr.list_cleanup_logs(10).unwrap();
        assert!(!logs.is_empty());
        assert_eq!(logs[0].cleanup_type, "vacuum");
    }

    #[test]
    fn test_insert_and_cleanup_uses_capture_helper() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let cap = NewCapture {
            ts: 1_700_000_000_000,
            app_name: Some("TestApp".into()),
            app_bundle_id: None,
            win_title: None,
            event_type: EventType::Auto,
            ax_text: Some("test".into()),
            ax_focused_role: None,
            ax_focused_id: None,
            ocr_text: None,
            screenshot_path: None,
            screenshot_source: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            url: None,
            webpage_title: None,
        };
        mgr.insert_capture(&cap).unwrap();

        // 清理比该记录更新的时间（不应删除任何东西）
        let (deleted, _, _) = mgr
            .run_old_captures_cleanup(1_000_000_000_000, dir.path())
            .unwrap();
        assert_eq!(deleted, 0);
    }

    #[test]
    fn test_old_captures_cleanup_keeps_timeline_extracts() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let old_ts = current_ts_ms() - 15 * 24 * 3600 * 1000;
        let capture_id = mgr
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (ts, event_type, ax_text) VALUES (?1, 'auto', 'raw')",
                    params![old_ts],
                )?;
                Ok(conn.last_insert_rowid())
            })
            .unwrap();

        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO timelines (capture_id, summary, category)
                 VALUES (?1, 'timeline extract', 'meeting')",
                params![capture_id],
            )?;
            conn.execute(
                "INSERT INTO vector_index (
                     capture_id, qdrant_point_id, chunk_index, chunk_text,
                     model_name, created_at, source_type
                 )
                 VALUES (?1, 'retention-vector', 0, 'raw vector', 'test', ?2, 'capture')",
                params![capture_id, current_ts_ms()],
            )?;
            Ok(())
        })
        .unwrap();

        let cutoff = current_ts_ms() - 14 * 24 * 3600 * 1000;
        let (deleted, _, _) = mgr.run_old_captures_cleanup(cutoff, dir.path()).unwrap();
        assert_eq!(deleted, 1);

        let timeline_count: i64 = mgr
            .with_conn(|conn| {
                conn.query_row("SELECT COUNT(*) FROM timelines", [], |row| row.get(0))
                    .map_err(StorageError::Sqlite)
            })
            .unwrap();
        assert_eq!(timeline_count, 1);
        let queued_point: String = mgr
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT qdrant_point_id
                     FROM vector_deletion_queue
                     WHERE reason = 'capture_retention_expired'",
                    [],
                    |row| row.get(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        assert_eq!(queued_point, "retention-vector");
    }

    #[test]
    fn test_manual_capture_delete_removes_file_and_vector_metadata_but_keeps_timeline() {
        let mgr = make_mgr();
        let dir = tempdir().unwrap();
        let relative_path = "screenshots/manual-delete.jpg";
        write_screenshot(dir.path(), relative_path, b"manual delete screenshot");

        let capture_id = mgr
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (ts, event_type, ax_text, screenshot_path)
                     VALUES (?1, 'manual', '待删除采集内容', ?2)",
                    params![current_ts_ms(), relative_path],
                )?;
                let capture_id = conn.last_insert_rowid();
                conn.execute(
                    "INSERT INTO timelines (capture_id, summary, category)
                     VALUES (?1, '保留的时间线', 'meeting')",
                    params![capture_id],
                )?;
                let timeline_id = conn.last_insert_rowid();
                conn.execute(
                    "UPDATE captures SET timeline_id = ?1 WHERE id = ?2",
                    params![timeline_id, capture_id],
                )?;
                conn.execute(
                    "INSERT INTO data_sources (
                        canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                     ) VALUES ('manual-delete-source', '保留的数据源', 'work_memory',
                        'memory_only', 'never', 'observed', '[]', 1, 1, 'active', 1, 1)",
                    [],
                )?;
                let source_id = conn.last_insert_rowid();
                conn.execute(
                    "INSERT INTO data_source_links (
                        source_id, source_ref_key, capture_id, timeline_id, link_kind,
                        observed_at, created_at
                     ) VALUES (?1, 'manual-delete-link', ?2, ?3, 'work_memory', 1, 1)",
                    params![source_id, capture_id, timeline_id],
                )?;
                conn.execute(
                    "INSERT INTO vector_index
                     (capture_id, qdrant_point_id, chunk_index, chunk_text, model_name, created_at)
                     VALUES (?1, 'manual-delete-vector', 0, '待删除向量', 'test', ?2)",
                    params![capture_id, current_ts_ms()],
                )?;
                Ok(capture_id)
            })
            .unwrap();

        assert!(mgr
            .delete_capture_with_assets(capture_id, dir.path())
            .unwrap());
        assert!(!dir.path().join(relative_path).exists());
        assert!(mgr.get_capture(capture_id).unwrap().is_none());

        let (timeline_count, vector_count, queued_count, direct_capture_links): (i64, i64, i64, i64) = mgr
            .with_conn(|conn| {
                Ok((
                    conn.query_row("SELECT COUNT(*) FROM timelines", [], |row| row.get(0))?,
                    conn.query_row("SELECT COUNT(*) FROM vector_index", [], |row| row.get(0))?,
                    conn.query_row(
                        "SELECT COUNT(*) FROM vector_deletion_queue
                         WHERE qdrant_point_id = 'manual-delete-vector'
                           AND reason = 'capture_deleted_by_user'",
                        [],
                        |row| row.get(0),
                    )?,
                    conn.query_row(
                        "SELECT COUNT(*) FROM data_source_links
                         WHERE source_ref_key = 'manual-delete-link'
                           AND capture_id IS NOT NULL",
                        [],
                        |row| row.get(0),
                    )?,
                ))
            })
            .unwrap();
        assert_eq!(timeline_count, 1);
        assert_eq!(vector_count, 0);
        assert_eq!(queued_count, 1);
        assert_eq!(direct_capture_links, 0);
    }
}
