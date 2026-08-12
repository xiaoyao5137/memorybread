//! Memory package export/import.
//!
//! These snapshots intentionally exclude raw capture payloads (OCR text,
//! keyboard input, screenshot paths, audio text). Timelines still require a
//! `captures.id` foreign key, so export includes redacted capture references
//! that can be restored as sensitive placeholder rows.

use std::{collections::BTreeMap, path::Path};

use rusqlite::{
    params, params_from_iter,
    types::{Value as SqlValue, ValueRef},
    Connection, OptionalExtension,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::storage::{db::current_ts_ms, error::StorageError, StorageManager};

pub const ASSET_SNAPSHOT_FORMAT_VERSION: i32 = 1;
pub const ASSET_SNAPSHOT_SCHEMA_VERSION: i32 = 4;

const EXCLUDED_RAW_CAPTURE_COLUMNS: &[&str] = &[
    "ax_text",
    "ocr_text",
    "input_text",
    "audio_text",
    "screenshot_path",
    "url",
    "webpage_title",
    "screenshot_source",
];

const EXCLUDED_TABLES: &[&str] = &[
    "captures",
    "captures_fts",
    "captures_fts_config",
    "captures_fts_data",
    "captures_fts_docsize",
    "captures_fts_idx",
    "user_preferences",
    "app_filters",
    "app_blacklist",
    "privacy_filters",
    "style_samples",
    "action_logs",
    "rag_sessions",
    "scheduled_tasks",
    "task_executions",
    "bake_runs",
    "bake_watermarks",
    "bake_retry_state",
    "bake_articles",
    "bake_articles_fts",
    "bake_designs",
    "bake_designs_fts",
    "bake_documents_fts",
    "bake_document_sections_fts",
    "bake_knowledge_fts",
    "bake_sops_fts",
    "bake_templates",
    "creation_history",
    "creation_evidence_assets",
    "designs_fts",
    "episodic_memories_fts",
    "knowledge_fts",
    "timelines_fts",
    "timelines_fts_config",
    "timelines_fts_data",
    "timelines_fts_docsize",
    "timelines_fts_idx",
    "vector_index",
    "system_metrics",
    "llm_usage_logs",
    "model_events",
    "data_cleanup_log",
    "data_extraction_state",
];

#[derive(Debug, Clone, Copy)]
struct AssetTableSpec {
    name: &'static str,
    identity_columns: &'static [&'static str],
}

