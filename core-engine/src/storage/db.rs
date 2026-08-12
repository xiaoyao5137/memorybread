//! StorageManager — 数据库连接管理与迁移执行
//!
//! # 设计要点
//!
//! - 使用 `Arc<Mutex<Connection>>` 在多线程间共享单一写连接
//! - WAL 模式允许读操作与写操作并发，不互相阻塞
//! - 所有阻塞 SQLite 调用通过 `tokio::task::spawn_blocking` 移出 async 线程
//! - 迁移 SQL 内嵌于二进制，应用启动时自动执行，无需外部文件

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::Connection;
use tracing::{debug, info, warn};

use super::error::StorageError;

// ─────────────────────────────────────────────────────────────────────────────
// 内嵌迁移 SQL
// ─────────────────────────────────────────────────────────────────────────────

/// 按版本顺序排列的迁移列表：(版本号, SQL)
static MIGRATIONS: &[(&str, &str)] = &[
    ("001_init", include_str!("migrations/001_init.sql")),
    (
        "002_seed_defaults",
        include_str!("migrations/002_seed_defaults.sql"),
    ),
    ("003_views", include_str!("migrations/003_views.sql")),
    (
        "004_captures_knowledge_id",
        include_str!("../../../shared/db-schema/migrations/004_captures_knowledge_id.sql"),
    ),
    (
        "005_monitor_tables",
        include_str!("../../../shared/db-schema/migrations/005_monitor_tables.sql"),
    ),
    (
        "006_monitor_metric_scopes",
        include_str!("../../../shared/db-schema/migrations/006_monitor_metric_scopes.sql"),
    ),
    (
        "007_vector_index_rag_metadata",
        include_str!("../../../shared/db-schema/migrations/007_vector_index_rag_metadata.sql"),
    ),
    (
        "008_knowledge_semantic_metadata",
        include_str!("../../../shared/db-schema/migrations/008_knowledge_semantic_metadata.sql"),
    ),
    (
        "009_bake_templates",
        include_str!("migrations/009_bake_templates.sql"),
    ),
    (
        "010_knowledge_entries",
        include_str!("migrations/010_knowledge_entries.sql"),
    ),
    (
        "011_bake_pipeline",
        include_str!("migrations/011_bake_pipeline.sql"),
    ),
    (
        "012_fix_knowledge_fts_triggers",
        include_str!("migrations/012_fix_knowledge_fts_triggers.sql"),
    ),
    (
        "013_rebuild_knowledge_fts",
        include_str!("migrations/013_rebuild_knowledge_fts.sql"),
    ),
    (
        "014_add_knowledge_timestamp_ms",
        include_str!("migrations/014_add_knowledge_timestamp_ms.sql"),
    ),
    (
        "015_split_knowledge_tables",
        include_str!("migrations/015_split_knowledge_tables.sql"),
    ),
    (
        "016_fix_split_tables_fts_triggers",
        include_str!("migrations/016_fix_split_tables_fts_triggers.sql"),
    ),
    (
        "018_create_bake_designs",
        include_str!("migrations/018_create_bake_designs.sql"),
    ),
    (
        "019_rename_to_timelines",
        include_str!("migrations/019_rename_to_timelines.sql"),
    ),
    (
        "020_add_detailed_content",
        include_str!("migrations/020_add_detailed_content.sql"),
    ),
    (
        "021_unify_bake_designs",
        include_str!("migrations/021_unify_bake_designs.sql"),
    ),
    (
        "022_fix_bake_fts_delete_triggers",
        include_str!("migrations/022_fix_bake_fts_delete_triggers.sql"),
    ),
    (
        "023_rename_bake_run_design_count",
        include_str!("migrations/023_rename_bake_run_design_count.sql"),
    ),
    (
        "024_create_bake_documents",
        include_str!("migrations/024_create_bake_documents.sql"),
    ),
    (
        "025_add_capture_web_source",
        include_str!("migrations/025_add_capture_web_source.sql"),
    ),
    (
        "026_add_capture_screenshot_source",
        include_str!("migrations/026_add_capture_screenshot_source.sql"),
    ),
    (
        "027_bake_retry_state",
        include_str!("migrations/027_bake_retry_state.sql"),
    ),
    (
        "028_remove_bake_manual_review",
        include_str!("migrations/028_remove_bake_manual_review.sql"),
    ),
    (
        "029_rename_capture_knowledge_id_to_timeline_id",
        include_str!("migrations/029_rename_capture_knowledge_id_to_timeline_id.sql"),
    ),
    (
        "030_archive_legacy_bake_article_timelines",
        include_str!("migrations/030_archive_legacy_bake_article_timelines.sql"),
    ),
    (
        "031_ensure_full_schema",
        include_str!("migrations/031_ensure_full_schema.sql"),
    ),
    (
        "032_restore_bake_article_from_legacy",
        include_str!("migrations/032_restore_bake_article_from_legacy.sql"),
    ),
    (
        "033_drop_bake_episodic_memory_id",
        include_str!("migrations/033_drop_bake_episodic_memory_id.sql"),
    ),
    (
        "034_create_creation_history",
        include_str!("migrations/034_create_creation_history.sql"),
    ),
    (
        "035_seed_privacy_defaults",
        include_str!("migrations/035_seed_privacy_defaults.sql"),
    ),
    (
        "036_seed_capture_retention_days",
        include_str!("migrations/036_seed_capture_retention_days.sql"),
    ),
    (
        "037_add_model_to_history",
        include_str!("migrations/037_add_model_to_history.sql"),
    ),
    (
        "038_add_latency_to_creation_history",
        include_str!("migrations/038_add_latency_to_creation_history.sql"),
    ),
    (
        "039_create_diaries",
        include_str!("migrations/039_create_diaries.sql"),
    ),
    (
        "040_update_default_capture_interval",
        include_str!("migrations/040_update_default_capture_interval.sql"),
    ),
    (
        "041_due_diary_catchup_tasks",
        include_str!("migrations/041_due_diary_catchup_tasks.sql"),
    ),
    (
        "042_seed_default_diary_tasks",
        include_str!("migrations/042_seed_default_diary_tasks.sql"),
    ),
    (
        "043_normalize_scheduled_task_cron",
        include_str!("migrations/043_normalize_scheduled_task_cron.sql"),
    ),
    (
        "044_correct_weekday_semantics",
        include_str!("migrations/044_correct_weekday_semantics.sql"),
    ),
    (
        "045_remove_daily_diary_future_plans",
        include_str!("migrations/045_remove_daily_diary_future_plans.sql"),
    ),
    (
        "046_seed_energy_saving_mode",
        include_str!("migrations/046_seed_energy_saving_mode.sql"),
    ),
    (
        "047_update_diary_timeline_sources",
        include_str!("migrations/047_update_diary_timeline_sources.sql"),
    ),
    (
        "048_preserve_existing_capture_runtime",
        include_str!("migrations/048_preserve_existing_capture_runtime.sql"),
    ),
    (
        "049_create_creation_skills",
        include_str!("migrations/049_create_creation_skills.sql"),
    ),
    (
        "050_add_creation_skill_lifecycle",
        include_str!("migrations/050_add_creation_skill_lifecycle.sql"),
    ),
    (
        "051_expand_creation_skill_examples",
        include_str!("migrations/051_expand_creation_skill_examples.sql"),
    ),
    (
        "052_add_creation_skill_market_source",
        include_str!("migrations/052_add_creation_skill_market_source.sql"),
    ),
    (
        "053_backfill_document_timeline_metadata",
        include_str!("migrations/053_backfill_document_timeline_metadata.sql"),
    ),
    (
        "054_task_notification_channels",
        include_str!("migrations/054_task_notification_channels.sql"),
    ),
    (
        "055_add_creation_agent_history",
        include_str!("migrations/055_add_creation_agent_history.sql"),
    ),
    (
        "056_creation_revision_context",
        include_str!("migrations/056_creation_revision_context.sql"),
    ),
    (
        "057_add_creation_skill_package_files",
        include_str!("migrations/057_add_creation_skill_package_files.sql"),
    ),
    (
        "058_add_creation_skill_distinctive_sections",
        include_str!("migrations/058_add_creation_skill_distinctive_sections.sql"),
    ),
    (
        "059_seed_other_privacy_filter",
        include_str!("migrations/059_seed_other_privacy_filter.sql"),
    ),
    (
        "060_add_creation_skill_description",
        include_str!("migrations/060_add_creation_skill_description.sql"),
    ),
    (
        "061_requeue_historical_bake_timeouts",
        include_str!("migrations/061_requeue_historical_bake_timeouts.sql"),
    ),
    (
        "062_document_artifact_identity",
        include_str!("migrations/062_document_artifact_identity.sql"),
    ),
    (
        "063_durable_artifact_vectors",
        include_str!("migrations/063_durable_artifact_vectors.sql"),
    ),
    (
        "064_data_memory_module",
        include_str!("../../../shared/db-schema/migrations/064_data_memory_module.sql"),
    ),
    (
        "065_allow_browser_attach_snapshots",
        include_str!("../../../shared/db-schema/migrations/065_allow_browser_attach_snapshots.sql"),
    ),
    (
        "066_creation_evidence_latest_data",
        include_str!("../../../shared/db-schema/migrations/066_creation_evidence_latest_data.sql"),
    ),
    (
        "067_integration_skill_runs",
        include_str!("migrations/067_integration_skill_runs.sql"),
    ),
    (
        "068_timeline_data_facts",
        include_str!("../../../shared/db-schema/migrations/068_timeline_data_facts.sql"),
    ),
    (
        "069_creation_skill_manual_source",
        include_str!("migrations/069_creation_skill_manual_source.sql"),
    ),
    (
        "070_requeue_bake_output_failures",
        include_str!("migrations/070_requeue_bake_output_failures.sql"),
    ),
    (
        "071_timelines_fts",
        include_str!("migrations/071_timelines_fts.sql"),
    ),
    (
        "072_requeue_bake_output_failures_v2",
        include_str!("migrations/072_requeue_bake_output_failures_v2.sql"),
    ),
    (
        "073_capture_attempt_audit",
        include_str!("../../../shared/db-schema/migrations/073_capture_attempt_audit.sql"),
    ),
    (
        "074_data_snapshot_period_history",
        include_str!("../../../shared/db-schema/migrations/074_data_snapshot_period_history.sql"),
    ),
    (
        "075_timeline_data_fact_period_history",
        include_str!(
            "../../../shared/db-schema/migrations/075_timeline_data_fact_period_history.sql"
        ),
    ),
    (
        "076_remove_creation_skill_structure_pattern",
        include_str!(
            "../../../shared/db-schema/migrations/076_remove_creation_skill_structure_pattern.sql"
        ),
    ),
    (
        "077_bake_retry_schedule",
        include_str!("migrations/077_bake_retry_schedule.sql"),
    ),
    (
        "078_local_breadcrumbs",
        include_str!("../../../shared/db-schema/migrations/078_local_breadcrumbs.sql"),
    ),
    (
        "079_breadcrumb_rule_activation",
        include_str!("../../../shared/db-schema/migrations/079_breadcrumb_rule_activation.sql"),
    ),
];