const ASSET_TABLES: &[AssetTableSpec] = &[
    AssetTableSpec {
        name: "breadcrumb_definitions",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "breadcrumb_rules",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "breadcrumb_inventory",
        identity_columns: &["breadcrumb_id"],
    },
    AssetTableSpec {
        name: "breadcrumb_awards",
        identity_columns: &["rule_id", "period_key"],
    },
    AssetTableSpec {
        name: "breadcrumb_equipment",
        identity_columns: &["surface"],
    },
    AssetTableSpec {
        name: "timelines",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "bake_knowledge",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "bake_documents",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "bake_document_sections",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "bake_sops",
        identity_columns: &["id"],
    },
    AssetTableSpec {
        name: "creation_skills",
        identity_columns: &["client_skill_key"],
    },
    AssetTableSpec {
        name: "data_sources",
        identity_columns: &["canonical_key"],
    },
    AssetTableSpec {
        name: "data_snapshots",
        identity_columns: &["source_id", "period_key"],
    },
    AssetTableSpec {
        name: "data_source_links",
        identity_columns: &["source_ref_key"],
    },
];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssetSnapshot {
    pub manifest: AssetSnapshotManifest,
    pub capture_refs: Vec<JsonRow>,
    pub tables: Vec<TableSnapshot>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssetSnapshotManifest {
    pub app: String,
    pub format_version: i32,
    pub schema_version: i32,
    pub exported_at_ms: i64,
    pub source_db_path: String,
    pub excluded_tables: Vec<String>,
    pub excluded_capture_columns: Vec<String>,
    pub table_summaries: Vec<TableSnapshotSummary>,
    pub payload_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableSnapshotSummary {
    pub name: String,
    pub row_count: usize,
    pub identity_columns: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableSnapshot {
    pub name: String,
    pub identity_columns: Vec<String>,
    pub columns: Vec<String>,
    pub rows: Vec<JsonRow>,
}

pub type JsonRow = BTreeMap<String, Value>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssetSnapshotExportResult {
    pub path: String,
    pub file_sha256: String,
    pub file_size_bytes: u64,
    pub manifest: AssetSnapshotManifest,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AssetSnapshotImportReport {
    pub file_sha256: String,
    pub payload_sha256: String,
    pub dry_run: bool,
    pub capture_refs: TableImportReport,
    pub tables: Vec<TableImportReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct TableImportReport {
    pub name: String,
    pub incoming: usize,
    pub inserted: usize,
    pub updated: usize,
    pub skipped: usize,
}

impl StorageManager {
    pub fn export_asset_snapshot_to_path(
        &self,
        path: &Path,
    ) -> Result<AssetSnapshotExportResult, StorageError> {
        let mut snapshot = self.export_asset_snapshot()?;
        snapshot.manifest.payload_sha256 = snapshot_payload_sha256(&snapshot)?;
        let bytes = serde_json::to_vec_pretty(&snapshot)?;

        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, &bytes)?;

        Ok(AssetSnapshotExportResult {
            path: path.display().to_string(),
            file_sha256: sha256_hex(&bytes),
            file_size_bytes: bytes.len() as u64,
            manifest: snapshot.manifest,
        })
    }

    pub fn import_asset_snapshot_from_path(
        &self,
        path: &Path,
        dry_run: bool,
    ) -> Result<AssetSnapshotImportReport, StorageError> {
        let bytes = std::fs::read(path)?;
        self.import_asset_snapshot_from_bytes(&bytes, dry_run)
    }

    pub fn import_asset_snapshot_from_bytes(
        &self,
        bytes: &[u8],
        dry_run: bool,
    ) -> Result<AssetSnapshotImportReport, StorageError> {
        let snapshot: AssetSnapshot = serde_json::from_slice(&bytes)?;
        let actual_payload_sha256 = snapshot_payload_sha256(&snapshot)?;
        if snapshot.manifest.payload_sha256 != actual_payload_sha256 {
            return Err(StorageError::MigrationFailed {
                version: "asset_snapshot_import",
                reason: "资产快照 payload_sha256 校验失败".to_string(),
            });
        }

        let mut report = self.import_asset_snapshot(&snapshot, dry_run)?;
        report.file_sha256 = sha256_hex(&bytes);
        report.payload_sha256 = actual_payload_sha256;
        Ok(report)
    }

    pub fn export_asset_snapshot(&self) -> Result<AssetSnapshot, StorageError> {
        self.with_conn(|conn| {
            let capture_refs = export_capture_refs(conn)?;
            let mut tables = Vec::new();
            let mut table_summaries = Vec::new();

            for spec in ASSET_TABLES {
                if !table_exists(conn, spec.name)? {
                    continue;
                }
                let table = export_table(conn, spec)?;
                table_summaries.push(TableSnapshotSummary {
                    name: table.name.clone(),
                    row_count: table.rows.len(),
                    identity_columns: table.identity_columns.clone(),
                });
                tables.push(table);
            }

            let mut snapshot = AssetSnapshot {
                manifest: AssetSnapshotManifest {
                    app: "MemoryBread".to_string(),
                    format_version: ASSET_SNAPSHOT_FORMAT_VERSION,
                    schema_version: ASSET_SNAPSHOT_SCHEMA_VERSION,
                    exported_at_ms: current_ts_ms(),
                    source_db_path: conn
                        .path()
                        .map(|path| path.to_string())
                        .unwrap_or(":memory:".into()),
                    excluded_tables: EXCLUDED_TABLES
                        .iter()
                        .map(|name| name.to_string())
                        .collect(),
                    excluded_capture_columns: EXCLUDED_RAW_CAPTURE_COLUMNS
                        .iter()
                        .map(|name| name.to_string())
                        .collect(),
                    table_summaries,
                    payload_sha256: String::new(),
                },
                capture_refs,
                tables,
            };
            snapshot.manifest.payload_sha256 = snapshot_payload_sha256(&snapshot)?;
            Ok(snapshot)
        })
    }

    pub fn import_asset_snapshot(
        &self,
        snapshot: &AssetSnapshot,
        dry_run: bool,
    ) -> Result<AssetSnapshotImportReport, StorageError> {
        if snapshot.manifest.format_version != ASSET_SNAPSHOT_FORMAT_VERSION {
            return Err(StorageError::MigrationFailed {
                version: "asset_snapshot_import",
                reason: format!(
                    "不支持的资产快照格式版本 {}",
                    snapshot.manifest.format_version
                ),
            });
        }

        let actual_payload_sha256 = snapshot_payload_sha256(snapshot)?;
        if !snapshot.manifest.payload_sha256.is_empty()
            && snapshot.manifest.payload_sha256 != actual_payload_sha256
        {
            return Err(StorageError::MigrationFailed {
                version: "asset_snapshot_import",
                reason: "资产快照 payload_sha256 校验失败".to_string(),
            });
        }

        self.with_conn(|conn| {
            let mut report = AssetSnapshotImportReport {
                payload_sha256: actual_payload_sha256,
                dry_run,
                ..Default::default()
            };

            if dry_run {
                report.capture_refs = dry_run_report("capture_refs", snapshot.capture_refs.len());
                report.tables = snapshot
                    .tables
                    .iter()
                    .map(|table| dry_run_report(&table.name, table.rows.len()))
                    .collect();
                return Ok(report);
            }

            conn.execute_batch("PRAGMA foreign_keys = ON;")?;
            let tx = conn.unchecked_transaction()?;
            report.capture_refs = import_capture_refs(&tx, &snapshot.capture_refs)?;

            for table in &snapshot.tables {
                if is_data_asset_table(&table.name) {
                    continue;
                }
                if !is_allowed_asset_table(&table.name) {
                    report.tables.push(TableImportReport {
                        name: table.name.clone(),
                        incoming: table.rows.len(),
                        skipped: table.rows.len(),
                        ..Default::default()
                    });
                    continue;
                }
                if !table_exists(&tx, &table.name)? {
                    report.tables.push(TableImportReport {
                        name: table.name.clone(),
                        incoming: table.rows.len(),
                        skipped: table.rows.len(),
                        ..Default::default()
                    });
                    continue;
                }

                let table_report = match table.name.as_str() {
                    "user_preferences" => upsert_user_preferences(&tx, table)?,
                    "app_filters" => upsert_app_filters(&tx, table)?,
                    "app_blacklist" => upsert_app_blacklist(&tx, table)?,
                    "privacy_filters" => upsert_privacy_filters(&tx, table)?,
                    "creation_skills" => upsert_creation_skills(&tx, table)?,
                    _ => insert_or_ignore_table(&tx, table)?,
                };
                report.tables.push(table_report);
            }

            let mut source_id_map = BTreeMap::new();
            for table_name in ["data_sources", "data_snapshots", "data_source_links"] {
                let Some(table) = snapshot
                    .tables
                    .iter()
                    .find(|table| table.name == table_name)
                else {
                    continue;
                };
                if !table_exists(&tx, table_name)? {
                    report.tables.push(TableImportReport {
                        name: table.name.clone(),
                        incoming: table.rows.len(),
                        skipped: table.rows.len(),
                        ..Default::default()
                    });
                    continue;
                }

                let table_report = match table_name {
                    "data_sources" => {
                        let (table_report, imported_source_ids) = upsert_data_sources(&tx, table)?;
                        source_id_map = imported_source_ids;
                        table_report
                    }
                    "data_snapshots" => upsert_data_snapshots(&tx, table, &source_id_map)?,
                    "data_source_links" => upsert_data_source_links(&tx, table, &source_id_map)?,
                    _ => unreachable!(),
                };
                report.tables.push(table_report);
            }

            tx.commit()?;
            Ok(report)
        })
    }
}

fn dry_run_report(name: &str, incoming: usize) -> TableImportReport {
    TableImportReport {
        name: name.to_string(),
        incoming,
        skipped: incoming,
        ..Default::default()
    }
}

fn snapshot_payload_sha256(snapshot: &AssetSnapshot) -> Result<String, StorageError> {
    #[derive(Serialize)]
    struct PayloadForHash<'a> {
        format_version: i32,
        schema_version: i32,
        capture_refs: &'a [JsonRow],
        tables: &'a [TableSnapshot],
    }

    let payload = PayloadForHash {
        format_version: snapshot.manifest.format_version,
        schema_version: snapshot.manifest.schema_version,
        capture_refs: &snapshot.capture_refs,
        tables: &snapshot.tables,
    };
    let bytes = serde_json::to_vec(&payload)?;
    Ok(sha256_hex(&bytes))
}

fn export_capture_refs(conn: &Connection) -> Result<Vec<JsonRow>, StorageError> {
    if !table_exists(conn, "captures")? || !table_exists(conn, "timelines")? {
        return Ok(Vec::new());
    }

    let mut stmt = conn.prepare(
        "WITH referenced_captures(id) AS (
            SELECT capture_id FROM timelines WHERE capture_id IS NOT NULL
            UNION
            SELECT capture_id FROM data_source_links WHERE capture_id IS NOT NULL
         )
         SELECT
            refs.id AS id,
            COALESCE(MAX(c.ts), MAX(t.created_at_ms), MAX(t.start_time), MAX(t.observed_at), 0) AS ts,
            MAX(c.app_name) AS app_name,
            MAX(c.app_bundle_id) AS app_bundle_id,
            MAX(c.win_title) AS win_title,
            COALESCE(MAX(c.event_type), 'snapshot_ref') AS event_type
         FROM referenced_captures refs
         LEFT JOIN captures c ON c.id = refs.id
         LEFT JOIN timelines t ON t.capture_id = refs.id
         GROUP BY refs.id
         ORDER BY refs.id ASC",
    )?;
    let column_names = stmt
        .column_names()
        .iter()
        .map(|name| name.to_string())
        .collect::<Vec<_>>();
    let rows = stmt.query_map([], |row| row_to_json(&column_names, row))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(StorageError::Sqlite)
}

fn export_table(conn: &Connection, spec: &AssetTableSpec) -> Result<TableSnapshot, StorageError> {
    let columns = table_columns(conn, spec.name)?;
    let order = if columns.iter().any(|column| column == "id") {
        " ORDER BY id ASC"
    } else {
        ""
    };
    let sql = format!("SELECT * FROM {}{}", spec.name, order);
    let mut stmt = conn.prepare(&sql)?;
    let column_names = stmt
        .column_names()
        .iter()
        .map(|name| name.to_string())
        .collect::<Vec<_>>();
    let rows = stmt.query_map([], |row| row_to_json(&column_names, row))?;
    Ok(TableSnapshot {
        name: spec.name.to_string(),
        identity_columns: spec
            .identity_columns
            .iter()
            .map(|name| name.to_string())
            .collect(),
        columns,
        rows: rows
            .collect::<Result<Vec<_>, _>>()
            .map_err(StorageError::Sqlite)?,
    })
}

fn row_to_json(column_names: &[String], row: &rusqlite::Row<'_>) -> rusqlite::Result<JsonRow> {
    let mut out = JsonRow::new();
    for (idx, column) in column_names.iter().enumerate() {
        out.insert(column.clone(), sql_value_ref_to_json(row.get_ref(idx)?));
    }
    Ok(out)
}

fn sql_value_ref_to_json(value: ValueRef<'_>) -> Value {
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => Value::from(value),
        ValueRef::Real(value) => Value::from(value),
        ValueRef::Text(value) => Value::String(String::from_utf8_lossy(value).to_string()),
        ValueRef::Blob(value) => {
            let mut object = serde_json::Map::new();
            object.insert(
                "__mb_blob_hex".to_string(),
                Value::String(hex_encode(value)),
            );
            Value::Object(object)
        }
    }
}

fn json_to_sql_value(value: &Value) -> SqlValue {
    match value {
        Value::Null => SqlValue::Null,
        Value::Bool(value) => SqlValue::Integer(i64::from(*value)),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                SqlValue::Integer(value)
            } else if let Some(value) = value.as_u64() {
                SqlValue::Integer(value.min(i64::MAX as u64) as i64)
            } else {
                SqlValue::Real(value.as_f64().unwrap_or_default())
            }
        }
        Value::String(value) => SqlValue::Text(value.clone()),
        Value::Array(_) | Value::Object(_) => {
            if let Some(hex) = value.get("__mb_blob_hex").and_then(Value::as_str) {
                SqlValue::Blob(hex_decode(hex).unwrap_or_default())
            } else {
                SqlValue::Text(value.to_string())
            }
        }
    }
}