// ─────────────────────────────────────────────────────────────────────────────
// StorageManager
// ─────────────────────────────────────────────────────────────────────────────

/// 持有 SQLite 连接的核心管理器。
///
/// 设计为可跨线程共享（`Clone` 复制的是 `Arc`，不复制连接本身）。
#[derive(Clone)]
pub struct StorageManager {
    pub(crate) conn: Arc<Mutex<Connection>>,
}

impl StorageManager {
    // ── 初始化 ───────────────────────────────────────────────────────────────

    /// 打开（或创建）数据库，执行所有待执行的迁移，返回管理器实例。
    ///
    /// `db_path` 通常为 `~/.memory-bread/memory-bread.db`。
    pub fn open(db_path: &Path) -> Result<Self, StorageError> {
        // 确保父目录存在
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| StorageError::MigrationFailed {
                version: "open",
                reason: e.to_string(),
            })?;
        }

        let conn = Connection::open(db_path)?;
        Self::configure_connection(&conn)?;

        let mgr = Self {
            conn: Arc::new(Mutex::new(conn)),
        };
        mgr.run_migrations()?;
        mgr.with_conn(|conn| {
            conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);")?;
            Ok(())
        })?;

        info!("StorageManager 初始化完成: {}", db_path.display());
        Ok(mgr)
    }

    /// 打开内存数据库（仅用于测试）。
    #[cfg(test)]
    pub fn open_in_memory() -> Result<Self, StorageError> {
        let conn = Connection::open_in_memory()?;
        Self::configure_connection(&conn)?;
        let mgr = Self {
            conn: Arc::new(Mutex::new(conn)),
        };
        mgr.run_migrations()?;
        Ok(mgr)
    }

    // ── 连接配置 ─────────────────────────────────────────────────────────────

    fn configure_connection(conn: &Connection) -> Result<(), StorageError> {
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA foreign_keys = ON;
             PRAGMA synchronous   = NORMAL;
             PRAGMA temp_store    = MEMORY;
             PRAGMA mmap_size     = 268435456;", // 256 MB mmap，提升读性能
        )?;
        debug!("SQLite PRAGMA 配置完成");
        Ok(())
    }

    // ── 迁移执行 ─────────────────────────────────────────────────────────────

    fn run_migrations(&self) -> Result<(), StorageError> {
        let conn = self.conn.lock().unwrap_or_else(|poisoned| {
            warn!("数据库连接锁曾因线程 panic 中毒，已恢复连接访问");
            poisoned.into_inner()
        });

        // 确保迁移记录表存在（迁移前的最小依赖）
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT    PRIMARY KEY,
                applied_at INTEGER NOT NULL
            );",
        )?;

        for (version, sql) in MIGRATIONS {
            let already_applied: bool = conn.query_row(
                "SELECT COUNT(*) > 0 FROM schema_migrations WHERE version = ?1",
                rusqlite::params![version],
                |row| row.get(0),
            )?;

            if already_applied {
                debug!("迁移 {} 已执行，跳过", version);
                continue;
            }

            if *version == "019_rename_to_timelines"
                && self.timelines_table_already_exists(&conn)?
            {
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )?;
                info!("迁移 {} 已由现有 schema 满足，登记后跳过", version);
                continue;
            }

            if *version == "029_rename_capture_knowledge_id_to_timeline_id"
                && self.capture_timeline_column_already_renamed(&conn)?
            {
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )?;
                info!("迁移 {} 已由现有 schema 满足，登记后跳过", version);
                continue;
            }

            if *version == "033_drop_bake_episodic_memory_id"
                && self.bake_legacy_memory_columns_already_dropped(&conn)?
            {
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )?;
                info!("迁移 {} 已由现有 schema 满足，登记后跳过", version);
                continue;
            }

            if *version == "031_ensure_full_schema" {
                self.run_ensure_full_schema(&conn)?;
                let count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                    rusqlite::params![version],
                    |row| row.get(0),
                )?;
                if count == 0 {
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                        rusqlite::params![version, current_ts_ms()],
                    )?;
                }
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "037_add_model_to_history" {
                Self::add_column_if_missing(&conn, "creation_history", "model", "TEXT")?;
                Self::add_column_if_missing(&conn, "creation_history", "references_json", "TEXT")?;
                Self::add_column_if_missing(&conn, "rag_sessions", "model", "TEXT")?;
                let count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                    rusqlite::params![version],
                    |row| row.get(0),
                )?;
                if count == 0 {
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                        rusqlite::params![version, current_ts_ms()],
                    )?;
                }
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "038_add_latency_to_creation_history" {
                Self::add_column_if_missing(&conn, "creation_history", "latency_ms", "INTEGER")?;
                let count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                    rusqlite::params![version],
                    |row| row.get(0),
                )?;
                if count == 0 {
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                        rusqlite::params![version, current_ts_ms()],
                    )?;
                }
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "055_add_creation_agent_history" {
                Self::add_column_if_missing(&conn, "creation_history", "session_id", "TEXT")?;
                Self::add_column_if_missing(
                    &conn,
                    "creation_history",
                    "conversation_json",
                    "TEXT",
                )?;
                Self::add_column_if_missing(&conn, "creation_history", "agent_trace_json", "TEXT")?;
                Self::add_column_if_missing(&conn, "creation_history", "goal_json", "TEXT")?;
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_creation_history_session
                     ON creation_history(session_id, created_at DESC)",
                    [],
                )?;
                let count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                    rusqlite::params![version],
                    |row| row.get(0),
                )?;
                if count == 0 {
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                        rusqlite::params![version, current_ts_ms()],
                    )?;
                }
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "056_creation_revision_context" {
                Self::add_column_if_missing(&conn, "creation_history", "root_request", "TEXT")?;
                Self::add_column_if_missing(
                    &conn,
                    "creation_history",
                    "parent_history_id",
                    "INTEGER",
                )?;
                Self::add_column_if_missing(
                    &conn,
                    "creation_history",
                    "revision_no",
                    "INTEGER NOT NULL DEFAULT 1",
                )?;
                Self::add_column_if_missing(
                    &conn,
                    "creation_history",
                    "edit_operation",
                    "TEXT NOT NULL DEFAULT 'create_document'",
                )?;
                Self::add_column_if_missing(
                    &conn,
                    "creation_history",
                    "document_patch_json",
                    "TEXT",
                )?;
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_creation_history_session_revision
                     ON creation_history(session_id, revision_no DESC, created_at DESC)",
                    [],
                )?;
                let count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                    rusqlite::params![version],
                    |row| row.get(0),
                )?;
                if count == 0 {
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                        rusqlite::params![version, current_ts_ms()],
                    )?;
                }
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "066_creation_evidence_latest_data" {
                // 066 同时包含 ADD COLUMN 和幂等表/索引创建。先补列再执行
                // 不含 ADD COLUMN 的剩余契约，兼容迁移中断后重新启动。
                Self::add_column_if_missing(&conn, "creation_history", "evidence_json", "TEXT")?;
                conn.execute_batch(
                    "BEGIN IMMEDIATE;
                     DELETE FROM data_snapshots
                     WHERE id NOT IN (
                        SELECT latest.id FROM data_snapshots latest
                        WHERE latest.id = (
                            SELECT candidate.id FROM data_snapshots candidate
                            WHERE candidate.source_id = latest.source_id
                            ORDER BY candidate.collected_at DESC, candidate.id DESC LIMIT 1
                        )
                     );
                     CREATE UNIQUE INDEX IF NOT EXISTS idx_data_snapshots_single_latest
                     ON data_snapshots(source_id);
                     CREATE TABLE IF NOT EXISTS creation_evidence_assets (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        history_id INTEGER REFERENCES creation_history(id) ON DELETE SET NULL,
                        source_id INTEGER REFERENCES data_sources(id) ON DELETE SET NULL,
                        data_snapshot_id INTEGER REFERENCES data_snapshots(id) ON DELETE SET NULL,
                        source_url TEXT NOT NULL,
                        page_title TEXT NOT NULL,
                        captured_at INTEGER NOT NULL,
                        image_path TEXT NOT NULL,
                        mime_type TEXT NOT NULL DEFAULT 'image/jpeg',
                        width INTEGER NOT NULL,
                        height INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        screenshot_source TEXT NOT NULL,
                        validation_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (validation_status IN ('pending', 'verified', 'rejected')),
                        validation_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                     );
                     CREATE INDEX IF NOT EXISTS idx_creation_evidence_run
                     ON creation_evidence_assets(run_id, captured_at DESC);
                     CREATE INDEX IF NOT EXISTS idx_creation_evidence_history
                     ON creation_evidence_assets(history_id, captured_at DESC);
                     COMMIT;",
                )
                .map_err(|e| StorageError::MigrationFailed {
                    version,
                    reason: e.to_string(),
                })?;
                let count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                    rusqlite::params![version],
                    |row| row.get(0),
                )?;
                if count == 0 {
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                        rusqlite::params![version, current_ts_ms()],
                    )?;
                }
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "062_document_artifact_identity" {
                // ADD COLUMN 本身不支持 IF NOT EXISTS。先用 Rust 幂等补列，再执行
                // 其余可重复 SQL，避免迁移中断后下次启动卡在 duplicate column。
                Self::add_column_if_missing(&conn, "bake_documents", "document_identity", "TEXT")?;
                conn.execute_batch(sql)
                    .map_err(|e| StorageError::MigrationFailed {
                        version,
                        reason: e.to_string(),
                    })?;
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at)
                     VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )?;
                info!("迁移 {} 执行成功", version);
                continue;
            }

            if *version == "079_breadcrumb_rule_activation" {
                // SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS。若进程在补列后、登记
                // schema_migrations 前退出，后续启动必须能够从这个中间状态恢复。
                Self::add_column_if_missing(
                    &conn,
                    "breadcrumb_rules",
                    "is_active",
                    "INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))",
                )?;
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at)
                     VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )?;
                info!("迁移 {} 执行成功", version);
                continue;
            }

            info!("执行迁移: {}", version);
            conn.execute_batch(sql)
                .map_err(|e| StorageError::MigrationFailed {
                    version,
                    reason: e.to_string(),
                })?;

            // 如果迁移 SQL 本身没有插入迁移记录，这里补插
            // （001_init.sql 末尾已有 INSERT，此处做幂等保护）
            let count: i64 = conn.query_row(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                rusqlite::params![version],
                |row| row.get(0),
            )?;
            if count == 0 {
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )?;
            }

            info!("迁移 {} 执行成功", version);
        }

        self.run_compatibility_schema_repairs(&conn)?;

        Ok(())
    }

    fn run_compatibility_schema_repairs(&self, conn: &Connection) -> Result<(), StorageError> {
        // 部分本地库在 references_json 出现前就已登记 037 迁移完成。
        // 这里做一次幂等修复，避免创作历史 API 因缺列无法读写旧记录。
        if self.table_exists(conn, "creation_history")? {
            Self::add_column_if_missing(conn, "creation_history", "model", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "references_json", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "latency_ms", "INTEGER")?;
            Self::add_column_if_missing(conn, "creation_history", "session_id", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "conversation_json", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "agent_trace_json", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "goal_json", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "root_request", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "parent_history_id", "INTEGER")?;
            Self::add_column_if_missing(
                conn,
                "creation_history",
                "revision_no",
                "INTEGER NOT NULL DEFAULT 1",
            )?;
            Self::add_column_if_missing(
                conn,
                "creation_history",
                "edit_operation",
                "TEXT NOT NULL DEFAULT 'create_document'",
            )?;
            Self::add_column_if_missing(conn, "creation_history", "document_patch_json", "TEXT")?;
            Self::add_column_if_missing(conn, "creation_history", "evidence_json", "TEXT")?;
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creation_history_session
                 ON creation_history(session_id, created_at DESC)",
                [],
            )?;
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creation_history_session_revision
                 ON creation_history(session_id, revision_no DESC, created_at DESC)",
                [],
            )?;
        }

        // 极少数残缺旧库只保留了迁移记录和部分业务表；039 会更新
        // scheduled_tasks，因此仅在其基础表存在时执行兼容修复。
        if self.table_exists(conn, "scheduled_tasks")? {
            conn.execute_batch(include_str!("migrations/039_create_diaries.sql"))?;
        }

        Ok(())
    }

    fn table_exists(&self, conn: &Connection, table: &str) -> Result<bool, StorageError> {
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
            rusqlite::params![table],
            |row| row.get(0),
        )?;
        Ok(count > 0)
    }

    fn has_column(conn: &Connection, table: &str, col: &str) -> Result<bool, StorageError> {
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM pragma_table_info(?1) WHERE name = ?2",
            rusqlite::params![table, col],
            |row| row.get(0),
        )?;
        Ok(count > 0)
    }

    fn add_column_if_missing(
        conn: &Connection,
        table: &str,
        col: &str,
        col_def: &str,
    ) -> Result<(), StorageError> {
        if !Self::has_column(conn, table, col)? {
            conn.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {col} {col_def};"))?;
        }
        Ok(())
    }

    fn run_ensure_full_schema(&self, conn: &Connection) -> Result<(), StorageError> {
        // timelines 补列
        Self::add_column_if_missing(conn, "timelines", "created_at_ms", "INTEGER")?;
        Self::add_column_if_missing(conn, "timelines", "updated_at_ms", "INTEGER")?;
        Self::add_column_if_missing(conn, "timelines", "time_range_start", "INTEGER")?;
        Self::add_column_if_missing(conn, "timelines", "time_range_end", "INTEGER")?;
        Self::add_column_if_missing(conn, "timelines", "key_timestamps", "TEXT")?;

        // captures 补列
        Self::add_column_if_missing(conn, "captures", "url", "TEXT")?;
        Self::add_column_if_missing(conn, "captures", "webpage_title", "TEXT")?;
        Self::add_column_if_missing(conn, "captures", "screenshot_source", "TEXT")?;
        Self::add_column_if_missing(conn, "captures", "timeline_id", "INTEGER")?;

        // bake_knowledge 补列
        Self::add_column_if_missing(conn, "bake_knowledge", "detailed_content", "TEXT")?;
        Self::add_column_if_missing(conn, "bake_knowledge", "document_id", "INTEGER")?;
        Self::add_column_if_missing(conn, "bake_knowledge", "section_ids", "TEXT DEFAULT '[]'")?;
        Self::add_column_if_missing(
            conn,
            "bake_knowledge",
            "source_timeline_ids",
            "TEXT DEFAULT '[]'",
        )?;
        Self::add_column_if_missing(
            conn,
            "bake_knowledge",
            "source_capture_ids",
            "TEXT NOT NULL DEFAULT '[]'",
        )?;
        // 023 迁移可能未真正执行：episodic_memory_id → timeline_id
        if Self::has_column(conn, "bake_knowledge", "episodic_memory_id")?
            && !Self::has_column(conn, "bake_knowledge", "timeline_id")?
        {
            conn.execute_batch(
                "ALTER TABLE bake_knowledge ADD COLUMN timeline_id INTEGER;
                 UPDATE bake_knowledge SET timeline_id = episodic_memory_id WHERE timeline_id IS NULL;",
            )?;
        }

        // 023 迁移可能未真正执行：template_created_count → design_created_count
        if Self::has_column(conn, "bake_runs", "template_created_count")?
            && !Self::has_column(conn, "bake_runs", "design_created_count")?
        {
            conn.execute_batch(
                "ALTER TABLE bake_runs ADD COLUMN design_created_count INTEGER NOT NULL DEFAULT 0;
                 UPDATE bake_runs SET design_created_count = template_created_count WHERE design_created_count = 0;",
            )?;
        }

        // bake_sops 补列
        Self::add_column_if_missing(conn, "bake_sops", "timeline_id", "INTEGER")?;
        Self::add_column_if_missing(conn, "bake_sops", "detailed_content", "TEXT")?;
        Self::add_column_if_missing(conn, "bake_sops", "source_capture_ids", "TEXT DEFAULT '[]'")?;

        // vector_index 补列
        Self::add_column_if_missing(conn, "vector_index", "document_id", "INTEGER")?;
        Self::add_column_if_missing(conn, "vector_index", "section_id", "INTEGER")?;

        // 缺失的表（CREATE TABLE IF NOT EXISTS 本身幂等，直接执行 SQL 片段）
        conn.execute_batch(include_str!("migrations/031_ensure_full_schema.sql"))?;

        Ok(())
    }

    fn timelines_table_already_exists(&self, conn: &Connection) -> Result<bool, StorageError> {
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='timelines'",
            [],
            |row| row.get(0),
        )?;
        Ok(count > 0)
    }

    fn capture_timeline_column_already_renamed(
        &self,
        conn: &Connection,
    ) -> Result<bool, StorageError> {
        let mut stmt = conn.prepare("PRAGMA table_info(captures)")?;
        let columns = stmt
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<Result<Vec<_>, _>>()?;
        let has_timeline_id = columns.iter().any(|name| name == "timeline_id");
        let has_knowledge_id = columns.iter().any(|name| name == "knowledge_id");
        Ok(has_timeline_id && !has_knowledge_id)
    }

    fn bake_legacy_memory_columns_already_dropped(
        &self,
        conn: &Connection,
    ) -> Result<bool, StorageError> {
        Ok(Self::has_column(conn, "bake_knowledge", "timeline_id")?
            && !Self::has_column(conn, "bake_knowledge", "episodic_memory_id")?
            && Self::has_column(conn, "bake_sops", "timeline_id")?
            && !Self::has_column(conn, "bake_sops", "episodic_memory_id")?)
    }

    // ── 工具方法 ─────────────────────────────────────────────────────────────

    /// 在持有连接锁的情况下执行一个同步闭包。
    ///
    /// 所有 repo 方法都通过此函数访问连接，避免到处 `lock().unwrap()`。
    pub fn with_conn<F, T>(&self, f: F) -> Result<T, StorageError>
    where
        F: FnOnce(&Connection) -> Result<T, StorageError>,
    {
        let conn = self.conn.lock().unwrap_or_else(|poisoned| {
            warn!("数据库连接锁曾因线程 panic 中毒，已恢复连接访问");
            poisoned.into_inner()
        });
        f(&conn)
    }

    /// 将同步 `with_conn` 包装为 async，内部使用 `spawn_blocking`。
    ///
    /// 调用者传入的闭包在独立线程池线程上执行，不会阻塞 tokio 运行时。
    pub async fn with_conn_async<F, T>(&self, f: F) -> Result<T, StorageError>
    where
        F: FnOnce(&Connection) -> Result<T, StorageError> + Send + 'static,
        T: Send + 'static,
    {
        let conn_arc = self.conn.clone();
        tokio::task::spawn_blocking(move || {
            let conn = conn_arc.lock().unwrap_or_else(|poisoned| {
                warn!("数据库连接锁曾因线程 panic 中毒，已恢复连接访问");
                poisoned.into_inner()
            });
            f(&conn)
        })
        .await?
    }

    /// 获取数据库文件路径（用于调试和统计）。
    pub fn db_path(&self) -> String {
        self.with_conn(|conn| {
            conn.path()
                .map(|p| p.to_string())
                .ok_or_else(|| StorageError::NotFound("数据库路径".to_string()))
        })
        .unwrap_or_else(|_| ":memory:".to_string())
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 工具
// ─────────────────────────────────────────────────────────────────────────────

pub fn current_ts_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creation_skill_structure_migration_drops_hidden_legacy_data() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE creation_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_skill_key TEXT NOT NULL UNIQUE,
                cloud_skill_id TEXT,
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                category_id TEXT,
                common_titles TEXT NOT NULL DEFAULT '[]',
                title_style TEXT NOT NULL DEFAULT '',
                text_style TEXT NOT NULL DEFAULT '',
                diagram_style TEXT NOT NULL DEFAULT '',
                structure_pattern TEXT NOT NULL DEFAULT '[]',
                writing_guidelines TEXT NOT NULL DEFAULT '[]',
                published INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_at INTEGER,
                status TEXT NOT NULL DEFAULT 'saved',
                installed INTEGER NOT NULL DEFAULT 0,
                section_headings TEXT NOT NULL DEFAULT '{}',
                field_examples TEXT NOT NULL DEFAULT '{}',
                example_document TEXT NOT NULL DEFAULT '',
                package_files TEXT NOT NULL DEFAULT '[]',
                distinctive_sections TEXT NOT NULL DEFAULT '[]',
                skill_description TEXT NOT NULL DEFAULT '{}',
                execution_steps TEXT NOT NULL DEFAULT '[]'
            );
            INSERT INTO creation_skills (
                client_skill_key, source_kind, source_id, title, summary,
                structure_pattern, section_headings, field_examples,
                created_at, updated_at
            ) VALUES (
                'legacy-skill', 'manual', 'legacy-skill', '旧技能', '旧技能摘要',
                '[\"辅助明细\"]',
                '{\"common_titles\":\"标题设计风格\",\"structure_pattern\":\"章节组织骨架\",\"structurePattern\":\"旧驼峰字段\"}',
                '{\"common_titles\":[\"示例标题\"],\"structure_pattern\":[\"辅助明细\"],\"structurePattern\":[\"旧驼峰示例\"]}',
                1, 2
            );",
        )
        .unwrap();

        conn.execute_batch(include_str!(
            "../../../shared/db-schema/migrations/076_remove_creation_skill_structure_pattern.sql"
        ))
        .unwrap();

        let structure_column_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('creation_skills')
                 WHERE name = 'structure_pattern'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let (headings, examples, title): (String, String, String) = conn
            .query_row(
                "SELECT section_headings, field_examples, title
                 FROM creation_skills WHERE client_skill_key = 'legacy-skill'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();

        assert_eq!(structure_column_count, 0);
        assert_eq!(title, "旧技能");
        assert!(!headings.contains("structure_pattern"));
        assert!(!headings.contains("structurePattern"));
        assert!(headings.contains("common_titles"));
        assert!(!examples.contains("structure_pattern"));
        assert!(!examples.contains("structurePattern"));
        assert!(examples.contains("示例标题"));
    }

    #[test]
    fn open_repairs_creation_history_schema_when_old_migration_was_marked_applied() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("legacy.db");

        {
            let conn = Connection::open(&db).unwrap();
            conn.execute_batch(
                "CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                );
                CREATE TABLE creation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt TEXT NOT NULL,
                    generated_content TEXT NOT NULL,
                    doc_type TEXT,
                    audience TEXT,
                    reference_count INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    model TEXT,
                    latency_ms INTEGER
                );",
            )
            .unwrap();

            for (version, _) in MIGRATIONS {
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                    rusqlite::params![version, current_ts_ms()],
                )
                .unwrap();
            }
        }

        let storage = StorageManager::open(&db).unwrap();
        storage
            .with_conn(|conn| {
                assert!(StorageManager::has_column(
                    conn,
                    "creation_history",
                    "references_json"
                )?);
                assert!(StorageManager::has_column(
                    conn,
                    "creation_history",
                    "model"
                )?);
                assert!(StorageManager::has_column(
                    conn,
                    "creation_history",
                    "latency_ms"
                )?);
                assert!(StorageManager::has_column(
                    conn,
                    "creation_history",
                    "root_request"
                )?);
                assert!(StorageManager::has_column(
                    conn,
                    "creation_history",
                    "document_patch_json"
                )?);
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn breadcrumb_activation_migration_recovers_when_column_exists_without_record() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("interrupted-breadcrumb-migration.db");

        let storage = StorageManager::open(&db).unwrap();
        drop(storage);

        let conn = Connection::open(&db).unwrap();
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = '079_breadcrumb_rule_activation'",
            [],
        )
        .unwrap();
        drop(conn);

        let storage = StorageManager::open(&db).unwrap();
        storage
            .with_conn(|conn| {
                assert!(StorageManager::has_column(
                    conn,
                    "breadcrumb_rules",
                    "is_active"
                )?);
                let applied: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM schema_migrations
                     WHERE version = '079_breadcrumb_rule_activation'",
                    [],
                    |row| row.get(0),
                )?;
                assert_eq!(applied, 1);
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn browser_attach_migration_preserves_legacy_snapshots() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "PRAGMA foreign_keys = ON;
             CREATE TABLE data_sources (
                id INTEGER PRIMARY KEY
             );
             INSERT INTO data_sources (id) VALUES (1);
             CREATE TABLE data_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
                collected_at INTEGER NOT NULL,
                observed_at INTEGER,
                collector TEXT NOT NULL CHECK (collector IN (
                    'chrome_attach', 'direct_http', 'memory_extract', 'capture_observation'
                )),
                content_text TEXT NOT NULL,
                structured_data TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL,
                freshness_ttl_seconds INTEGER NOT NULL DEFAULT 0,
                provenance TEXT NOT NULL DEFAULT '{}',
                source_capture_ids TEXT NOT NULL DEFAULT '[]',
                source_timeline_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'partial')),
                created_at INTEGER NOT NULL,
                UNIQUE(source_id, content_hash, collected_at)
             );
             INSERT INTO data_snapshots (
                source_id, collected_at, observed_at, collector, content_text,
                structured_data, content_hash, created_at
             ) VALUES (1, 1000, 1000, 'memory_extract', '历史数据 42', '{}', 'old', 1000);",
        )
        .unwrap();

        conn.execute_batch(include_str!(
            "../../../shared/db-schema/migrations/065_allow_browser_attach_snapshots.sql"
        ))
        .unwrap();

        let preserved: i64 = conn
            .query_row("SELECT COUNT(*) FROM data_snapshots", [], |row| row.get(0))
            .unwrap();
        assert_eq!(preserved, 1);
        conn.execute(
            "INSERT INTO data_snapshots (
                source_id, collected_at, observed_at, collector, content_text,
                structured_data, content_hash, created_at
             ) VALUES (1, 2000, 2000, 'browser_attach', '即时数据 84', '{}', 'new', 2000)",
            [],
        )
        .unwrap();
        let indexed: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM data_snapshots_fts WHERE data_snapshots_fts MATCH '即时数据'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(indexed, 1);
    }

    #[test]
    fn default_diary_cron_expressions_include_seconds() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                let mut stmt = conn.prepare(
                    "SELECT cron_expression
                     FROM scheduled_tasks
                     WHERE template_id IN ('daily_journal', 'weekly_report', 'monthly_summary')",
                )?;
                let rows = stmt
                    .query_map([], |row| row.get::<_, String>(0))?
                    .collect::<Result<Vec<_>, _>>()?;

                assert_eq!(rows.len(), 3);
                let protected_count: i64 = conn.query_row(
                    "SELECT count(*) FROM scheduled_tasks
                     WHERE template_id IN ('daily_journal', 'weekly_report', 'monthly_summary')
                       AND is_builtin = 1",
                    [],
                    |row| row.get(0),
                )?;
                assert_eq!(protected_count, 3);
                assert!(StorageManager::has_column(
                    conn,
                    "scheduled_tasks",
                    "notification_channel_ids"
                )?);
                assert!(rows.iter().all(|cron| cron.split_whitespace().count() == 6));
                let weekly_cron: String = conn.query_row(
                    "SELECT cron_expression FROM scheduled_tasks WHERE template_id = 'weekly_report'",
                    [],
                    |row| row.get(0),
                )?;
                assert_eq!(weekly_cron, "0 0 9 * * 2");
                let daily_instruction: String = conn.query_row(
                    "SELECT user_instruction FROM scheduled_tasks WHERE template_id = 'daily_journal'",
                    [],
                    |row| row.get(0),
                )?;
                assert!(!daily_instruction.contains("【明日计划】"));
                assert!(daily_instruction.contains("不要生成明日计划"));
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn capture_runtime_migration_preserves_existing_users_only() {
        fn preference_after_migration(has_capture: bool) -> Option<String> {
            let conn = Connection::open_in_memory().unwrap();
            conn.execute_batch(
                "CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                 );
                 CREATE TABLE user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'learned',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    updated_at INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 1
                 );
                 CREATE TABLE captures (id INTEGER PRIMARY KEY);",
            )
            .unwrap();
            if has_capture {
                conn.execute("INSERT INTO captures (id) VALUES (1)", [])
                    .unwrap();
            }

            conn.execute_batch(include_str!(
                "migrations/048_preserve_existing_capture_runtime.sql"
            ))
            .unwrap();
            conn.query_row(
                "SELECT value FROM user_preferences WHERE key = 'runtime.capture_enabled'",
                [],
                |row| row.get(0),
            )
            .ok()
        }

        assert_eq!(preference_after_migration(true).as_deref(), Some("true"));
        assert_eq!(preference_after_migration(false), None);
    }

    #[test]
    fn normalization_migration_clears_stuck_due_time() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
             );
             CREATE TABLE scheduled_tasks (
                id INTEGER PRIMARY KEY,
                cron_expression TEXT NOT NULL,
                next_run_at INTEGER,
                updated_at INTEGER NOT NULL
             );
             INSERT INTO scheduled_tasks (id, cron_expression, next_run_at, updated_at)
             VALUES (1, '0 9 * * *', 0, 0);",
        )
        .unwrap();

        conn.execute_batch(include_str!(
            "migrations/043_normalize_scheduled_task_cron.sql"
        ))
        .unwrap();

        let (cron, next_run): (String, Option<i64>) = conn
            .query_row(
                "SELECT cron_expression, next_run_at FROM scheduled_tasks WHERE id = 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(cron, "0 0 9 * * *");
        assert!(next_run.is_none());
    }

    #[test]
    fn weekday_semantics_migration_moves_weekly_report_to_monday() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
             );
             CREATE TABLE scheduled_tasks (
                id INTEGER PRIMARY KEY,
                template_id TEXT,
                cron_expression TEXT NOT NULL,
                next_run_at INTEGER,
                updated_at INTEGER NOT NULL
             );
             INSERT INTO scheduled_tasks
                (id, template_id, cron_expression, next_run_at, updated_at)
             VALUES (1, 'weekly_report', '0 0 9 * * 1', 0, 0);",
        )
        .unwrap();

        conn.execute_batch(include_str!("migrations/044_correct_weekday_semantics.sql"))
            .unwrap();

        let (cron, next_run): (String, Option<i64>) = conn
            .query_row(
                "SELECT cron_expression, next_run_at FROM scheduled_tasks WHERE id = 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(cron, "0 0 9 * * 2");
        assert!(next_run.is_none());
    }

    #[test]
    fn document_metadata_migration_backfills_and_requeues_only_substantive_docs() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
             );
             CREATE TABLE timelines (
                id INTEGER PRIMARY KEY,
                category TEXT,
                activity_type TEXT,
                content_origin TEXT,
                evidence_strength TEXT,
                updated_at TEXT,
                updated_at_ms INTEGER
             );
             CREATE TABLE captures (
                id INTEGER PRIMARY KEY,
                timeline_id INTEGER,
                url TEXT,
                ax_text TEXT,
                ocr_text TEXT
             );
             CREATE TABLE bake_retry_state (
                timeline_id INTEGER PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                last_error TEXT,
                last_failed_at_ms INTEGER NOT NULL
             );
             INSERT INTO timelines VALUES
                (1, '其他', NULL, NULL, NULL, NULL, 1),
                (2, '其他', NULL, NULL, NULL, NULL, 1);
             INSERT INTO captures VALUES
                (10, 1, 'https://docs.corp.kuaishou.com/k/home/space/doc-id', replace(hex(zeroblob(300)), '00', '文'), NULL),
                (20, 2, 'https://docs.corp.kuaishou.com/k/home/space/short-id', '只有标题', NULL);
             INSERT INTO bake_retry_state VALUES
                (1, 3, '旧错误', 1),
                (2, 3, '旧错误', 1);",
        )
        .unwrap();

        conn.execute_batch(include_str!(
            "migrations/053_backfill_document_timeline_metadata.sql"
        ))
        .unwrap();

        let metadata: (String, String, String, String, i64) = conn
            .query_row(
                "SELECT category, activity_type, content_origin, evidence_strength, updated_at_ms
                 FROM timelines WHERE id = 1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(
            (&metadata.0, &metadata.1, &metadata.2, &metadata.3),
            (
                &"文档".to_string(),
                &"reading".to_string(),
                &"document_reference".to_string(),
                &"medium".to_string(),
            )
        );
        assert!(metadata.4 > 1);
        let short_activity: Option<String> = conn
            .query_row(
                "SELECT activity_type FROM timelines WHERE id = 2",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(short_activity.is_none());
        let retry_ids: Vec<i64> = conn
            .prepare("SELECT timeline_id FROM bake_retry_state ORDER BY timeline_id")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(retry_ids, vec![2]);
    }

    #[test]
    fn historical_bake_timeout_migration_requeues_and_rolls_back_watermark() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE timelines (
                id INTEGER PRIMARY KEY,
                updated_at_ms INTEGER
             );
             CREATE TABLE captures (
                id INTEGER PRIMARY KEY,
                timeline_id INTEGER,
                ts INTEGER
             );
             CREATE TABLE bake_retry_state (
                timeline_id INTEGER PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                last_error TEXT,
                last_failed_at_ms INTEGER NOT NULL
             );
             CREATE TABLE bake_watermarks (
                pipeline_name TEXT PRIMARY KEY,
                last_processed_ts INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
             );
             INSERT INTO timelines VALUES (1, 500), (2, 700);
             INSERT INTO captures VALUES (10, 1, 550), (20, 2, 750);
             INSERT INTO bake_retry_state VALUES
                (1, 1, 'upstream error (504 Gateway Timeout): timeout', 600),
                (2, 3, 'internal error: deterministic payload error', 800);
             INSERT INTO bake_watermarks VALUES ('unified', 1000, 1000);",
        )
        .unwrap();

        conn.execute_batch(include_str!(
            "migrations/061_requeue_historical_bake_timeouts.sql"
        ))
        .unwrap();

        let watermark: i64 = conn
            .query_row(
                "SELECT last_processed_ts FROM bake_watermarks WHERE pipeline_name = 'unified'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(watermark, 549);
        let retry_ids: Vec<i64> = conn
            .prepare("SELECT timeline_id FROM bake_retry_state ORDER BY timeline_id")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(retry_ids, vec![2]);
    }

    #[test]
    fn document_identity_migration_preserves_legacy_duplicates_and_claims_one_identity() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "PRAGMA foreign_keys = ON;
             CREATE TABLE bake_documents (
                 id INTEGER PRIMARY KEY,
                 source_url TEXT,
                 deleted_at INTEGER
             );
             CREATE TABLE bake_knowledge (
                 id INTEGER PRIMARY KEY,
                 timeline_id INTEGER NOT NULL,
                 created_at_ms INTEGER
             );
             CREATE TABLE bake_sops (
                 id INTEGER PRIMARY KEY,
                 timeline_id INTEGER NOT NULL,
                 created_at_ms INTEGER
             );
             INSERT INTO bake_documents (id, source_url, deleted_at) VALUES
                 (10, 'https://Docs.Corp.Example/d/home/ABC123?section=one', NULL),
                 (11, 'http://docs.corp.example/d/home/abc123#section=two', NULL),
                 (12, 'https://docs.corp.example/d/home/deleted', 123);",
        )
        .unwrap();

        StorageManager::add_column_if_missing(&conn, "bake_documents", "document_identity", "TEXT")
            .unwrap();
        conn.execute_batch(include_str!(
            "migrations/062_document_artifact_identity.sql"
        ))
        .unwrap();

        let active_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM bake_documents WHERE deleted_at IS NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(active_count, 2);
        let claimed: Vec<(i64, Option<String>)> = conn
            .prepare(
                "SELECT id, document_identity
                 FROM bake_documents
                 WHERE deleted_at IS NULL
                 ORDER BY id",
            )
            .unwrap()
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(
            claimed,
            vec![
                (10, Some("docs.corp.example/d/home/abc123".to_string())),
                (11, None),
            ]
        );
        assert!(conn
            .execute(
                "UPDATE bake_documents
                 SET document_identity = 'docs.corp.example/d/home/abc123'
                 WHERE id = 11",
                [],
            )
            .is_err());
    }

    #[test]
    fn durable_artifact_vector_migration_queues_soft_and_hard_deletes() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE bake_documents (
                 id INTEGER PRIMARY KEY,
                 deleted_at INTEGER
             );
             INSERT INTO bake_documents VALUES (80, NULL), (81, NULL);",
        )
        .unwrap();
        conn.execute_batch(include_str!("migrations/063_durable_artifact_vectors.sql"))
            .unwrap();
        conn.execute_batch(
            "INSERT INTO artifact_vector_index (
                 document_id, qdrant_point_id, doc_key, content_hash,
                 chunk_index, chunk_text, model_name, indexed_at
             )
             VALUES
                 (80, 'soft-point', 'document:80', 'v1', 0, 'soft', 'test', 1),
                 (81, 'hard-point', 'document:81', 'v1', 0, 'hard', 'test', 1);
             UPDATE bake_documents SET deleted_at = 2 WHERE id = 80;
             PRAGMA foreign_keys = OFF;
             DELETE FROM bake_documents WHERE id = 81;",
        )
        .unwrap();

        let ledger_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM artifact_vector_index", [], |row| {
                row.get(0)
            })
            .unwrap();
        let queued: Vec<String> = conn
            .prepare(
                "SELECT qdrant_point_id
                 FROM vector_deletion_queue
                 ORDER BY qdrant_point_id",
            )
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(ledger_count, 0);
        assert_eq!(queued, vec!["hard-point", "soft-point"]);
    }
}