fn import_capture_refs(
    conn: &Connection,
    refs: &[JsonRow],
) -> Result<TableImportReport, StorageError> {
    if refs.is_empty() {
        return Ok(TableImportReport {
            name: "capture_refs".to_string(),
            ..Default::default()
        });
    }

    let mut report = TableImportReport {
        name: "capture_refs".to_string(),
        incoming: refs.len(),
        ..Default::default()
    };

    let mut stmt = conn.prepare(
        "INSERT OR IGNORE INTO captures (
            id, ts, app_name, app_bundle_id, win_title, event_type,
            is_sensitive, pii_scrubbed
         )
         VALUES (?1, ?2, ?3, ?4, ?5, 'snapshot_ref', 1, 1)",
    )?;

    for row in refs {
        let id = json_i64(row, "id").unwrap_or_default();
        let ts = json_i64(row, "ts").unwrap_or_else(current_ts_ms);
        let app_name = json_string(row, "app_name");
        let app_bundle_id = json_string(row, "app_bundle_id");
        let win_title = json_string(row, "win_title");
        let affected = stmt.execute(params![id, ts, app_name, app_bundle_id, win_title])?;
        if affected == 0 {
            report.skipped += 1;
        } else {
            report.inserted += affected;
        }
    }

    Ok(report)
}

fn insert_or_ignore_table(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<TableImportReport, StorageError> {
    let existing_columns = table_columns(conn, &table.name)?;
    let insert_columns = table
        .columns
        .iter()
        .filter(|column| existing_columns.contains(column))
        .cloned()
        .collect::<Vec<_>>();
    if insert_columns.is_empty() {
        return Ok(TableImportReport {
            name: table.name.clone(),
            incoming: table.rows.len(),
            skipped: table.rows.len(),
            ..Default::default()
        });
    }

    let sql = insert_sql("INSERT OR IGNORE", &table.name, &insert_columns, None);
    let mut stmt = conn.prepare(&sql)?;
    let mut report = TableImportReport {
        name: table.name.clone(),
        incoming: table.rows.len(),
        ..Default::default()
    };

    for row in &table.rows {
        let values = values_for_columns(row, &insert_columns);
        let affected = stmt.execute(params_from_iter(values.iter()))?;
        if affected == 0 {
            report.skipped += 1;
        } else {
            report.inserted += affected;
        }
    }
    Ok(report)
}

fn upsert_user_preferences(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique(
        conn,
        table,
        "key",
        &[
            "key",
            "value",
            "source",
            "confidence",
            "updated_at",
            "sample_count",
        ],
        &[
            "value",
            "source",
            "confidence",
            "updated_at",
            "sample_count",
        ],
    )
}

fn upsert_app_filters(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique(
        conn,
        table,
        "app_name",
        &["app_name", "filter_type", "reason", "created_at"],
        &["filter_type", "reason"],
    )
}

fn upsert_app_blacklist(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique(
        conn,
        table,
        "bundle_id",
        &[
            "bundle_id",
            "app_name",
            "enabled",
            "reason",
            "created_at",
            "updated_at",
        ],
        &["app_name", "enabled", "reason", "updated_at"],
    )
}

fn upsert_privacy_filters(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique(
        conn,
        table,
        "filter_type",
        &[
            "filter_type",
            "filter_name",
            "enabled",
            "config_json",
            "updated_at",
        ],
        &["filter_name", "enabled", "config_json", "updated_at"],
    )
}

fn upsert_creation_skills(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique(
        conn,
        table,
        "client_skill_key",
        &[
            "client_skill_key",
            "cloud_skill_id",
            "source_kind",
            "source_id",
            "title",
            "summary",
            "category_id",
            "common_titles",
            "title_style",
            "text_style",
            "diagram_style",
            "writing_guidelines",
            "distinctive_sections",
            "section_headings",
            "field_examples",
            "example_document",
            "package_files",
            "status",
            "installed",
            "published",
            "created_at",
            "updated_at",
            "deleted_at",
        ],
        &[
            "cloud_skill_id",
            "source_kind",
            "source_id",
            "title",
            "summary",
            "category_id",
            "common_titles",
            "title_style",
            "text_style",
            "diagram_style",
            "writing_guidelines",
            "distinctive_sections",
            "section_headings",
            "field_examples",
            "example_document",
            "package_files",
            "status",
            "installed",
            "published",
            "updated_at",
            "deleted_at",
        ],
    )
}

fn upsert_data_sources(
    conn: &Connection,
    table: &TableSnapshot,
) -> Result<(TableImportReport, BTreeMap<i64, i64>), StorageError> {
    let report = upsert_by_unique_where(
        conn,
        table,
        "canonical_key",
        &[
            "canonical_key",
            "title",
            "source_kind",
            "source_url",
            "access_mode",
            "refresh_policy",
            "realtime_level",
            "source_app_name",
            "source_window_title",
            "tags",
            "first_seen_at",
            "last_seen_at",
            "last_collected_at",
            "last_success_at",
            "last_error_code",
            "status",
            "created_at",
            "updated_at",
            "deleted_at",
        ],
        &[
            "title",
            "source_kind",
            "source_url",
            "access_mode",
            "refresh_policy",
            "realtime_level",
            "source_app_name",
            "source_window_title",
            "tags",
            "first_seen_at",
            "last_seen_at",
            "last_collected_at",
            "last_success_at",
            "last_error_code",
            "status",
            "updated_at",
            "deleted_at",
        ],
        Some("excluded.updated_at >= data_sources.updated_at"),
    )?;

    let mut source_id_map = BTreeMap::new();
    let mut stmt = conn.prepare("SELECT id FROM data_sources WHERE canonical_key = ?1")?;
    for row in &table.rows {
        let (Some(source_id), Some(canonical_key)) =
            (json_i64(row, "id"), json_string(row, "canonical_key"))
        else {
            continue;
        };
        if let Some(imported_id) = stmt
            .query_row(params![canonical_key], |row| row.get::<_, i64>(0))
            .optional()?
        {
            source_id_map.insert(source_id, imported_id);
        }
    }

    Ok((report, source_id_map))
}

fn upsert_data_snapshots(
    conn: &Connection,
    table: &TableSnapshot,
    source_id_map: &BTreeMap<i64, i64>,
) -> Result<TableImportReport, StorageError> {
    let (mut remapped, missing_sources) = remap_data_source_ids(table, source_id_map);
    for column in [
        "period_granularity",
        "period_key",
        "period_start_at",
        "period_end_at",
    ] {
        if !remapped.columns.iter().any(|existing| existing == column) {
            remapped.columns.push(column.to_string());
        }
    }
    const WEEK_MILLIS: i64 = 7 * 24 * 60 * 60 * 1000;
    const FIRST_MONDAY_MILLIS: i64 = 4 * 24 * 60 * 60 * 1000;
    for row in &mut remapped.rows {
        if row
            .get("period_key")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
        {
            continue;
        }
        let observed_at = json_i64(row, "observed_at")
            .or_else(|| json_i64(row, "collected_at"))
            .unwrap_or_default();
        let start_at = (observed_at - FIRST_MONDAY_MILLIS).div_euclid(WEEK_MILLIS) * WEEK_MILLIS
            + FIRST_MONDAY_MILLIS;
        row.insert("period_granularity".to_string(), Value::from("week"));
        row.insert(
            "period_key".to_string(),
            Value::from(format!("week:{start_at}")),
        );
        row.insert("period_start_at".to_string(), Value::from(start_at));
        row.insert(
            "period_end_at".to_string(),
            Value::from(start_at + WEEK_MILLIS - 1),
        );
    }
    let mut report = upsert_by_unique_columns_where(
        conn,
        &remapped,
        &["source_id", "period_key"],
        &[
            "source_id",
            "period_granularity",
            "period_key",
            "period_start_at",
            "period_end_at",
            "collected_at",
            "observed_at",
            "collector",
            "content_text",
            "structured_data",
            "content_hash",
            "freshness_ttl_seconds",
            "provenance",
            "source_capture_ids",
            "source_timeline_ids",
            "status",
            "created_at",
        ],
        &[
            "collected_at",
            "observed_at",
            "period_granularity",
            "period_start_at",
            "period_end_at",
            "collector",
            "content_text",
            "structured_data",
            "content_hash",
            "freshness_ttl_seconds",
            "provenance",
            "source_capture_ids",
            "source_timeline_ids",
            "status",
            "created_at",
        ],
        Some(
            "COALESCE(excluded.observed_at, excluded.collected_at) >= \
             COALESCE(data_snapshots.observed_at, data_snapshots.collected_at)",
        ),
    )?;
    report.incoming = table.rows.len();
    report.skipped += missing_sources;
    Ok(report)
}

fn upsert_data_source_links(
    conn: &Connection,
    table: &TableSnapshot,
    source_id_map: &BTreeMap<i64, i64>,
) -> Result<TableImportReport, StorageError> {
    let (remapped, missing_sources) = remap_data_source_ids(table, source_id_map);
    let mut report = upsert_by_unique_where(
        conn,
        &remapped,
        "source_ref_key",
        &[
            "source_id",
            "source_ref_key",
            "capture_id",
            "timeline_id",
            "link_kind",
            "observed_at",
            "created_at",
        ],
        &[
            "source_id",
            "capture_id",
            "timeline_id",
            "link_kind",
            "observed_at",
        ],
        Some("excluded.observed_at >= data_source_links.observed_at"),
    )?;
    report.incoming = table.rows.len();
    report.skipped += missing_sources;
    Ok(report)
}

fn remap_data_source_ids(
    table: &TableSnapshot,
    source_id_map: &BTreeMap<i64, i64>,
) -> (TableSnapshot, usize) {
    let mut rows = Vec::with_capacity(table.rows.len());
    let mut missing_sources = 0;
    for row in &table.rows {
        let Some(source_id) = json_i64(row, "source_id") else {
            missing_sources += 1;
            continue;
        };
        let Some(imported_source_id) = source_id_map.get(&source_id) else {
            missing_sources += 1;
            continue;
        };
        let mut remapped = row.clone();
        remapped.insert("source_id".to_string(), Value::from(*imported_source_id));
        rows.push(remapped);
    }
    (
        TableSnapshot {
            name: table.name.clone(),
            identity_columns: table.identity_columns.clone(),
            columns: table.columns.clone(),
            rows,
        },
        missing_sources,
    )
}

fn upsert_by_unique(
    conn: &Connection,
    table: &TableSnapshot,
    unique_column: &str,
    wanted_columns: &[&str],
    update_columns: &[&str],
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique_where(
        conn,
        table,
        unique_column,
        wanted_columns,
        update_columns,
        None,
    )
}

fn upsert_by_unique_where(
    conn: &Connection,
    table: &TableSnapshot,
    unique_column: &str,
    wanted_columns: &[&str],
    update_columns: &[&str],
    update_condition: Option<&str>,
) -> Result<TableImportReport, StorageError> {
    upsert_by_unique_columns_where(
        conn,
        table,
        &[unique_column],
        wanted_columns,
        update_columns,
        update_condition,
    )
}

fn upsert_by_unique_columns_where(
    conn: &Connection,
    table: &TableSnapshot,
    unique_columns: &[&str],
    wanted_columns: &[&str],
    update_columns: &[&str],
    update_condition: Option<&str>,
) -> Result<TableImportReport, StorageError> {
    let existing_columns = table_columns(conn, &table.name)?;
    let insert_columns = wanted_columns
        .iter()
        .filter(|column| {
            existing_columns.iter().any(|existing| existing == **column)
                && table.columns.iter().any(|incoming| incoming == **column)
        })
        .map(|column| column.to_string())
        .collect::<Vec<_>>();

    if unique_columns
        .iter()
        .any(|unique| !insert_columns.iter().any(|column| column == unique))
    {
        return Ok(TableImportReport {
            name: table.name.clone(),
            incoming: table.rows.len(),
            skipped: table.rows.len(),
            ..Default::default()
        });
    }

    let update_clause = update_columns
        .iter()
        .filter(|column| insert_columns.iter().any(|existing| existing == **column))
        .map(|column| format!("{column} = excluded.{column}"))
        .collect::<Vec<_>>()
        .join(", ");
    let conflict = if update_clause.is_empty() {
        format!("ON CONFLICT({}) DO NOTHING", unique_columns.join(", "))
    } else {
        let condition = update_condition
            .map(|condition| format!(" WHERE {condition}"))
            .unwrap_or_default();
        format!(
            "ON CONFLICT({}) DO UPDATE SET {update_clause}{condition}",
            unique_columns.join(", ")
        )
    };
    let sql = insert_sql("INSERT", &table.name, &insert_columns, Some(&conflict));
    let exists_sql = format!(
        "SELECT 1 FROM {} WHERE {}",
        table.name,
        unique_columns
            .iter()
            .enumerate()
            .map(|(index, column)| format!("{column} = ?{}", index + 1))
            .collect::<Vec<_>>()
            .join(" AND ")
    );
    let mut stmt = conn.prepare(&sql)?;
    let mut report = TableImportReport {
        name: table.name.clone(),
        incoming: table.rows.len(),
        ..Default::default()
    };

    for row in &table.rows {
        let unique_values = unique_columns
            .iter()
            .filter_map(|column| row.get(*column))
            .map(json_to_sql_value)
            .collect::<Vec<_>>();
        if unique_values.len() != unique_columns.len() {
            report.skipped += 1;
            continue;
        }
        let existed = conn
            .query_row(&exists_sql, params_from_iter(unique_values.iter()), |_| {
                Ok(())
            })
            .optional()?
            .is_some();
        let values = values_for_columns(row, &insert_columns);
        let affected = stmt.execute(params_from_iter(values.iter()))?;
        if affected == 0 {
            report.skipped += 1;
        } else if existed {
            report.updated += affected;
        } else {
            report.inserted += affected;
        }
    }
    Ok(report)
}

fn insert_sql(
    verb: &str,
    table: &str,
    columns: &[String],
    conflict_clause: Option<&str>,
) -> String {
    let column_sql = columns.join(", ");
    let placeholders = (1..=columns.len())
        .map(|idx| format!("?{idx}"))
        .collect::<Vec<_>>()
        .join(", ");
    let conflict = conflict_clause
        .map(|clause| format!(" {clause}"))
        .unwrap_or_default();
    format!("{verb} INTO {table} ({column_sql}) VALUES ({placeholders}){conflict}")
}

fn values_for_columns(row: &JsonRow, columns: &[String]) -> Vec<SqlValue> {
    columns
        .iter()
        .map(|column| {
            row.get(column)
                .map(json_to_sql_value)
                .unwrap_or(SqlValue::Null)
        })
        .collect()
}

fn table_exists(conn: &Connection, table: &str) -> Result<bool, StorageError> {
    let exists: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?1",
        params![table],
        |row| row.get(0),
    )?;
    Ok(exists > 0)
}

fn is_allowed_asset_table(table: &str) -> bool {
    ASSET_TABLES.iter().any(|spec| spec.name == table)
}

fn is_data_asset_table(table: &str) -> bool {
    matches!(
        table,
        "data_sources" | "data_snapshots" | "data_source_links"
    )
}

fn table_columns(conn: &Connection, table: &str) -> Result<Vec<String>, StorageError> {
    let sql = format!("PRAGMA table_info({table})");
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(StorageError::Sqlite)
}

fn json_i64(row: &JsonRow, column: &str) -> Option<i64> {
    row.get(column).and_then(Value::as_i64)
}

fn json_string(row: &JsonRow, column: &str) -> Option<String> {
    row.get(column)
        .and_then(Value::as_str)
        .map(|value| value.to_string())
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn hex_decode(input: &str) -> Option<Vec<u8>> {
    if input.len() % 2 != 0 {
        return None;
    }
    let mut bytes = Vec::with_capacity(input.len() / 2);
    for idx in (0..input.len()).step_by(2) {
        let byte = u8::from_str_radix(&input[idx..idx + 2], 16).ok()?;
        bytes.push(byte);
    }
    Some(bytes)
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use tempfile::tempdir;

    use super::*;

    fn seed_assets(storage: &StorageManager) {
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, app_bundle_id, win_title, event_type,
                        ax_text, ocr_text, input_text, audio_text, screenshot_path,
                        is_sensitive, pii_scrubbed
                     )
                     VALUES (
                        42, 1700000000000, 'TestApp', 'com.memorybread.test', 'Sensitive Window',
                        'manual', 'raw ax', 'raw ocr', 'raw input', 'raw audio', 'secret.jpg', 0, 0
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO timelines (
                        id, capture_id, summary, overview, details, entities, category,
                        importance, created_at_ms, updated_at_ms
                     )
                     VALUES (
                        7, 42, '一次重要时间线', '概览', '{\"source\":\"test\"}', '[]',
                        'work', 4, 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO bake_knowledge (
                        id, timeline_id, title, summary, content, entities, importance,
                        source_capture_ids
                     )
                     VALUES (9, 7, '知识标题', '知识摘要', '知识内容', '[]', 4, '[42]')",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO bake_sops (
                        id, timeline_id, title, summary, content, detailed_content,
                        entities, importance, source_capture_ids
                     )
                     VALUES (
                        10, 7, '发布检查操作', '发布前完成检查', '检查版本与变更记录',
                        '1. 检查版本号\n2. 检查变更记录', '[]', 4, '[42]'
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO bake_documents (
                        id, title, doc_type, status, tags, applicable_tasks,
                        source_memory_ids, source_capture_ids, source_episode_ids,
                        linked_knowledge_ids, sections_json, style_phrases,
                        replacement_rules, structured_content, image_assets,
                        creation_mode, review_status, created_at, updated_at
                     )
                     VALUES (
                        11, '文档标题', 'plan', 'enabled', '[]', '[]',
                        '[\"7\"]', '[\"42\"]', '[]', '[\"9\"]', '[]', '[]',
                        '[]', '{}', '[]', 'manual', 'ready', 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO creation_skills (
                        client_skill_key, source_kind, source_id, title, summary,
                        common_titles, title_style, text_style, diagram_style,
                        writing_guidelines, distinctive_sections, package_files,
                        published, created_at, updated_at
                     ) VALUES (
                        'snapshot-skill-1', 'bake_document', '11', '架构文档写作法',
                        '用于验证本地 Skill 快照恢复。', '[\"总体架构设计\"]', '结论先行。',
                        '正式、克制。', '标注系统边界。', '[\"写明技术取舍\"]',
                        '[{\"title\":\"定义先行\",\"description\":\"先建立共同概念。\",\"guidance\":\"先解释对象，再说明边界。\",\"examples\":[\"协作入口连接任务与结果证据。\"]}]',
                        '[{\"path\":\"SKILL.md\",\"media_type\":\"text/markdown\",\"content_base64\":\"IyBTa2lsbA==\",\"size_bytes\":7}]',
                        0, 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, source_url, access_mode,
                        refresh_policy, realtime_level, first_seen_at, last_seen_at,
                        last_collected_at, last_success_at, status, created_at, updated_at
                     ) VALUES (
                        91, 'report:https://bi.example.com/dashboard', '经营看板',
                        'report_url', 'https://bi.example.com/dashboard', 'browser_session',
                        'on_demand', 'live', 1700000000000, 1700000000000,
                        1700000000000, 1700000000000, 'active', 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO data_snapshots (
                        id, source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                     ) VALUES (
                        92, 91, 1700000000000, 1700000000000, 'chrome_attach',
                        '本周订单 1200', '{}', 'snapshot-hash', 900,
                        '{\"local_only\":true}', '[42]', '[7]', 'success', 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO data_source_links (
                        id, source_id, source_ref_key, capture_id, timeline_id,
                        link_kind, observed_at, created_at
                     ) VALUES (
                        93, 91, 'capture:42:active_url:test', 42, 7,
                        'active_url', 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO breadcrumb_definitions (
                       id, breadcrumb_key, name, tagline, description, icon_key,
                       palette_key, rarity, updated_at
                     ) VALUES (
                       'breadcrumb-1', 'focused', '专注面包屑', '专注留下痕迹',
                       '记录稳定的专注投入。', 'focus', 'forest', 'common', 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO breadcrumb_rules (
                       id, rule_key, breadcrumb_id, title, description, period, metric_key,
                       threshold, metric_unit, increment, version, updated_at
                     ) VALUES (
                       'rule-1', 'weekly_focus', 'breadcrumb-1', '本周专注',
                       '本周专注达到一百分钟。', 'weekly', 'focus_minutes', '100',
                       'minute', 1, 1, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO breadcrumb_inventory (
                       breadcrumb_id, quantity, first_earned_at, last_earned_at, updated_at
                     ) VALUES ('breadcrumb-1', 2, 1700000000000, 1700000000000, 1700000000000)",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO breadcrumb_awards (
                       rule_id, period_key, breadcrumb_id, observed_value, increment,
                       rule_version, awarded_at
                     ) VALUES ('rule-1', '2026-W33', 'breadcrumb-1', '120', 1, 1, 1700000000000)",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO breadcrumb_equipment (surface, breadcrumb_id, equipped_at)
                     VALUES ('profile_avatar', 'breadcrumb-1', 1700000000000)",
                    [],
                )?;
                Ok(())
            })
            .unwrap();
        storage
            .upsert_preference("snapshot.test", "user-value", "user", 1.0)
            .unwrap();
    }

    #[test]
    fn asset_snapshot_excludes_raw_capture_payloads_and_imports_idempotently() {
        let source = StorageManager::open_in_memory().unwrap();
        seed_assets(&source);
        let dir = tempdir().unwrap();
        let path = dir.path().join("assets.mbsnapshot.json");

        let export = source.export_asset_snapshot_to_path(&path).unwrap();
        assert!(export.file_size_bytes > 0);

        let snapshot: AssetSnapshot =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert!(snapshot.tables.iter().all(|table| table.name != "captures"));
        assert!(snapshot.tables.iter().all(|table| matches!(
            table.name.as_str(),
            "breadcrumb_definitions"
                | "breadcrumb_rules"
                | "breadcrumb_inventory"
                | "breadcrumb_awards"
                | "breadcrumb_equipment"
                | "timelines"
                | "bake_knowledge"
                | "bake_documents"
                | "bake_document_sections"
                | "bake_sops"
                | "creation_skills"
                | "data_sources"
                | "data_snapshots"
                | "data_source_links"
        )));
        assert_eq!(snapshot.capture_refs.len(), 1);
        assert!(snapshot.capture_refs[0].get("ax_text").is_none());
        assert!(snapshot.capture_refs[0].get("screenshot_path").is_none());

        let target = StorageManager::open_in_memory().unwrap();
        let first = target
            .import_asset_snapshot_from_path(&path, false)
            .unwrap();
        let second = target
            .import_asset_snapshot_from_path(&path, false)
            .unwrap();

        let timeline_count: i64 = target
            .with_conn(|conn| {
                conn.query_row("SELECT COUNT(*) FROM timelines", [], |row| row.get(0))
                    .map_err(StorageError::Sqlite)
            })
            .unwrap();
        let knowledge_count: i64 = target
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT COUNT(*) FROM bake_knowledge WHERE id = 9",
                    [],
                    |row| row.get(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        let restored_sop: (String, String) = target
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT title, detailed_content FROM bake_sops WHERE id = 10",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        let capture_text: Option<String> = target
            .with_conn(|conn| {
                conn.query_row("SELECT ax_text FROM captures WHERE id = 42", [], |row| {
                    row.get(0)
                })
                .optional()
                .map_err(StorageError::Sqlite)
            })
            .unwrap()
            .flatten();
        let restored_skill = target
            .list_creation_skills()
            .unwrap()
            .into_iter()
            .find(|skill| skill.client_skill_key == "snapshot-skill-1")
            .expect("creation skill restored from snapshot");

        assert_eq!(timeline_count, 1);
        assert_eq!(knowledge_count, 1);
        assert_eq!(restored_sop.0, "发布检查操作");
        assert!(restored_sop.1.contains("检查变更记录"));
        assert_eq!(count_table(&target, "data_sources"), 1);
        assert_eq!(count_table(&target, "data_snapshots"), 1);
        assert_eq!(count_table(&target, "data_source_links"), 1);
        assert_eq!(count_table(&target, "breadcrumb_definitions"), 1);
        assert_eq!(count_table(&target, "breadcrumb_rules"), 1);
        assert_eq!(count_table(&target, "breadcrumb_inventory"), 1);
        assert_eq!(count_table(&target, "breadcrumb_awards"), 1);
        assert_eq!(count_table(&target, "breadcrumb_equipment"), 1);
        assert_eq!(restored_skill.title, "架构文档写作法");
        assert_eq!(restored_skill.package_files.len(), 1);
        assert_eq!(restored_skill.package_files[0].path, "SKILL.md");
        assert_eq!(restored_skill.distinctive_sections.len(), 1);
        assert_eq!(restored_skill.distinctive_sections[0].title, "定义先行");
        assert!(capture_text.is_none());
        assert!(first.capture_refs.inserted >= 1);
        assert_eq!(second.capture_refs.skipped, 1);
    }

    #[test]
    fn asset_snapshot_remaps_data_assets_when_local_ids_are_occupied() {
        let source = StorageManager::open_in_memory().unwrap();
        seed_assets(&source);
        let snapshot = source.export_asset_snapshot().unwrap();

        let target = StorageManager::open_in_memory().unwrap();
        target
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode,
                        refresh_policy, realtime_level, first_seen_at, last_seen_at,
                        status, created_at, updated_at
                     ) VALUES (
                        91, 'work:existing-local-source', '本机已有数据', 'work_memory',
                        'memory_only', 'never', 'observed', 1700000001000, 1700000001000,
                        'active', 1700000001000, 1700000001000
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        let first = target.import_asset_snapshot(&snapshot, false).unwrap();
        let imported_source_id: i64 = target
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT id FROM data_sources
                     WHERE canonical_key = 'report:https://bi.example.com/dashboard'",
                    [],
                    |row| row.get(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        let imported_snapshot_content: String = target
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT content_text FROM data_snapshots WHERE source_id = ?1",
                    params![imported_source_id],
                    |row| row.get(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap();
        assert_eq!(imported_snapshot_content, "本周订单 1200");

        target
            .with_conn(|conn| {
                conn.execute(
                    "UPDATE data_snapshots
                     SET collected_at = 1700000002000, observed_at = 1700000002000,
                         content_text = '本机更新数据', content_hash = 'local-newer-hash'
                     WHERE source_id = ?1",
                    params![imported_source_id],
                )?;
                Ok(())
            })
            .unwrap();
        let second = target.import_asset_snapshot(&snapshot, false).unwrap();
        let (snapshot_content, link_source_id): (String, i64) = target
            .with_conn(|conn| {
                let snapshot_content = conn.query_row(
                    "SELECT content_text FROM data_snapshots WHERE source_id = ?1",
                    params![imported_source_id],
                    |row| row.get(0),
                )?;
                let link_source_id = conn.query_row(
                    "SELECT source_id FROM data_source_links
                     WHERE source_ref_key = 'capture:42:active_url:test'",
                    [],
                    |row| row.get(0),
                )?;
                Ok((snapshot_content, link_source_id))
            })
            .unwrap();

        assert_ne!(imported_source_id, 91);
        assert_eq!(snapshot_content, "本机更新数据");
        assert_eq!(link_source_id, imported_source_id);
        assert_eq!(count_table(&target, "data_sources"), 2);
        assert_eq!(count_table(&target, "data_snapshots"), 1);
        assert_eq!(count_table(&target, "data_source_links"), 1);
        assert!(first
            .tables
            .iter()
            .any(|table| table.name == "data_sources" && table.inserted == 1));
        assert!(second
            .tables
            .iter()
            .any(|table| table.name == "data_sources" && table.updated == 1));
        assert!(second
            .tables
            .iter()
            .any(|table| table.name == "data_snapshots" && table.skipped == 1));
    }

    #[test]
    fn asset_snapshot_round_trips_external_db_from_env() {
        let Ok(db_path) = std::env::var("MEMORY_BREAD_SNAPSHOT_E2E_DB") else {
            return;
        };

        let source = StorageManager::open(Path::new(&db_path)).unwrap();
        let dir = tempdir().unwrap();
        let path = dir.path().join("external-assets.mbsnapshot.json");
        let export = source.export_asset_snapshot_to_path(&path).unwrap();
        assert!(export.file_size_bytes > 0);

        let target = StorageManager::open_in_memory().unwrap();
        let report = target
            .import_asset_snapshot_from_path(&path, false)
            .unwrap();

        assert_eq!(
            count_table(&target, "timelines"),
            summary_count(&export.manifest, "timelines")
        );
        assert_eq!(
            count_table(&target, "bake_knowledge"),
            summary_count(&export.manifest, "bake_knowledge")
        );
        assert_eq!(
            count_table(&target, "bake_sops"),
            summary_count(&export.manifest, "bake_sops")
        );
        assert_eq!(
            count_table(&target, "bake_documents"),
            summary_count(&export.manifest, "bake_documents")
        );
        assert_eq!(
            count_table(&target, "data_sources"),
            summary_count(&export.manifest, "data_sources")
        );
        assert_eq!(
            count_table(&target, "data_snapshots"),
            summary_count(&export.manifest, "data_snapshots")
        );
        assert_eq!(
            count_table(&target, "data_source_links"),
            summary_count(&export.manifest, "data_source_links")
        );
        assert_eq!(
            count_table(&target, "captures"),
            report.capture_refs.inserted as i64
        );
    }

    fn summary_count(manifest: &AssetSnapshotManifest, table: &str) -> i64 {
        manifest
            .table_summaries
            .iter()
            .find(|summary| summary.name == table)
            .map(|summary| summary.row_count as i64)
            .unwrap_or_default()
    }

    fn count_table(storage: &StorageManager, table: &str) -> i64 {
        storage
            .with_conn(|conn| {
                conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .map_err(StorageError::Sqlite)
            })
            .unwrap()
    }
}
