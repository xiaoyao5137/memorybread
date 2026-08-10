use std::collections::{HashMap, HashSet};

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::storage::{
    db::current_ts_ms,
    error::StorageError,
    fts::{build_fts_or_query, split_query_terms, DEFAULT_FTS_CANDIDATE_CAP},
    models_data::{
        DataExtractionSummary, DataSearchResult, DataSnapshotRecord, DataSourceRecord,
        DiscoveredSourceOutcome,
    },
    StorageManager,
};

const REPORT_FRESH_SECONDS: i64 = 15 * 60;
const DATA_TEXT_MAX_CHARS: usize = 80_000;
const DATA_MEMORY_VERSION: &str = "data-memory.v15";
const CURRENT_TIMELINE_DATA_FACT_VERSION: &str = "timeline-data-fact.v3";
const DATA_PERIOD_GRANULARITY: &str = "week";
const WEEK_MILLIS: i64 = 7 * 24 * 60 * 60 * 1000;
const EPOCH_FIRST_MONDAY_MILLIS: i64 = 4 * 24 * 60 * 60 * 1000;
const DATA_HISTORY_LIMIT: usize = 16;

#[derive(Debug, Clone)]
struct DataPeriodTag {
    key: String,
    start_at: i64,
    end_at: i64,
}

fn weekly_period_tag(observed_at: i64) -> DataPeriodTag {
    let offset = observed_at.saturating_sub(EPOCH_FIRST_MONDAY_MILLIS);
    let week_index = offset.div_euclid(WEEK_MILLIS);
    let start_at = week_index
        .saturating_mul(WEEK_MILLIS)
        .saturating_add(EPOCH_FIRST_MONDAY_MILLIS);
    DataPeriodTag {
        key: format!("week:{start_at}"),
        start_at,
        end_at: start_at.saturating_add(WEEK_MILLIS - 1),
    }
}

fn attach_period_tag(value: &mut Value, period: &DataPeriodTag) {
    if !value.is_object() {
        *value = json!({});
    }
    if let Some(object) = value.as_object_mut() {
        object.insert(
            "period".to_string(),
            json!({
                "granularity": DATA_PERIOD_GRANULARITY,
                "key": period.key,
                "start_at": period.start_at,
                "end_at": period.end_at,
            }),
        );
    }
}

#[derive(Debug)]
struct CaptureCandidate {
    id: i64,
    ts: i64,
    app_name: Option<String>,
    win_title: Option<String>,
    webpage_title: Option<String>,
    url: Option<String>,
    text: String,
    timeline_id: Option<i64>,
    timeline_summary: Option<String>,
    timeline_overview: Option<String>,
    timeline_details: Option<String>,
    timeline_updated_at_ms: Option<i64>,
}

#[derive(Debug)]
struct TimelineDataContext {
    capture_ids: Vec<i64>,
    source_urls: Vec<String>,
    observed_at: i64,
    metric_statements: Vec<Value>,
    model_fact_contract: Option<String>,
    model_facts: Vec<ModelDataFact>,
    evidence_text: String,
}

#[derive(Debug, Clone)]
struct ModelDataFact {
    title: String,
    subject: String,
    action: String,
    target_context: String,
    dimension: String,
    metric: String,
    value: String,
    unit: String,
    statement: String,
    evidence_quote: String,
    confidence: String,
    observed_at: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SemanticMetricRow {
    dimension: String,
    metric: String,
    value: String,
    note: String,
    statement: String,
    observed_at: Option<i64>,
}

#[derive(Debug, Clone)]
struct SemanticDataView {
    title: String,
    subject: String,
    identity: String,
    summary: String,
    rows: Vec<SemanticMetricRow>,
    statements: Vec<Value>,
    latest_observed_at: Option<i64>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct SemanticSubject {
    display: String,
    identity: String,
}

#[derive(Debug, Clone, Default)]
struct HistoricalRegenerationSummary {
    regenerated_count: usize,
    merged_count: usize,
    rejected_count: usize,
}

#[derive(Debug, Clone)]
struct DataSourceLinkRecord {
    source_ref_key: String,
    capture_id: Option<i64>,
    timeline_id: Option<i64>,
    link_kind: String,
    observed_at: i64,
    created_at: i64,
}

impl StorageManager {
    pub fn list_data_sources(
        &self,
        query: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<(Vec<DataSourceRecord>, i64), StorageError> {
        self.with_conn(|conn| {
            let mut candidates = Vec::new();
            let mut stmt = conn.prepare(
                "SELECT id, title, source_kind, source_url, access_mode, refresh_policy,
                        realtime_level, source_app_name, source_window_title, tags,
                        first_seen_at, last_seen_at, last_collected_at, last_success_at,
                        last_error_code, status, created_at, updated_at
                 FROM data_sources
                 WHERE deleted_at IS NULL
                 ORDER BY last_seen_at DESC, id DESC",
            )?;
            let rows = stmt.query_map([], map_data_source_row)?;
            for row in rows {
                candidates.push(row?);
            }
            // FTS5 预筛：data_snapshots_fts 命中快照对应的 source_id 可用时，
            // 在加载快照文本前先收窄候选（source 级字段命中的候选保留）；
            // FTS 不可用时返回 None，回退原有全量校验。
            let fts_source_ids = query
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .and_then(|q| data_snapshot_fts_source_ids(conn, q));
            for record in &mut candidates {
                let passes_prefilter = match &fts_source_ids {
                    Some(ids) => {
                        ids.contains(&record.id)
                            || data_source_base_fields_match(record, query.unwrap_or_default())
                    }
                    None => true,
                };
                if passes_prefilter {
                    let latest = latest_snapshot(conn, record)?;
                    record.latest_snapshot = latest;
                }
            }
            candidates.retain(is_presentable_data_source);
            if let Some(query) = query.map(str::trim).filter(|value| !value.is_empty()) {
                candidates.retain(|source| data_source_matches_query(source, query));
            }
            // 数据页展示的是最新快照的采集时间，因此默认顺序也必须使用同一字段。
            // last_seen_at 只表示来源近期再次被识别，不能把未更新的旧快照推到前面。
            candidates.sort_by(|left, right| {
                let left_collected_at = left
                    .latest_snapshot
                    .as_ref()
                    .map(|snapshot| snapshot.collected_at)
                    .unwrap_or(i64::MIN);
                let right_collected_at = right
                    .latest_snapshot
                    .as_ref()
                    .map(|snapshot| snapshot.collected_at)
                    .unwrap_or(i64::MIN);
                right_collected_at
                    .cmp(&left_collected_at)
                    .then_with(|| right.id.cmp(&left.id))
            });
            let total = candidates.len() as i64;
            let records = candidates.into_iter().skip(offset).take(limit).collect();
            Ok((records, total))
        })
    }

    pub fn list_pending_data_sources(
        &self,
        query: Option<&str>,
        limit: usize,
    ) -> Result<(Vec<DataSourceRecord>, i64), StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, title, source_kind, source_url, access_mode, refresh_policy,
                        realtime_level, source_app_name, source_window_title, tags,
                        first_seen_at, last_seen_at, last_collected_at, last_success_at,
                        last_error_code, status, created_at, updated_at
                 FROM data_sources
                 WHERE deleted_at IS NULL
                   AND source_kind = 'report_url'
                   AND NOT EXISTS (
                       SELECT 1 FROM data_snapshots snapshot
                       WHERE snapshot.source_id = data_sources.id
                   )
                 ORDER BY last_seen_at DESC, id DESC",
            )?;
            let rows = stmt.query_map([], map_data_source_row)?;
            let mut pending = rows.collect::<Result<Vec<_>, _>>()?;
            if let Some(query) = query.map(str::trim).filter(|value| !value.is_empty()) {
                pending.retain(|source| data_source_matches_query(source, query));
            }
            let total = pending.len() as i64;
            pending.truncate(limit.clamp(1, 5000));
            Ok((pending, total))
        })
    }

    pub fn get_data_source(&self, id: i64) -> Result<Option<DataSourceRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut record = conn
                .query_row(
                    "SELECT id, title, source_kind, source_url, access_mode, refresh_policy,
                            realtime_level, source_app_name, source_window_title, tags,
                            first_seen_at, last_seen_at, last_collected_at, last_success_at,
                            last_error_code, status, created_at, updated_at
                     FROM data_sources WHERE id = ?1 AND deleted_at IS NULL",
                    [id],
                    map_data_source_row,
                )
                .optional()?;
            if let Some(record) = &mut record {
                let latest = latest_snapshot(conn, record)?;
                record.latest_snapshot = latest;
            }
            Ok(record)
        })
    }

    pub fn save_data_snapshot(
        &self,
        source_id: i64,
        collector: &str,
        title: Option<&str>,
        content_text: &str,
        structured_data: &Value,
        collected_at: i64,
    ) -> Result<DataSnapshotRecord, StorageError> {
        self.with_conn(|conn| {
            let mut source = conn
                .query_row(
                    "SELECT id, title, source_kind, source_url, access_mode, refresh_policy,
                            realtime_level, source_app_name, source_window_title, tags,
                            first_seen_at, last_seen_at, last_collected_at, last_success_at,
                            last_error_code, status, created_at, updated_at
                     FROM data_sources WHERE id = ?1 AND deleted_at IS NULL",
                    [source_id],
                    map_data_source_row,
                )
                .optional()?
                .ok_or_else(|| StorageError::NotFound(format!("data source {source_id}")))?;
            let normalized_content = clip_text(content_text, DATA_TEXT_MAX_CHARS);
            let mut enriched_structured = structured_data.clone();
            let period = weekly_period_tag(collected_at);
            let semantic_context = semantic_context_for_source(
                &source,
                title.map(str::trim).filter(|value| !value.is_empty()),
                None,
            );
            let semantic = semantic_view_for_content(
                &normalized_content,
                &enriched_structured,
                Some(collected_at),
                &semantic_context,
            )
            .map(semantic_view_to_json)
            .unwrap_or_else(|| rejected_semantic_view_json("no_semantic_metric"));
            merge_semantic_view(&mut enriched_structured, semantic);
            attach_period_tag(&mut enriched_structured, &period);
            let structured_json = serde_json::to_string(&enriched_structured)?;
            let content_hash = hash_text(&format!("{normalized_content}\n{structured_json}"));
            let mut provenance = json!({
                "collector": collector,
                "cookie_persisted": false,
                "local_only": true
            });
            attach_period_tag(&mut provenance, &period);
            conn.execute(
                "INSERT INTO data_snapshots (
                    source_id, collected_at, observed_at, period_granularity, period_key,
                    period_start_at, period_end_at, collector, content_text,
                    structured_data, content_hash, freshness_ttl_seconds, provenance,
                    source_capture_ids, source_timeline_ids, status, created_at
                 ) VALUES (?1, ?2, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12,
                           '[]', '[]', 'success', ?2)
                 ON CONFLICT(source_id, period_key) DO UPDATE SET
                    collected_at = excluded.collected_at,
                    observed_at = excluded.observed_at,
                    collector = excluded.collector,
                    content_text = excluded.content_text,
                    structured_data = excluded.structured_data,
                    content_hash = excluded.content_hash,
                    freshness_ttl_seconds = excluded.freshness_ttl_seconds,
                    provenance = excluded.provenance,
                    source_capture_ids = excluded.source_capture_ids,
                    source_timeline_ids = excluded.source_timeline_ids,
                    status = excluded.status,
                    created_at = excluded.created_at",
                params![
                    source_id,
                    collected_at,
                    DATA_PERIOD_GRANULARITY,
                    period.key,
                    period.start_at,
                    period.end_at,
                    collector,
                    normalized_content,
                    structured_json,
                    content_hash,
                    REPORT_FRESH_SECONDS,
                    provenance.to_string(),
                ],
            )?;
            if let Some(title) = title.map(str::trim).filter(|value| !value.is_empty()) {
                source.title = clip_text(title, 240);
                conn.execute(
                    "UPDATE data_sources SET title = ?2, last_collected_at = ?3,
                            last_success_at = ?3, last_error_code = NULL, status = 'active',
                            updated_at = ?3 WHERE id = ?1",
                    params![source_id, &source.title, collected_at],
                )?;
            } else {
                conn.execute(
                    "UPDATE data_sources SET last_collected_at = ?2, last_success_at = ?2,
                            last_error_code = NULL, status = 'active', updated_at = ?2
                     WHERE id = ?1",
                    params![source_id, collected_at],
                )?;
            }
            latest_snapshot(conn, &source)?.ok_or_else(|| {
                StorageError::NotFound(format!("data snapshot for source {source_id}"))
            })
        })
    }

    pub fn mark_data_source_error(
        &self,
        source_id: i64,
        error_code: &str,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE data_sources SET last_error_code = ?2, status = 'unavailable',
                        updated_at = ?3 WHERE id = ?1",
                params![source_id, error_code, current_ts_ms()],
            )?;
            Ok(())
        })
    }

    pub fn delete_data_source(&self, source_id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let changed = conn.execute(
                "UPDATE data_sources
                 SET status = 'disabled', deleted_at = ?2, updated_at = ?2
                 WHERE id = ?1 AND deleted_at IS NULL",
                params![source_id, now],
            )?;
            Ok(changed > 0)
        })
    }

    /// 注册时间线推理中模型分类发现的数据报表/数据平台页面。
    ///
    /// 与 `extract_data_candidates` 的标记快路径互补：标记未命中但模型判定为
    /// data_report/data_platform 的页面，经 sidecar 代码校验后走这里注册为
    /// `report_url + browser_session + on_demand` 源，供创作时浏览器实时刷新。
    /// 拒绝条件：URL 无法规范化、capture 不存在、capture 命中敏感过滤。
    pub fn register_discovered_report_source(
        &self,
        url: &str,
        title: &str,
        capture_id: i64,
        timeline_id: Option<i64>,
        observed_at: i64,
    ) -> Result<DiscoveredSourceOutcome, StorageError> {
        let Some(canonical) = canonical_data_url(url) else {
            return Ok(DiscoveredSourceOutcome::RejectedInvalidUrl);
        };
        self.with_conn(|conn| {
            let capture = conn
                .query_row(
                    "SELECT ts, is_sensitive, app_name, win_title, webpage_title
                     FROM captures WHERE id = ?1",
                    [capture_id],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, Option<String>>(2)?,
                            row.get::<_, Option<String>>(3)?,
                            row.get::<_, Option<String>>(4)?,
                        ))
                    },
                )
                .optional()?;
            let Some((capture_ts, is_sensitive, app_name, win_title, webpage_title)) = capture
            else {
                return Ok(DiscoveredSourceOutcome::RejectedCaptureMissing);
            };
            if is_sensitive != 0 {
                return Ok(DiscoveredSourceOutcome::RejectedCaptureSensitive);
            }
            let observed = if observed_at > 0 { observed_at } else { capture_ts };
            let key = format!("report:{canonical}");
            let existed = source_exists(conn, &key)?;
            let fallback_title = webpage_title
                .as_deref()
                .or(win_title.as_deref())
                .unwrap_or("数据来源");
            let resolved_title = clip_text(
                if title.trim().is_empty() {
                    fallback_title
                } else {
                    title.trim()
                },
                240,
            );
            let now = current_ts_ms();
            conn.execute(
                "INSERT INTO data_sources (
                    canonical_key, title, source_kind, source_url, access_mode, refresh_policy,
                    realtime_level, source_app_name, source_window_title, tags, first_seen_at,
                    last_seen_at, status, created_at, updated_at
                 ) VALUES (?1, ?2, 'report_url', ?3, 'browser_session', 'on_demand', 'live',
                           ?4, ?5, '[\"report\", \"model_classified\"]', ?6, ?6, 'active', ?7, ?7)
                 ON CONFLICT(canonical_key) DO UPDATE SET
                    title = CASE WHEN LENGTH(excluded.title) > LENGTH(data_sources.title)
                                 THEN excluded.title ELSE data_sources.title END,
                    source_url = excluded.source_url,
                    source_app_name = COALESCE(excluded.source_app_name, data_sources.source_app_name),
                    source_window_title = COALESCE(excluded.source_window_title, data_sources.source_window_title),
                    last_seen_at = MAX(data_sources.last_seen_at, excluded.last_seen_at),
                    updated_at = excluded.updated_at",
                params![key, resolved_title, canonical, app_name, win_title, observed, now],
            )?;
            let source_id = source_id_for_key(conn, &key)?;
            let ref_key = format!(
                "capture:{}:discovered:{}:{}",
                capture_id,
                timeline_id.unwrap_or(0),
                hash_text(&canonical)
            );
            conn.execute(
                "INSERT OR IGNORE INTO data_source_links (
                    source_id, source_ref_key, capture_id, timeline_id, link_kind, observed_at, created_at
                 ) VALUES (?1, ?2, ?3, ?4, 'active_url', ?5, ?6)",
                params![source_id, ref_key, capture_id, timeline_id, observed, now],
            )?;
            Ok(DiscoveredSourceOutcome::Registered {
                source_id,
                created: !existed,
            })
        })
    }

    pub fn extract_data_candidates(
        &self,
        limit: usize,
    ) -> Result<DataExtractionSummary, StorageError> {
        self.with_conn(|conn| {
            let regeneration = regenerate_legacy_data_memories(conn, limit.clamp(1, 5000))?;
            let (candidates, newest_capture_id, backfill_before_capture_id) =
                load_capture_candidates(conn, limit.clamp(1, 5000))?;
            let mut summary = DataExtractionSummary {
                scanned_count: candidates.len(),
                historical_regenerated_count: regeneration.regenerated_count,
                historical_merged_count: regeneration.merged_count,
                historical_rejected_count: regeneration.rejected_count,
                ..DataExtractionSummary::default()
            };
            let mut handled_work_timelines = HashSet::new();
            for candidate in candidates {
                let mut candidate_created = false;
                let active_url = candidate
                    .url
                    .as_deref()
                    .and_then(canonical_data_url)
                    .filter(|url| {
                        looks_like_data_url(url, candidate_title(&candidate), &candidate.text)
                    });
                if let Some(url) = active_url {
                    let created = upsert_report_source(conn, &candidate, &url, "active_url")?;
                    summary.source_created_count += usize::from(created);
                    summary.source_updated_count += usize::from(!created);
                    candidate_created = true;
                }

                for embedded in extract_http_urls(&candidate.text).into_iter().take(12) {
                    let Some(url) = canonical_data_url(&embedded) else {
                        continue;
                    };
                    if candidate
                        .url
                        .as_deref()
                        .and_then(canonical_data_url)
                        .as_deref()
                        == Some(url.as_str())
                        || !looks_like_data_url(&url, candidate_title(&candidate), &candidate.text)
                    {
                        continue;
                    }
                    let created = upsert_report_source(conn, &candidate, &url, "embedded_url")?;
                    summary.source_created_count += usize::from(created);
                    summary.source_updated_count += usize::from(!created);
                    candidate_created = true;
                }

                if let Some(timeline_id) = candidate.timeline_id {
                    if !handled_work_timelines.contains(&timeline_id) {
                        handled_work_timelines.insert(timeline_id);
                        let context = load_timeline_data_context(conn, &candidate, timeline_id)?;
                        let mut semantic_context = [
                            candidate.timeline_summary.as_deref(),
                            candidate.timeline_overview.as_deref(),
                            candidate.timeline_details.as_deref(),
                            candidate.webpage_title.as_deref(),
                            candidate.win_title.as_deref(),
                        ]
                        .into_iter()
                        .flatten()
                        .collect::<Vec<_>>()
                        .join("\n");
                        if let Some(timeline_topic) = candidate
                            .timeline_overview
                            .as_deref()
                            .or(candidate.timeline_summary.as_deref())
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                        {
                            semantic_context
                                .push_str(&format!("\ntimeline_topic:{timeline_topic}"));
                        }
                        if let Some(window_title) = candidate
                            .webpage_title
                            .as_deref()
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                            .or_else(|| {
                                candidate
                                    .win_title
                                    .as_deref()
                                    .map(str::trim)
                                    .filter(|value| !value.is_empty())
                            })
                        {
                            semantic_context.push_str(&format!("\nwindow_title:{window_title}"));
                        }
                        if let Some(app_name) = candidate.app_name.as_deref() {
                            semantic_context.push_str(&format!("\napplication:{app_name}"));
                        }
                        let views =
                            semantic_views_for_timeline_context(&context, &semantic_context);
                        if !views.is_empty() {
                            for view in views {
                                let (created, snapshot_created) = upsert_work_memory_view(
                                    conn,
                                    &candidate,
                                    timeline_id,
                                    &context,
                                    &view,
                                )?;
                                summary.source_created_count += usize::from(created);
                                summary.source_updated_count += usize::from(!created);
                                summary.snapshot_created_count += usize::from(snapshot_created);
                            }
                            candidate_created = true;
                        }
                    }
                }

                if !candidate_created {
                    summary.skipped_count += 1;
                }
            }
            save_data_extraction_cursor(conn, newest_capture_id, backfill_before_capture_id)?;
            Ok(summary)
        })
    }

    pub fn regenerate_historical_data_memories(
        &self,
        limit: usize,
    ) -> Result<DataExtractionSummary, StorageError> {
        self.with_conn(|conn| {
            let regeneration = regenerate_legacy_data_memories(conn, limit.clamp(1, 5000))?;
            Ok(DataExtractionSummary {
                historical_regenerated_count: regeneration.regenerated_count,
                historical_merged_count: regeneration.merged_count,
                historical_rejected_count: regeneration.rejected_count,
                ..DataExtractionSummary::default()
            })
        })
    }

    pub fn search_data_sources(
        &self,
        query: &str,
        need_fresh: bool,
        as_of_ms: i64,
        limit: usize,
    ) -> Result<Vec<DataSearchResult>, StorageError> {
        let (mut sources, _) = self.list_data_sources(None, 5000, 0)?;
        let (pending, _) = self.list_pending_data_sources(None, 5000)?;
        sources.extend(pending);
        let mut histories =
            self.with_conn(|conn| load_snapshot_histories(conn, DATA_HISTORY_LIMIT))?;
        let terms = keyword_terms(query);
        let mut results = sources
            .into_iter()
            .filter(|source| source.status != "disabled")
            .map(|source| {
                let history = histories.remove(&source.id).unwrap_or_default();
                score_data_source(source, history, query, &terms, need_fresh, as_of_ms)
            })
            .filter(|result| result.relevance_score >= 0.12 || terms.is_empty())
            .collect::<Vec<_>>();
        results.sort_by(|left, right| {
            right
                .final_score
                .partial_cmp(&left.final_score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| right.collected_at.cmp(&left.collected_at))
        });
        results.truncate(limit.clamp(1, 50));
        Ok(results)
    }
}

fn load_snapshot_histories(
    conn: &Connection,
    per_source_limit: usize,
) -> Result<HashMap<i64, Vec<DataSnapshotRecord>>, StorageError> {
    let mut stmt = conn.prepare(
        "SELECT id, source_id, collected_at, observed_at, collector, content_text,
                structured_data, content_hash, freshness_ttl_seconds, provenance,
                source_capture_ids, source_timeline_ids, status, period_granularity,
                period_key, period_start_at, period_end_at
         FROM (
            SELECT snapshot.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY snapshot.source_id
                       ORDER BY snapshot.period_start_at DESC, snapshot.collected_at DESC,
                                snapshot.id DESC
                   ) AS period_rank
            FROM data_snapshots snapshot
            JOIN data_sources source ON source.id = snapshot.source_id
            WHERE source.deleted_at IS NULL AND source.status <> 'disabled'
         ) ranked
         WHERE period_rank <= ?1
         ORDER BY source_id ASC, period_start_at DESC, collected_at DESC, id DESC",
    )?;
    let rows = stmt.query_map([per_source_limit.max(1) as i64], map_data_snapshot_row)?;
    let mut histories: HashMap<i64, Vec<DataSnapshotRecord>> = HashMap::new();
    for row in rows {
        let snapshot = row?;
        histories
            .entry(snapshot.source_id)
            .or_default()
            .push(snapshot);
    }
    Ok(histories)
}

fn regenerate_legacy_data_memories(
    conn: &Connection,
    limit: usize,
) -> Result<HistoricalRegenerationSummary, StorageError> {
    let mut stmt = conn.prepare(
        "WITH latest_snapshot AS (
             SELECT snapshot.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY snapshot.source_id
                        ORDER BY snapshot.collected_at DESC, snapshot.id DESC
                    ) AS snapshot_rank
             FROM data_snapshots snapshot
         )
         SELECT source.id
         FROM data_sources source
         LEFT JOIN latest_snapshot snapshot
           ON snapshot.source_id = source.id AND snapshot.snapshot_rank = 1
         WHERE source.deleted_at IS NULL
           AND (
               COALESCE(json_extract(snapshot.structured_data, '$.extraction_version'), '') <> ?1
               OR (
                   COALESCE(json_extract(snapshot.structured_data, '$.semantic_origin'), '') = 'legacy_parser'
                   AND EXISTS (
                       SELECT 1
                       FROM json_each(COALESCE(snapshot.source_timeline_ids, '[]')) timeline_ref
                       JOIN timeline_data_fact_runs fact_run
                         ON fact_run.timeline_id = timeline_ref.value
                       JOIN timeline_data_facts fact
                         ON fact.timeline_id = timeline_ref.value
                       WHERE fact_run.contract_version = ?2
                         AND fact_run.accepted_count > 0
                         AND LENGTH(TRIM(fact.value)) >= 2
                         AND INSTR(
                             REPLACE(LOWER(COALESCE(snapshot.content_text, '')), ' ', ''),
                             REPLACE(LOWER(fact.value || fact.unit), ' ', '')
                         ) > 0
                   )
               )
           )
         ORDER BY source.id ASC
         LIMIT ?3",
    )?;
    let source_ids = stmt
        .query_map(
            params![
                DATA_MEMORY_VERSION,
                CURRENT_TIMELINE_DATA_FACT_VERSION,
                limit as i64
            ],
            |row| row.get::<_, i64>(0),
        )?
        .collect::<Result<Vec<_>, _>>()?;
    drop(stmt);
    if source_ids.is_empty() {
        return Ok(HistoricalRegenerationSummary::default());
    }

    conn.execute_batch("SAVEPOINT regenerate_data_memory")?;
    let result = regenerate_legacy_data_memories_inner(conn, &source_ids);
    match result {
        Ok(summary) => {
            conn.execute_batch("RELEASE SAVEPOINT regenerate_data_memory")?;
            Ok(summary)
        }
        Err(error) => {
            let _ = conn.execute_batch(
                "ROLLBACK TO SAVEPOINT regenerate_data_memory;
                 RELEASE SAVEPOINT regenerate_data_memory;",
            );
            Err(error)
        }
    }
}

fn regenerate_legacy_data_memories_inner(
    conn: &Connection,
    source_ids: &[i64],
) -> Result<HistoricalRegenerationSummary, StorageError> {
    let mut summary = HistoricalRegenerationSummary::default();
    for source_id in source_ids {
        let source = conn.query_row(
            "SELECT id, title, source_kind, source_url, access_mode, refresh_policy,
                    realtime_level, source_app_name, source_window_title, tags,
                    first_seen_at, last_seen_at, last_collected_at, last_success_at,
                    last_error_code, status, created_at, updated_at
             FROM data_sources WHERE id = ?1 AND deleted_at IS NULL",
            [source_id],
            map_data_source_row,
        )?;
        let Some(snapshot) = raw_latest_snapshot(conn, *source_id)? else {
            continue;
        };
        // 同一时间线的多个旧数据源可能在前一个 source 的多事实重建中被分别复用。
        // 候选列表是在重建开始前一次性生成的；到这里若最新快照已升级完成，直接
        // 记为已处理，避免再次展开同一组事实后把刚复用的 source 当重复项删除。
        if snapshot
            .structured_data
            .get("extraction_version")
            .and_then(Value::as_str)
            == Some(DATA_MEMORY_VERSION)
            && snapshot
                .structured_data
                .get("semantic_origin")
                .and_then(Value::as_str)
                == Some("model_structured_fact")
        {
            summary.regenerated_count += 1;
            continue;
        }
        let previous_subject = snapshot
            .structured_data
            .get("semantic_subject")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let semantic_context = semantic_context_for_source(&source, None, previous_subject);
        if source.source_kind == "work_memory" {
            let linked_model_views = linked_current_model_fact_views(conn, &snapshot)?;
            let (views, has_current_contract) = if let Some(model_views) = linked_model_views {
                (
                    model_views
                        .into_iter()
                        .filter(|view| semantic_view_matches_legacy_snapshot(&snapshot, view))
                        .collect(),
                    true,
                )
            } else {
                (
                    semantic_views_for_content(
                        &snapshot.content_text,
                        &snapshot.structured_data,
                        snapshot.observed_at,
                        &semantic_context,
                    ),
                    false,
                )
            };
            let views = if views.iter().any(|view| {
                view.statements.iter().any(|statement| {
                    statement.get("fact_contract").and_then(Value::as_str)
                        == Some(CURRENT_TIMELINE_DATA_FACT_VERSION)
                })
            }) {
                views
            } else {
                views
                    .into_iter()
                    .max_by_key(semantic_view_quality_score)
                    .into_iter()
                    .collect()
            };
            if views.is_empty() {
                if !has_current_contract {
                    persist_rejected_snapshot(conn, &snapshot)?;
                    summary.rejected_count += 1;
                }
                summary.regenerated_count += 1;
                continue;
            }
            let merged = regenerate_work_memory_source(conn, &source, &snapshot, &views)?;
            summary.regenerated_count += 1;
            summary.merged_count += merged;
        } else {
            let views = semantic_views_for_content(
                &snapshot.content_text,
                &snapshot.structured_data,
                snapshot.observed_at,
                &semantic_context,
            );
            let semantic = views
                .into_iter()
                .max_by_key(|view| view.rows.len() * 100 + view.summary.chars().count().min(260));
            if let Some(view) = semantic {
                persist_enriched_snapshot(conn, &snapshot, semantic_view_to_json(view))?;
            } else {
                persist_rejected_snapshot(conn, &snapshot)?;
                summary.rejected_count += 1;
            }
            summary.regenerated_count += 1;
        }
    }
    Ok(summary)
}

fn linked_current_model_fact_views(
    conn: &Connection,
    snapshot: &DataSnapshotRecord,
) -> Result<Option<Vec<SemanticDataView>>, StorageError> {
    let mut found_current_contract = false;
    let mut views: Vec<SemanticDataView> = Vec::new();
    for timeline_id in &snapshot.source_timeline_ids {
        let contract = conn
            .query_row(
                "SELECT contract_version FROM timeline_data_fact_runs WHERE timeline_id = ?1",
                [timeline_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        if contract.as_deref() != Some(CURRENT_TIMELINE_DATA_FACT_VERSION) {
            continue;
        }
        found_current_contract = true;

        let mut evidence_parts = Vec::new();
        if let Some(timeline_text) = conn
            .query_row(
                "SELECT TRIM(COALESCE(summary, '') || char(10) ||
                             COALESCE(overview, '') || char(10) || COALESCE(details, ''))
                 FROM timelines WHERE id = ?1",
                [timeline_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?
            .filter(|text| !text.trim().is_empty())
        {
            evidence_parts.push(timeline_text);
        }
        let mut capture_stmt = conn.prepare(
            "SELECT TRIM(COALESCE(ax_text, '') || char(10) || COALESCE(ocr_text, '') ||
                         char(10) || COALESCE(input_text, '') || char(10) ||
                         COALESCE(audio_text, ''))
             FROM captures
             WHERE is_sensitive = 0 AND timeline_id = ?1
             ORDER BY ts ASC, id ASC",
        )?;
        for capture_text in capture_stmt.query_map([timeline_id], |row| row.get::<_, String>(0))? {
            let capture_text = capture_text?;
            if !capture_text.trim().is_empty() {
                evidence_parts.push(capture_text);
            }
        }

        let mut fact_stmt = conn.prepare(
            "SELECT title, subject, action, target_context, dimension, metric, value, unit,
                    statement, evidence_quote, confidence, observed_at
             FROM timeline_data_facts
             WHERE timeline_id = ?1
             ORDER BY id ASC",
        )?;
        let facts = fact_stmt
            .query_map([timeline_id], |row| {
                Ok(ModelDataFact {
                    title: row.get(0)?,
                    subject: row.get(1)?,
                    action: row.get(2)?,
                    target_context: row.get(3)?,
                    dimension: row.get(4)?,
                    metric: row.get(5)?,
                    value: row.get(6)?,
                    unit: row.get(7)?,
                    statement: row.get(8)?,
                    evidence_quote: row.get(9)?,
                    confidence: row.get(10)?,
                    observed_at: row.get(11)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let timeline_views = semantic_views_from_model_facts(
            &facts,
            &evidence_parts.join("\n"),
            CURRENT_TIMELINE_DATA_FACT_VERSION,
        );
        for view in timeline_views {
            if let Some(existing) = views
                .iter_mut()
                .find(|existing| existing.identity == view.identity)
            {
                merge_semantic_rows(&mut existing.rows, view.rows);
                for statement in view.statements {
                    if !existing.statements.contains(&statement) {
                        existing.statements.push(statement);
                    }
                }
                if view.latest_observed_at >= existing.latest_observed_at {
                    existing.title = view.title;
                    existing.summary = view.summary;
                    existing.latest_observed_at = view.latest_observed_at;
                }
            } else {
                views.push(view);
            }
        }
    }
    if found_current_contract {
        views.sort_by(|left, right| {
            right
                .latest_observed_at
                .cmp(&left.latest_observed_at)
                .then_with(|| left.title.cmp(&right.title))
        });
        Ok(Some(views))
    } else {
        Ok(None)
    }
}

fn semantic_view_quality_score(view: &SemanticDataView) -> usize {
    let dimension_count = view
        .rows
        .iter()
        .filter(|row| !row.dimension.is_empty())
        .count();
    view.rows.len() * 1000
        + dimension_count * 100
        + view.title.chars().count().min(80)
        + view.summary.chars().count().min(260)
}

fn regenerate_work_memory_source(
    conn: &Connection,
    source: &DataSourceRecord,
    snapshot: &DataSnapshotRecord,
    views: &[SemanticDataView],
) -> Result<usize, StorageError> {
    let links = load_data_source_links(conn, source.id)?;
    let now = current_ts_ms();
    let mut reused_legacy_source = false;
    let mut merged_count = 0;
    let mut first_target_id = None;
    let mut first_target_snapshot_id = None;

    for (index, view) in views.iter().enumerate() {
        let fallback_scope = snapshot
            .source_timeline_ids
            .first()
            .map(|timeline_id| format!("timeline:{timeline_id}"))
            .unwrap_or_else(|| format!("legacy-source:{}", source.id));
        let scope = semantic_source_scope(
            source.source_window_title.as_deref(),
            source.source_app_name.as_deref(),
            &fallback_scope,
        );
        let identity_hash = hash_text(&format!("{scope}|{}", view.identity));
        let key = format!("memory:semantic:{DATA_MEMORY_VERSION}:{identity_hash}");
        let existing_target = conn
            .query_row(
                "SELECT id FROM data_sources WHERE canonical_key = ?1",
                [&key],
                |row| row.get::<_, i64>(0),
            )
            .optional()?;
        let reusable_legacy_target = if index > 0 && existing_target.is_none() {
            reusable_linked_legacy_source(conn, source.id, &snapshot.source_timeline_ids, view)?
        } else {
            None
        };
        let target_id = if index == 0 && existing_target == Some(source.id) {
            reused_legacy_source = true;
            source.id
        } else if index == 0 && existing_target.is_none() {
            conn.execute(
                "UPDATE data_sources
                 SET canonical_key = ?2, title = ?3, updated_at = ?4
                 WHERE id = ?1",
                params![source.id, key, view.title, now],
            )?;
            reused_legacy_source = true;
            source.id
        } else if let Some(target_id) = reusable_legacy_target {
            conn.execute(
                "UPDATE data_sources
                 SET canonical_key = ?2, title = ?3, updated_at = ?4
                 WHERE id = ?1",
                params![target_id, key, view.title, now],
            )?;
            target_id
        } else {
            conn.execute(
                "INSERT INTO data_sources (
                    canonical_key, title, source_kind, source_url, access_mode,
                    refresh_policy, realtime_level, source_app_name, source_window_title,
                    tags, first_seen_at, last_seen_at, last_collected_at, last_success_at,
                    last_error_code, status, created_at, updated_at
                 ) VALUES (?1, ?2, 'work_memory', ?3, 'memory_only', 'never', 'observed',
                           ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
                 ON CONFLICT(canonical_key) DO UPDATE SET
                    title = CASE
                                WHEN excluded.last_seen_at >= data_sources.last_seen_at
                                THEN excluded.title ELSE data_sources.title
                            END,
                    source_url = COALESCE(excluded.source_url, data_sources.source_url),
                    source_app_name = CASE
                                WHEN excluded.last_seen_at >= data_sources.last_seen_at
                                THEN excluded.source_app_name ELSE data_sources.source_app_name
                            END,
                    source_window_title = CASE
                                WHEN excluded.last_seen_at >= data_sources.last_seen_at
                                THEN excluded.source_window_title ELSE data_sources.source_window_title
                            END,
                    first_seen_at = MIN(data_sources.first_seen_at, excluded.first_seen_at),
                    last_seen_at = MAX(data_sources.last_seen_at, excluded.last_seen_at),
                    last_collected_at = MAX(COALESCE(data_sources.last_collected_at, 0), COALESCE(excluded.last_collected_at, 0)),
                    last_success_at = MAX(COALESCE(data_sources.last_success_at, 0), COALESCE(excluded.last_success_at, 0)),
                    updated_at = excluded.updated_at",
                params![
                    key,
                    view.title,
                    source.source_url,
                    source.source_app_name,
                    source.source_window_title,
                    serde_json::to_string(&source.tags)?,
                    source.first_seen_at,
                    source.last_seen_at,
                    source.last_collected_at,
                    source.last_success_at,
                    source.last_error_code,
                    source.status,
                    source.created_at,
                    now,
                ],
            )?;
            let target_id = source_id_for_key(conn, &key)?;
            if existing_target.is_some() && target_id != source.id {
                merged_count += 1;
            }
            target_id
        };

        conn.execute(
            "UPDATE data_sources SET title = ?2, updated_at = ?3 WHERE id = ?1",
            params![target_id, view.title, now],
        )?;

        if target_id != source.id {
            duplicate_data_source_links(conn, target_id, &identity_hash, &links)?;
        }
        let target_snapshot_id = upsert_regenerated_work_snapshot(conn, target_id, snapshot, view)?;
        if index == 0 {
            first_target_id = Some(target_id);
            first_target_snapshot_id = Some(target_snapshot_id);
        }
    }

    if !reused_legacy_source {
        if let (Some(target_id), Some(target_snapshot_id)) =
            (first_target_id, first_target_snapshot_id)
        {
            conn.execute(
                "UPDATE creation_evidence_assets
                 SET source_id = ?2,
                     data_snapshot_id = CASE WHEN data_snapshot_id = ?3 THEN ?4 ELSE data_snapshot_id END,
                     updated_at = ?5
                 WHERE source_id = ?1 OR data_snapshot_id = ?3",
                params![source.id, target_id, snapshot.id, target_snapshot_id, now],
            )?;
        }
        conn.execute(
            "UPDATE data_sources
             SET status = 'disabled', deleted_at = ?2, updated_at = ?2
             WHERE id = ?1 AND deleted_at IS NULL",
            params![source.id, now],
        )?;
    }
    Ok(merged_count)
}

fn reusable_linked_legacy_source(
    conn: &Connection,
    current_source_id: i64,
    timeline_ids: &[i64],
    view: &SemanticDataView,
) -> Result<Option<i64>, StorageError> {
    for timeline_id in timeline_ids {
        let mut stmt = conn.prepare(
            "WITH latest_snapshot AS (
                     SELECT snapshot.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY snapshot.source_id
                                ORDER BY snapshot.collected_at DESC, snapshot.id DESC
                            ) AS snapshot_rank
                     FROM data_snapshots snapshot
                 )
                 SELECT source.id
                 FROM data_sources source
                 JOIN latest_snapshot snapshot
                   ON snapshot.source_id = source.id AND snapshot.snapshot_rank = 1
                 WHERE source.id <> ?1
                   AND source.deleted_at IS NULL
                   AND source.source_kind = 'work_memory'
                   AND COALESCE(
                       json_extract(snapshot.structured_data, '$.semantic_origin'), ''
                   ) = 'legacy_parser'
                   AND EXISTS (
                       SELECT 1
                       FROM json_each(COALESCE(snapshot.source_timeline_ids, '[]')) timeline_ref
                       WHERE timeline_ref.value = ?2
                   )
                 ORDER BY ABS(source.id - ?1) ASC, source.id ASC",
        )?;
        let candidates = stmt
            .query_map(params![current_source_id, timeline_id], |row| {
                row.get::<_, i64>(0)
            })?
            .collect::<Result<Vec<_>, _>>()?;
        for candidate in candidates {
            let Some(snapshot) = raw_latest_snapshot(conn, candidate)? else {
                continue;
            };
            if semantic_view_matches_legacy_snapshot(&snapshot, view) {
                return Ok(Some(candidate));
            }
        }
    }
    Ok(None)
}

fn semantic_view_matches_legacy_snapshot(
    snapshot: &DataSnapshotRecord,
    view: &SemanticDataView,
) -> bool {
    let haystack = normalize_evidence_text(&format!(
        "{}\n{}",
        snapshot.content_text, snapshot.structured_data
    ));
    view.rows.iter().any(|row| {
        let value = normalize_evidence_text(&row.value);
        let metric = normalize_evidence_text(&row.metric);
        let value_matches = (value.chars().count() >= 2 && haystack.contains(&value))
            || numeric_tokens_hit(&value, &haystack);
        value_matches && semantic_metric_matches_haystack(&haystack, &metric)
    })
}

fn semantic_metric_matches_haystack(haystack: &str, metric: &str) -> bool {
    if metric.is_empty() {
        return false;
    }
    if haystack.contains(metric) {
        return true;
    }
    let chars = metric.chars().collect::<Vec<_>>();
    if chars.len() < 2 {
        return false;
    }
    // 旧标题常把动作或对象塞进指标前缀（“用户设定了视频时长”），因此允许
    // 新指标与旧正文只共享末尾的度量语义；不能用“视频/生成”等前缀重合来
    // 判定，否则同为 15 秒的视频时长和接口耗时仍会串线。
    let tail = chars[chars.len() - 2..].iter().collect::<String>();
    !matches!(tail.as_str(), "任务" | "用户" | "当前" | "数据" | "结果") && haystack.contains(&tail)
}

fn load_data_source_links(
    conn: &Connection,
    source_id: i64,
) -> Result<Vec<DataSourceLinkRecord>, StorageError> {
    let mut stmt = conn.prepare(
        "SELECT source_ref_key, capture_id, timeline_id, link_kind, observed_at, created_at
         FROM data_source_links WHERE source_id = ?1 ORDER BY id ASC",
    )?;
    let links = stmt
        .query_map([source_id], |row| {
            Ok(DataSourceLinkRecord {
                source_ref_key: row.get(0)?,
                capture_id: row.get(1)?,
                timeline_id: row.get(2)?,
                link_kind: row.get(3)?,
                observed_at: row.get(4)?,
                created_at: row.get(5)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(links)
}

fn duplicate_data_source_links(
    conn: &Connection,
    target_id: i64,
    identity_hash: &str,
    links: &[DataSourceLinkRecord],
) -> Result<(), StorageError> {
    for link in links {
        let ref_key = format!("{}:semantic:{}", link.source_ref_key, identity_hash);
        let capture_id = match link.capture_id {
            Some(capture_id)
                if conn.query_row(
                    "SELECT COUNT(*) > 0 FROM captures WHERE id = ?1",
                    [capture_id],
                    |row| row.get::<_, bool>(0),
                )? =>
            {
                Some(capture_id)
            }
            _ => None,
        };
        let timeline_id = match link.timeline_id {
            Some(timeline_id)
                if conn.query_row(
                    "SELECT COUNT(*) > 0 FROM timelines WHERE id = ?1",
                    [timeline_id],
                    |row| row.get::<_, bool>(0),
                )? =>
            {
                Some(timeline_id)
            }
            _ => None,
        };
        conn.execute(
            "INSERT OR IGNORE INTO data_source_links (
                source_id, source_ref_key, capture_id, timeline_id, link_kind, observed_at, created_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                target_id,
                ref_key,
                capture_id,
                timeline_id,
                link.link_kind,
                link.observed_at,
                link.created_at,
            ],
        )?;
    }
    Ok(())
}

fn upsert_regenerated_work_snapshot(
    conn: &Connection,
    target_id: i64,
    source_snapshot: &DataSnapshotRecord,
    view: &SemanticDataView,
) -> Result<i64, StorageError> {
    let content = clip_text(&semantic_view_content(view), DATA_TEXT_MAX_CHARS);
    let observed_at = view
        .latest_observed_at
        .into_iter()
        .chain(source_snapshot.observed_at)
        .chain(std::iter::once(source_snapshot.collected_at))
        .max()
        .unwrap_or(source_snapshot.collected_at);
    let period = weekly_period_tag(observed_at);
    let mut provenance = source_snapshot.provenance.clone();
    if !provenance.is_object() {
        provenance = json!({});
    }
    if let Some(object) = provenance.as_object_mut() {
        object.insert(
            "generation_version".to_string(),
            Value::String(DATA_MEMORY_VERSION.to_string()),
        );
        object.insert(
            "semantic_identity".to_string(),
            Value::String(view.identity.clone()),
        );
        object.insert(
            "semantic_subject".to_string(),
            Value::String(view.subject.clone()),
        );
        object.insert("history_regenerated".to_string(), Value::Bool(true));
    }
    attach_period_tag(&mut provenance, &period);
    let mut structured = semantic_view_to_json(view.clone());
    attach_period_tag(&mut structured, &period);
    let content_hash = hash_text(&format!("{content}\n{structured}"));
    conn.execute(
        "INSERT INTO data_snapshots (
            source_id, collected_at, observed_at, period_granularity, period_key,
            period_start_at, period_end_at, collector, content_text, structured_data,
            content_hash, freshness_ttl_seconds, provenance, source_capture_ids,
            source_timeline_ids, status, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'memory_extract', ?8, ?9, ?10, 0,
                   ?11, ?12, ?13, 'success', ?14)
         ON CONFLICT(source_id, period_key) DO UPDATE SET
            collected_at = excluded.collected_at,
            observed_at = excluded.observed_at,
            collector = excluded.collector,
            content_text = excluded.content_text,
            structured_data = excluded.structured_data,
            content_hash = excluded.content_hash,
            freshness_ttl_seconds = excluded.freshness_ttl_seconds,
            provenance = excluded.provenance,
            source_capture_ids = excluded.source_capture_ids,
            source_timeline_ids = excluded.source_timeline_ids,
            status = excluded.status,
            created_at = excluded.created_at
         WHERE excluded.observed_at >= COALESCE(data_snapshots.observed_at, data_snapshots.collected_at)",
        params![
            target_id,
            observed_at,
            observed_at,
            DATA_PERIOD_GRANULARITY,
            &period.key,
            period.start_at,
            period.end_at,
            content,
            structured.to_string(),
            content_hash,
            provenance.to_string(),
            serde_json::to_string(&source_snapshot.source_capture_ids)?,
            serde_json::to_string(&source_snapshot.source_timeline_ids)?,
            current_ts_ms(),
        ],
    )?;
    conn.query_row(
        "SELECT id FROM data_snapshots
         WHERE source_id = ?1 AND period_key = ?2 LIMIT 1",
        params![target_id, &period.key],
        |row| row.get::<_, i64>(0),
    )
    .map_err(Into::into)
}

fn persist_enriched_snapshot(
    conn: &Connection,
    snapshot: &DataSnapshotRecord,
    semantic: Value,
) -> Result<(), StorageError> {
    let mut structured = snapshot.structured_data.clone();
    merge_semantic_view(&mut structured, semantic);
    let content_hash = hash_text(&format!("{}\n{}", snapshot.content_text, structured));
    conn.execute(
        "UPDATE data_snapshots SET structured_data = ?2, content_hash = ?3 WHERE id = ?1",
        params![snapshot.id, structured.to_string(), content_hash],
    )?;
    Ok(())
}

fn persist_rejected_snapshot(
    conn: &Connection,
    snapshot: &DataSnapshotRecord,
) -> Result<(), StorageError> {
    persist_enriched_snapshot(
        conn,
        snapshot,
        rejected_semantic_view_json("no_semantic_metric"),
    )
}

fn raw_latest_snapshot(
    conn: &Connection,
    source_id: i64,
) -> Result<Option<DataSnapshotRecord>, StorageError> {
    conn.query_row(
        "SELECT id, source_id, collected_at, observed_at, collector, content_text,
                structured_data, content_hash, freshness_ttl_seconds, provenance,
                source_capture_ids, source_timeline_ids, status, period_granularity,
                period_key, period_start_at, period_end_at
         FROM data_snapshots WHERE source_id = ?1
         ORDER BY collected_at DESC, id DESC LIMIT 1",
        [source_id],
        map_data_snapshot_row,
    )
    .optional()
    .map_err(Into::into)
}

fn map_data_snapshot_row(row: &Row<'_>) -> rusqlite::Result<DataSnapshotRecord> {
    Ok(DataSnapshotRecord {
        id: row.get(0)?,
        source_id: row.get(1)?,
        collected_at: row.get(2)?,
        observed_at: row.get(3)?,
        collector: row.get(4)?,
        content_text: row.get(5)?,
        structured_data: parse_json_value(row.get::<_, String>(6)?, json!({})),
        content_hash: row.get(7)?,
        freshness_ttl_seconds: row.get(8)?,
        provenance: parse_json_value(row.get::<_, String>(9)?, json!({})),
        source_capture_ids: parse_json_i64(row.get::<_, String>(10)?),
        source_timeline_ids: parse_json_i64(row.get::<_, String>(11)?),
        status: row.get(12)?,
        period_granularity: row.get(13)?,
        period_key: row.get(14)?,
        period_start_at: row.get(15)?,
        period_end_at: row.get(16)?,
    })
}

fn load_capture_candidates(
    conn: &Connection,
    limit: usize,
) -> Result<(Vec<CaptureCandidate>, i64, Option<i64>), StorageError> {
    let global_max_id = conn.query_row("SELECT COALESCE(MAX(id), 0) FROM captures", [], |row| {
        row.get::<_, i64>(0)
    })?;
    let cursor = conn
        .query_row(
            "SELECT newest_capture_id, backfill_before_capture_id
             FROM data_extraction_state WHERE singleton_id = 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<i64>>(1)?)),
        )
        .optional()?;

    if cursor.is_none() {
        let candidates = query_capture_candidates(conn, "c.id >= ?1", 0, limit, "DESC")?;
        let backfill_before = if candidates.len() < limit {
            None
        } else {
            candidates.iter().map(|item| item.id).min()
        };
        return Ok((candidates, global_max_id, backfill_before));
    }

    let (current_newest, current_backfill_before) = cursor.unwrap_or_default();
    let new_budget = ((limit * 3) / 4).max(1);
    let mut candidates =
        query_capture_candidates(conn, "c.id > ?1", current_newest, new_budget, "ASC")?;
    let next_newest = if candidates.len() < new_budget {
        global_max_id
    } else {
        candidates
            .iter()
            .map(|item| item.id)
            .max()
            .unwrap_or(current_newest)
    };

    let remaining = limit.saturating_sub(candidates.len());
    let mut next_backfill_before = current_backfill_before;
    if remaining > 0 {
        if let Some(before_id) = current_backfill_before.filter(|value| *value > 0) {
            let mut backfill =
                query_capture_candidates(conn, "c.id < ?1", before_id, remaining, "DESC")?;
            next_backfill_before = if backfill.len() < remaining {
                None
            } else {
                backfill.iter().map(|item| item.id).min()
            };
            candidates.append(&mut backfill);
        }
    }
    Ok((candidates, next_newest, next_backfill_before))
}

fn query_capture_candidates(
    conn: &Connection,
    cursor_predicate: &str,
    cursor_value: i64,
    limit: usize,
    direction: &str,
) -> Result<Vec<CaptureCandidate>, StorageError> {
    debug_assert!(matches!(direction, "ASC" | "DESC"));
    let sql = format!(
        "SELECT c.id, c.ts, c.app_name, c.win_title, c.webpage_title, c.url,
                COALESCE(c.ax_text, ''), COALESCE(c.ocr_text, ''),
                COALESCE(c.input_text, ''), COALESCE(c.audio_text, ''),
                c.timeline_id, t.summary, t.overview, t.details, t.updated_at_ms
         FROM captures c
         LEFT JOIN timelines t ON t.id = c.timeline_id
         WHERE c.is_sensitive = 0
           AND ({cursor_predicate})
           AND (COALESCE(c.url, '') <> ''
                OR c.timeline_id IS NOT NULL
                OR COALESCE(c.ax_text, '') <> ''
                OR COALESCE(c.ocr_text, '') <> ''
                OR COALESCE(c.input_text, '') <> ''
                OR COALESCE(c.audio_text, '') <> '')
         ORDER BY c.id {direction} LIMIT ?2"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![cursor_value, limit as i64], |row| {
        let text = [
            row.get::<_, String>(6)?,
            row.get::<_, String>(7)?,
            row.get::<_, String>(8)?,
            row.get::<_, String>(9)?,
        ]
        .into_iter()
        .filter(|part| !part.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n");
        Ok(CaptureCandidate {
            id: row.get(0)?,
            ts: row.get(1)?,
            app_name: row.get(2)?,
            win_title: row.get(3)?,
            webpage_title: row.get(4)?,
            url: row.get(5)?,
            text,
            timeline_id: row.get(10)?,
            timeline_summary: row.get(11)?,
            timeline_overview: row.get(12)?,
            timeline_details: row.get(13)?,
            timeline_updated_at_ms: row.get(14)?,
        })
    })?;
    rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
}

fn save_data_extraction_cursor(
    conn: &Connection,
    newest_capture_id: i64,
    backfill_before_capture_id: Option<i64>,
) -> Result<(), StorageError> {
    conn.execute(
        "INSERT INTO data_extraction_state (
            singleton_id, newest_capture_id, backfill_before_capture_id, updated_at
         ) VALUES (1, ?1, ?2, ?3)
         ON CONFLICT(singleton_id) DO UPDATE SET
            newest_capture_id = excluded.newest_capture_id,
            backfill_before_capture_id = excluded.backfill_before_capture_id,
            updated_at = excluded.updated_at",
        params![
            newest_capture_id,
            backfill_before_capture_id,
            current_ts_ms()
        ],
    )?;
    Ok(())
}

fn upsert_report_source(
    conn: &Connection,
    candidate: &CaptureCandidate,
    url: &str,
    link_kind: &str,
) -> Result<bool, StorageError> {
    let key = format!("report:{url}");
    let existed = source_exists(conn, &key)?;
    let title = clip_text(candidate_title(candidate), 240);
    let now = current_ts_ms();
    conn.execute(
        "INSERT INTO data_sources (
            canonical_key, title, source_kind, source_url, access_mode, refresh_policy,
            realtime_level, source_app_name, source_window_title, tags, first_seen_at,
            last_seen_at, status, created_at, updated_at
         ) VALUES (?1, ?2, 'report_url', ?3, 'browser_session', 'on_demand', 'live',
                   ?4, ?5, '[\"report\"]', ?6, ?6, 'active', ?7, ?7)
         ON CONFLICT(canonical_key) DO UPDATE SET
            title = CASE WHEN LENGTH(excluded.title) > LENGTH(data_sources.title)
                         THEN excluded.title ELSE data_sources.title END,
            source_url = excluded.source_url,
            source_app_name = COALESCE(excluded.source_app_name, data_sources.source_app_name),
            source_window_title = COALESCE(excluded.source_window_title, data_sources.source_window_title),
            last_seen_at = MAX(data_sources.last_seen_at, excluded.last_seen_at),
            updated_at = excluded.updated_at",
        params![
            key,
            title,
            url,
            candidate.app_name,
            candidate.win_title,
            candidate.ts,
            now,
        ],
    )?;
    let source_id = source_id_for_key(conn, &key)?;
    let ref_key = format!("capture:{}:{link_kind}:{}", candidate.id, hash_text(url));
    conn.execute(
        "INSERT OR IGNORE INTO data_source_links (
            source_id, source_ref_key, capture_id, timeline_id, link_kind, observed_at, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            source_id,
            ref_key,
            candidate.id,
            candidate.timeline_id,
            link_kind,
            candidate.ts,
            now,
        ],
    )?;
    Ok(!existed)
}

fn upsert_work_memory_view(
    conn: &Connection,
    candidate: &CaptureCandidate,
    timeline_id: i64,
    context: &TimelineDataContext,
    view: &SemanticDataView,
) -> Result<(bool, bool), StorageError> {
    let scope = semantic_source_scope(
        candidate
            .webpage_title
            .as_deref()
            .map(str::trim)
            .filter(|title| !title.is_empty())
            .or(candidate.win_title.as_deref()),
        candidate.app_name.as_deref(),
        &format!("timeline:{timeline_id}"),
    );
    let identity_hash = hash_text(&format!("{scope}|{}", view.identity));
    let key = format!("memory:semantic:{DATA_MEMORY_VERSION}:{identity_hash}");
    let existed = source_exists(conn, &key)?;
    let source_title = clip_text(&view.title, 240);
    let source_url = candidate
        .url
        .as_deref()
        .and_then(canonical_data_url)
        .filter(|url| !url.is_empty())
        .or_else(|| context.source_urls.first().cloned());
    let now = current_ts_ms();
    conn.execute(
        "INSERT INTO data_sources (
            canonical_key, title, source_kind, source_url, access_mode, refresh_policy,
            realtime_level, source_app_name, source_window_title, tags, first_seen_at,
            last_seen_at, status, created_at, updated_at
         ) VALUES (?1, ?2, 'work_memory', ?3, 'memory_only', 'never', 'observed',
                   ?4, ?5, '[\"work_memory\"]', ?6, ?6, 'active', ?7, ?7)
         ON CONFLICT(canonical_key) DO UPDATE SET
            title = CASE
                        WHEN excluded.last_seen_at >= data_sources.last_seen_at
                        THEN excluded.title ELSE data_sources.title
                    END,
            source_url = COALESCE(excluded.source_url, data_sources.source_url),
            source_app_name = CASE
                        WHEN excluded.last_seen_at >= data_sources.last_seen_at
                        THEN excluded.source_app_name ELSE data_sources.source_app_name
                    END,
            source_window_title = CASE
                        WHEN excluded.last_seen_at >= data_sources.last_seen_at
                        THEN excluded.source_window_title ELSE data_sources.source_window_title
                    END,
            first_seen_at = MIN(data_sources.first_seen_at, excluded.first_seen_at),
            last_seen_at = MAX(data_sources.last_seen_at, excluded.last_seen_at),
            updated_at = excluded.updated_at",
        params![
            key,
            source_title,
            source_url,
            candidate.app_name,
            candidate.win_title,
            candidate.ts,
            now
        ],
    )?;
    let source_id = source_id_for_key(conn, &key)?;
    for capture_id in &context.capture_ids {
        let ref_key = format!("timeline:{timeline_id}:work_memory:{identity_hash}:{capture_id}");
        conn.execute(
            "INSERT OR IGNORE INTO data_source_links (
                source_id, source_ref_key, capture_id, timeline_id, link_kind, observed_at, created_at
             ) VALUES (?1, ?2, ?3, ?4, 'work_memory', ?5, ?6)",
            params![
                source_id,
                ref_key,
                capture_id,
                timeline_id,
                context.observed_at,
                now
            ],
        )?;
    }
    let content = clip_text(&semantic_view_content(view), DATA_TEXT_MAX_CHARS);
    let observed_at = view.latest_observed_at.unwrap_or(context.observed_at);
    let period = weekly_period_tag(observed_at);
    let mut structured = semantic_view_to_json(view.clone());
    attach_period_tag(&mut structured, &period);
    let content_hash = hash_text(&format!("{content}\n{structured}"));
    let previous_hash = conn
        .query_row(
            "SELECT content_hash FROM data_snapshots
             WHERE source_id = ?1 AND period_key = ?2 LIMIT 1",
            params![source_id, &period.key],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    let mut provenance = json!({
        "source": "timeline",
        "observed_at_is_lower_bound": true,
        "semantic_subject": view.subject,
        "semantic_identity": view.identity,
        "semantic_scope": scope,
        "generation_version": DATA_MEMORY_VERSION,
    });
    attach_period_tag(&mut provenance, &period);
    conn.execute(
        "INSERT INTO data_snapshots (
            source_id, collected_at, observed_at, period_granularity, period_key,
            period_start_at, period_end_at, collector, content_text, structured_data,
            content_hash, freshness_ttl_seconds, provenance, source_capture_ids,
            source_timeline_ids, status, created_at
         ) VALUES (?1, ?2, ?2, ?3, ?4, ?5, ?6, 'memory_extract', ?7, ?8, ?9, 0,
                   ?10, ?11, ?12, 'success', ?13)
         ON CONFLICT(source_id, period_key) DO UPDATE SET
            collected_at = excluded.collected_at,
            observed_at = excluded.observed_at,
            collector = excluded.collector,
            content_text = excluded.content_text,
            structured_data = excluded.structured_data,
            content_hash = excluded.content_hash,
            freshness_ttl_seconds = excluded.freshness_ttl_seconds,
            provenance = excluded.provenance,
            source_capture_ids = excluded.source_capture_ids,
            source_timeline_ids = excluded.source_timeline_ids,
            status = excluded.status,
            created_at = excluded.created_at
         WHERE excluded.observed_at >= COALESCE(data_snapshots.observed_at, data_snapshots.collected_at)",
        params![
            source_id,
            observed_at,
            DATA_PERIOD_GRANULARITY,
            &period.key,
            period.start_at,
            period.end_at,
            content,
            structured.to_string(),
            content_hash,
            provenance.to_string(),
            serde_json::to_string(&context.capture_ids)?,
            serde_json::to_string(&vec![timeline_id])?,
            now,
        ],
    )?;
    let current_hash = conn.query_row(
        "SELECT content_hash FROM data_snapshots
         WHERE source_id = ?1 AND period_key = ?2 LIMIT 1",
        params![source_id, &period.key],
        |row| row.get::<_, String>(0),
    )?;
    let snapshot_changed = previous_hash.as_deref() != Some(current_hash.as_str());
    conn.execute(
        "UPDATE data_sources SET last_collected_at = MAX(COALESCE(last_collected_at, 0), ?2),
                last_success_at = MAX(COALESCE(last_success_at, 0), ?2), updated_at = ?3
         WHERE id = ?1",
        params![
            source_id,
            view.latest_observed_at.unwrap_or(context.observed_at),
            now
        ],
    )?;
    Ok((!existed, snapshot_changed))
}

fn load_timeline_data_context(
    conn: &Connection,
    candidate: &CaptureCandidate,
    timeline_id: i64,
) -> Result<TimelineDataContext, StorageError> {
    let timeline_text = [
        candidate.timeline_summary.as_deref(),
        candidate.timeline_overview.as_deref(),
        candidate.timeline_details.as_deref(),
    ]
    .into_iter()
    .flatten()
    .filter(|part| !part.trim().is_empty())
    .collect::<Vec<_>>()
    .join("\n");
    let timeline_observed_at = candidate.timeline_updated_at_ms.unwrap_or(candidate.ts);
    let mut statements = metric_statements(&timeline_text, timeline_observed_at);
    let mut evidence_parts = vec![timeline_text.clone()];
    let mut observed_at = if statements.is_empty() {
        0
    } else {
        timeline_observed_at
    };
    let mut capture_ids = Vec::new();
    let mut source_urls = Vec::new();
    let mut stmt = conn.prepare(
        "SELECT c.id, c.ts, COALESCE(c.ax_text, ''), COALESCE(c.ocr_text, ''),
                COALESCE(c.input_text, ''), COALESCE(c.audio_text, ''), c.url
         FROM captures c
         WHERE c.is_sensitive = 0
           AND (c.timeline_id = ?1 OR c.id = (
                SELECT capture_id FROM timelines WHERE id = ?1
           ))
         ORDER BY c.ts ASC, c.id ASC",
    )?;
    let rows = stmt.query_map([timeline_id], |row| {
        let text = [
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
        ]
        .into_iter()
        .filter(|part| !part.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n");
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            text,
            row.get::<_, Option<String>>(6)?,
        ))
    })?;
    for row in rows {
        let (capture_id, capture_ts, text, capture_url) = row?;
        capture_ids.push(capture_id);
        if let Some(url) = capture_url
            .as_deref()
            .and_then(canonical_data_url)
            .filter(|url| !url.is_empty() && !source_urls.contains(url))
        {
            source_urls.push(url);
        }
        if !text.trim().is_empty() {
            evidence_parts.push(text.clone());
        }
        let mut capture_statements = metric_statements(&text, capture_ts);
        if !capture_statements.is_empty() {
            observed_at = observed_at.max(capture_ts);
            statements.append(&mut capture_statements);
        }
    }
    capture_ids.sort_unstable();
    capture_ids.dedup();
    statements.truncate(80);
    let model_fact_contract = conn
        .query_row(
            "SELECT contract_version FROM timeline_data_fact_runs WHERE timeline_id = ?1",
            [timeline_id],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    let mut model_facts = Vec::new();
    if model_fact_contract.is_some() {
        let mut fact_stmt = conn.prepare(
            "SELECT title, subject, action, target_context, dimension, metric, value, unit,
                    statement, evidence_quote, confidence, observed_at
             FROM timeline_data_facts
             WHERE timeline_id = ?1
             ORDER BY id ASC",
        )?;
        let rows = fact_stmt.query_map([timeline_id], |row| {
            Ok(ModelDataFact {
                title: row.get(0)?,
                subject: row.get(1)?,
                action: row.get(2)?,
                target_context: row.get(3)?,
                dimension: row.get(4)?,
                metric: row.get(5)?,
                value: row.get(6)?,
                unit: row.get(7)?,
                statement: row.get(8)?,
                evidence_quote: row.get(9)?,
                confidence: row.get(10)?,
                observed_at: row.get(11)?,
            })
        })?;
        for row in rows {
            model_facts.push(row?);
        }
    }
    Ok(TimelineDataContext {
        capture_ids,
        source_urls,
        observed_at: observed_at.max(timeline_observed_at),
        metric_statements: statements,
        model_fact_contract,
        model_facts,
        evidence_text: evidence_parts.join("\n"),
    })
}

fn is_presentable_data_source(source: &DataSourceRecord) -> bool {
    let Some(snapshot) = source.latest_snapshot.as_ref() else {
        return false;
    };
    snapshot
        .structured_data
        .get("metric_rows")
        .and_then(Value::as_array)
        .is_some_and(|rows| !rows.is_empty())
}

/// FTS5 预筛：返回 data_snapshots_fts 命中快照对应的 source_id 集合。
///
/// 返回 `None` 表示 FTS 不可用或候选不可靠（表缺失/查询失败/空命中/被上限截断），
/// 调用方应回退原有全量过滤路径。
fn data_snapshot_fts_source_ids(conn: &Connection, query: &str) -> Option<HashSet<i64>> {
    let terms = split_query_terms(query);
    let fts_query = build_fts_or_query(&terms)?;
    let snapshot_ids: Vec<i64> = conn
        .query_row(
            "SELECT (SELECT GROUP_CONCAT(rowid) FROM data_snapshots_fts WHERE data_snapshots_fts MATCH ?1)",
            params![fts_query],
            |row| row.get::<_, Option<String>>(0),
        )
        .ok()
        .flatten()
        .map(|joined| {
            joined
                .split(',')
                .filter_map(|part| part.trim().parse::<i64>().ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if snapshot_ids.is_empty() || snapshot_ids.len() >= DEFAULT_FTS_CANDIDATE_CAP {
        return None;
    }
    let placeholders = vec!["?"; snapshot_ids.len()].join(",");
    let sql = format!("SELECT DISTINCT source_id FROM data_snapshots WHERE id IN ({placeholders})");
    let mut stmt = conn.prepare(&sql).ok()?;
    let binds: Vec<&dyn rusqlite::ToSql> = snapshot_ids
        .iter()
        .map(|id| id as &dyn rusqlite::ToSql)
        .collect();
    let ids = stmt
        .query_map(binds.as_slice(), |row| row.get::<_, i64>(0))
        .ok()?
        .collect::<Result<HashSet<_>, _>>()
        .ok()?;
    Some(ids)
}

/// 仅校验数据源自身字段（标题/URL/应用名）是否包含关键词，
/// 用于 FTS 预筛时保留未被快照索引覆盖但 source 级字段命中的候选。
fn data_source_base_fields_match(source: &DataSourceRecord, query: &str) -> bool {
    let query = query.trim().to_lowercase();
    if query.is_empty() {
        return true;
    }
    format!(
        "{}\n{}\n{}",
        source.title,
        source.source_url.as_deref().unwrap_or_default(),
        source.source_app_name.as_deref().unwrap_or_default()
    )
    .to_lowercase()
    .contains(&query)
}

fn data_source_matches_query(source: &DataSourceRecord, query: &str) -> bool {
    let query = query.trim().to_lowercase();
    if query.is_empty() {
        return true;
    }
    let snapshot_text = source
        .latest_snapshot
        .as_ref()
        .map(|snapshot| format!("{}\n{}", snapshot.content_text, snapshot.structured_data))
        .unwrap_or_default();
    format!(
        "{}\n{}\n{}\n{}",
        source.title,
        source.source_url.as_deref().unwrap_or_default(),
        source.source_app_name.as_deref().unwrap_or_default(),
        snapshot_text
    )
    .to_lowercase()
    .contains(&query)
}

fn semantic_views_for_timeline_context(
    context: &TimelineDataContext,
    semantic_context: &str,
) -> Vec<SemanticDataView> {
    if let Some(contract) = context.model_fact_contract.as_deref() {
        let views =
            semantic_views_from_model_facts(&context.model_facts, &context.evidence_text, contract);
        if !views.is_empty() || contract == CURRENT_TIMELINE_DATA_FACT_VERSION {
            return views;
        }
        // v1/v2 已经产生过历史数据，继续兼容旧回退行为；v3 起由 Sidecar
        // 聚焦补提炼负责恢复遗漏，零事实也不得再让 Rust 猜出另一套宽泛语义。
    }
    semantic_views_from_statements(&context.metric_statements, semantic_context)
}

fn semantic_views_from_model_facts(
    facts: &[ModelDataFact],
    evidence_text: &str,
    fact_contract: &str,
) -> Vec<SemanticDataView> {
    let normalized_source = normalize_evidence_text(evidence_text);
    let mut views: Vec<SemanticDataView> = Vec::new();
    for fact in facts.iter().take(80) {
        if !model_data_fact_is_valid(fact, &normalized_source) {
            continue;
        }
        let identity = [
            fact.subject.as_str(),
            fact.action.as_str(),
            fact.target_context.as_str(),
            fact.metric.as_str(),
        ]
        .into_iter()
        .map(normalize_identity_text)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(":");
        if identity.is_empty() {
            continue;
        }
        let value = if fact.unit.trim().is_empty() || fact.value.trim().ends_with(fact.unit.trim())
        {
            fact.value.trim().to_string()
        } else {
            format!("{}{}", fact.value.trim(), fact.unit.trim())
        };
        let row = SemanticMetricRow {
            dimension: fact.dimension.clone(),
            metric: fact.metric.clone(),
            value,
            note: String::new(),
            statement: fact.statement.clone(),
            observed_at: fact.observed_at,
        };
        let accepted_statement = json!({
            "statement": clip_text(&fact.statement, 500),
            "evidence_quote": clip_text(&fact.evidence_quote, 500),
            "fact_contract": fact_contract,
            "action": fact.action,
            "target_context": fact.target_context,
            "confidence": fact.confidence,
            "observed_at": fact.observed_at,
        });
        if let Some(existing) = views.iter_mut().find(|view| view.identity == identity) {
            merge_semantic_rows(&mut existing.rows, vec![row]);
            if !existing
                .statements
                .iter()
                .any(|item| item == &accepted_statement)
            {
                existing.statements.push(accepted_statement);
            }
            existing.latest_observed_at = existing.latest_observed_at.max(fact.observed_at);
            if fact.observed_at >= existing.latest_observed_at {
                existing.title = fact.title.clone();
                existing.summary = fact.statement.clone();
            }
        } else {
            views.push(SemanticDataView {
                title: fact.title.clone(),
                subject: fact.subject.clone(),
                identity,
                summary: fact.statement.clone(),
                rows: vec![row],
                statements: vec![accepted_statement],
                latest_observed_at: fact.observed_at,
            });
        }
    }
    for view in &mut views {
        view.title = clip_text(&view.title, 120);
        view.summary = clip_text(&view.summary, 500);
        view.rows.truncate(120);
        view.statements.truncate(80);
    }
    views.sort_by(|left, right| {
        right
            .latest_observed_at
            .cmp(&left.latest_observed_at)
            .then_with(|| left.title.cmp(&right.title))
    });
    views
}

fn model_data_fact_is_valid(fact: &ModelDataFact, normalized_source: &str) -> bool {
    let required = [
        fact.title.as_str(),
        fact.subject.as_str(),
        fact.metric.as_str(),
        fact.value.as_str(),
        fact.statement.as_str(),
        fact.evidence_quote.as_str(),
    ];
    if required.iter().any(|value| value.trim().is_empty())
        || !matches!(fact.confidence.as_str(), "low" | "medium" | "high")
        || fact.title.chars().count() > 120
        || fact.subject.chars().count() > 80
        || fact.metric.chars().count() > 60
        || fact.value.chars().count() > 40
        || fact.unit.chars().count() > 24
        || fact.statement.chars().count() > 500
        || fact.evidence_quote.chars().count() > 500
    {
        return false;
    }
    if generic_model_fact_anchor(&fact.subject)
        && !specific_model_fact_context(&fact.target_context)
    {
        return false;
    }
    if generic_model_execution_context(&fact.target_context)
        && (generic_model_fact_anchor(&fact.subject)
            || model_fact_subject_is_value_like(&fact.subject, &fact.value))
    {
        return false;
    }
    let evidence = normalize_evidence_text(&fact.evidence_quote);
    let subject = normalize_evidence_text(&fact.subject);
    let statement = normalize_evidence_text(&fact.statement);
    let value = normalize_evidence_text(&fact.value);
    let unit = normalize_evidence_text(&fact.unit);
    let dimension = normalize_evidence_text(&fact.dimension);
    if evidence.is_empty() || !normalized_source.contains(&evidence) {
        return false;
    }
    // 防幻觉底线：值以及 subject/metric/target_context 中至少一个
    // 语义锚点必须能回证到 evidence。逐字命中失败时，允许数字
    // token 全部命中（容忍 "87%+" vs "87%" 这类格式差异）。
    // title/statement 是否逐字复现 subject/metric/value 属于展示结构问题，
    // 不再作为拒绝理由（与 sidecar _validated_data_facts 口径一致）。
    let metric = normalize_evidence_text(&fact.metric);
    let target_context = normalize_evidence_text(&fact.target_context);
    let subject_ok = evidence.contains(&subject) || numeric_tokens_hit(&subject, &evidence);
    let semantic_anchor_ok = subject_ok
        || (!metric.is_empty() && evidence.contains(&metric))
        || (!target_context.is_empty() && evidence.contains(&target_context));
    let value_ok = evidence.contains(&value) || numeric_tokens_hit(&value, &evidence);
    let unit_ok = unit.is_empty() || evidence.contains(&unit);
    let dimension_ok =
        dimension.is_empty() || evidence.contains(&dimension) || statement.contains(&dimension);
    semantic_anchor_ok && value_ok && unit_ok && dimension_ok
}

fn generic_model_fact_anchor(value: &str) -> bool {
    matches!(
        normalize_identity_text(value).as_str(),
        "duration"
            | "aspectratio"
            | "width"
            | "height"
            | "size"
            | "value"
            | "参数"
            | "生成参数"
            | "请求参数"
            | "配置参数"
            | "已用时"
            | "耗时"
            | "总耗时"
            | "任务耗时"
            | "任务总耗时"
            | "整个任务"
            | "本次任务"
            | "该任务"
    )
}

fn specific_model_fact_context(value: &str) -> bool {
    let normalized = normalize_identity_text(value);
    normalized.chars().count() >= 6
        && !matches!(
            normalized.as_str(),
            "视频参数配置"
                | "生成参数配置"
                | "任务处理过程"
                | "api接口调用"
                | "接口调用"
                | "当前任务"
                | "本次任务"
                | "整个任务"
        )
        && !generic_model_execution_context(value)
}

fn generic_model_execution_context(value: &str) -> bool {
    let normalized = normalize_identity_text(value);
    [
        "参数配置",
        "生成控制",
        "过程监控",
        "接口调用",
        "任务处理过程",
    ]
    .iter()
    .any(|suffix| normalized.ends_with(suffix))
}

fn model_fact_subject_is_value_like(subject: &str, value: &str) -> bool {
    let subject = normalize_evidence_text(subject);
    let value = normalize_evidence_text(value);
    let Some(value_start) = (!value.is_empty()).then(|| subject.find(&value)).flatten() else {
        return false;
    };
    let value_end = value_start + value.len();
    let residue = format!("{}{}", &subject[..value_start], &subject[value_end..]);
    residue.chars().filter(|ch| ch.is_alphabetic()).count() <= 4
}

/// 提取字符串中的数字 token（以数字开头，后跟 0-9 : . %），与 sidecar 的
/// `[0-9][0-9:.%]*` 正则口径保持一致。命名避开下方指标语句解析用的
/// `numeric_tokens`（返回 NumericToken 结构体，职责不同）。
fn fact_numeric_tokens(value: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    for ch in value.chars() {
        if ch.is_ascii_digit() {
            current.push(ch);
        } else if !current.is_empty() && matches!(ch, '.' | ':' | '%') {
            current.push(ch);
        } else if !current.is_empty() {
            tokens.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
}

fn numeric_tokens_hit(value: &str, evidence: &str) -> bool {
    let tokens = fact_numeric_tokens(value);
    !tokens.is_empty() && tokens.iter().all(|token| evidence.contains(token))
}

fn normalize_evidence_text(value: &str) -> String {
    value
        .chars()
        .filter(|ch| !ch.is_whitespace())
        .flat_map(char::to_lowercase)
        .map(fullwidth_punct_to_halfwidth)
        .collect()
}

/// 与 sidecar `_HALFWIDTH_SYMBOL_MAP` 保持一致：OCR 与模型输出的全角/半角
/// 标点差异不应破坏逐字回证。
fn fullwidth_punct_to_halfwidth(ch: char) -> char {
    match ch {
        '\u{FF0C}' => ',',
        '\u{3002}' => '.',
        '\u{FF1A}' => ':',
        '\u{FF1B}' => ';',
        '\u{FF01}' => '!',
        '\u{FF1F}' => '?',
        '\u{FF08}' => '(',
        '\u{FF09}' => ')',
        '\u{FF3B}' => '[',
        '\u{FF3D}' => ']',
        '\u{3010}' => '[',
        '\u{3011}' => ']',
        '\u{FF5B}' => '{',
        '\u{FF5D}' => '}',
        '\u{FF05}' => '%',
        '\u{FF0B}' => '+',
        '\u{FF0D}' => '-',
        '\u{FF1D}' => '=',
        '\u{FF1C}' => '<',
        '\u{FF1E}' => '>',
        '\u{FF04}' => '$',
        '\u{FFE5}' => '\u{00A5}',
        other => other,
    }
}

fn semantic_views_from_statements(
    statements: &[Value],
    semantic_context: &str,
) -> Vec<SemanticDataView> {
    let mut views: Vec<SemanticDataView> = Vec::new();
    for statement_value in statements.iter().take(160) {
        let statement = statement_value
            .as_str()
            .or_else(|| statement_value.get("statement").and_then(Value::as_str))
            .unwrap_or_default();
        let observed_at = statement_value.get("observed_at").and_then(Value::as_i64);
        let Some((statement_rows, _)) = semantic_statement(statement, observed_at) else {
            continue;
        };
        let subject = semantic_subject(statement, semantic_context, &statement_rows);
        if rows_require_subject(&statement_rows) && subject.display.is_empty() {
            continue;
        }
        let identity = semantic_identity_for_rows(&statement_rows, &subject.identity);
        if identity.is_empty() {
            continue;
        }
        let title = semantic_title_for_rows(&statement_rows, &subject.display);
        let accepted_statement = json!({
            "statement": clip_text(statement, 500),
            "observed_at": observed_at,
        });
        if let Some(existing) = views.iter_mut().find(|view| view.identity == identity) {
            merge_semantic_rows(&mut existing.rows, statement_rows);
            if !existing
                .statements
                .iter()
                .any(|item| item == &accepted_statement)
            {
                existing.statements.push(accepted_statement);
            }
            existing.latest_observed_at = existing.latest_observed_at.max(observed_at);
            existing.title = semantic_title_for_rows(&existing.rows, &existing.subject);
            let insight = existing
                .rows
                .iter()
                .find_map(|row| (!row.note.is_empty()).then_some(row.note.as_str()));
            existing.summary = semantic_summary(&existing.title, &existing.rows, insight);
        } else {
            views.push(SemanticDataView {
                title: title.clone(),
                subject: subject.display,
                identity,
                summary: semantic_summary(&title, &statement_rows, None),
                rows: statement_rows,
                statements: vec![accepted_statement],
                latest_observed_at: observed_at,
            });
        }
    }
    for view in &mut views {
        view.rows.truncate(120);
        view.statements.truncate(80);
        let insight = view
            .rows
            .iter()
            .find_map(|row| (!row.note.is_empty()).then_some(row.note.as_str()));
        view.title = clip_text(&semantic_title_for_rows(&view.rows, &view.subject), 80);
        view.summary = clip_text(&semantic_summary(&view.title, &view.rows, insight), 260);
    }
    // 最终质量门禁：提炼结果必须能脱离来源卡片独立说明“什么对象的什么指标、
    // 值是多少”。尤其不能让“两类合计占比”这类缺少分类母体的标题直接通过。
    views.retain(semantic_view_is_self_contained);
    views.sort_by(|left, right| {
        right
            .latest_observed_at
            .cmp(&left.latest_observed_at)
            .then_with(|| right.rows.len().cmp(&left.rows.len()))
            .then_with(|| left.title.cmp(&right.title))
    });
    views
}

fn semantic_view_is_self_contained(view: &SemanticDataView) -> bool {
    if view.rows.is_empty() || view.title.trim().is_empty() || view.summary.trim().is_empty() {
        return false;
    }
    if rows_require_subject(&view.rows) && view.subject.trim().is_empty() {
        return false;
    }

    let title_and_summary = normalize_identity_text(&format!("{} {}", view.title, view.summary));
    if !view.subject.trim().is_empty()
        && !title_and_summary.contains(&normalize_identity_text(&view.subject))
    {
        return false;
    }

    let Some(first_row) = view.rows.first() else {
        return false;
    };
    view.rows.iter().all(semantic_metric_row_is_plausible)
        && title_and_summary.contains(&normalize_identity_text(&first_row.metric))
        && title_and_summary.contains(&normalize_identity_text(&first_row.value))
}

fn semantic_metric_row_is_plausible(row: &SemanticMetricRow) -> bool {
    let metric = row.metric.trim();
    let value = row.value.trim().to_lowercase();
    let normalized_metric = normalize_identity_text(metric);
    if metric.is_empty()
        || metric.chars().count() > 36
        // 数字前的聊天动作残片不是真正的指标名。例如“我先把 QPS 调到 1
        // 看看”曾被截成“我先把qps”，继而直接污染数据标题。这里作为第二道
        // 门禁，避免未来新增解析规则时重新放过同类内容。
        || [
            "我先把",
            "我们先把",
            "先把",
            "先将",
            "请把",
            "请将",
            "帮我把",
            "帮忙把",
            "准备把",
            "准备将",
            "计划把",
            "计划将",
            "打算把",
            "打算将",
            "尝试把",
            "尝试将",
            "试着把",
            "试着将",
        ]
        .iter()
        .any(|marker| normalized_metric.starts_with(marker))
        || ["他们", "它们", "这些", "那些", "上述", "该项", "这项"]
            .iter()
            .any(|marker| normalized_metric.starts_with(marker))
        || [
            "此外",
            "还涉及",
            "提及了",
            "用户随后",
            "返回首页",
            "个人中心",
        ]
        .iter()
        .any(|marker| metric.contains(marker))
    {
        return false;
    }

    let metric_lower = metric.to_lowercase();
    if metric_lower.contains("cpu")
        && metric_lower.contains("内存")
        && ["、", "及", "和"]
            .iter()
            .any(|separator| metric_lower.contains(separator))
    {
        return false;
    }
    let is_ratio = ["占比", "比例", "率", "增幅", "降幅", "同比", "环比"]
        .iter()
        .any(|marker| metric_lower.contains(marker));
    if is_ratio
        && !["%", "％", "百分点", "倍"]
            .iter()
            .any(|unit| value.contains(unit))
        && !value_is_unit_interval(&value)
        && !value_is_colon_ratio(&value)
    {
        return false;
    }

    let is_money = [
        "成本",
        "收入",
        "营收",
        "gmv",
        "销售额",
        "利润",
        "预算",
        "金额",
        "余额",
        "资损",
    ]
    .iter()
    .any(|marker| metric_lower.contains(marker));
    if is_money
        && !value.contains("元/秒")
        && ["秒", "分钟", "小时", "gb", "tb", "mb", "kb"]
            .iter()
            .any(|unit| value.contains(unit))
    {
        return false;
    }
    if metric_lower.contains("成本") && value.ends_with('人') {
        return false;
    }
    if ["耗时", "时长", "响应时间", "等待时间"]
        .iter()
        .any(|marker| metric_lower.contains(marker))
        && !["毫秒", "秒", "分钟", "小时", "天", "倍"]
            .iter()
            .any(|unit| value.contains(unit))
    {
        return false;
    }
    true
}

fn value_is_unit_interval(value: &str) -> bool {
    value
        .trim()
        .trim_start_matches(['+', '-'])
        .parse::<f64>()
        .is_ok_and(|number| (0.0..=1.0).contains(&number))
}

fn value_is_colon_ratio(value: &str) -> bool {
    let mut parts = value.trim().split([':', '：']);
    let Some(left) = parts.next() else {
        return false;
    };
    let Some(right) = parts.next() else {
        return false;
    };
    if parts.next().is_some() {
        return false;
    }
    [left, right]
        .iter()
        .all(|part| part.parse::<f64>().is_ok_and(|number| number > 0.0))
}

fn merge_semantic_rows(target: &mut Vec<SemanticMetricRow>, rows: Vec<SemanticMetricRow>) {
    for row in rows {
        if let Some(existing) = target.iter_mut().find(|existing| {
            existing.dimension.eq_ignore_ascii_case(&row.dimension)
                && existing.metric.eq_ignore_ascii_case(&row.metric)
        }) {
            if row.observed_at >= existing.observed_at {
                *existing = row;
            }
        } else {
            target.push(row);
        }
    }
}

fn semantic_identity_for_rows(rows: &[SemanticMetricRow], subject: &str) -> String {
    let mut metrics = rows
        .iter()
        .map(|row| canonical_identity_metric(&row.metric))
        .filter(|metric| !metric.is_empty())
        .collect::<Vec<_>>();
    metrics.sort();
    metrics.dedup();
    let metric_identity = metrics.join("|");
    let subject_identity = normalize_identity_text(subject);
    if subject_identity.is_empty() {
        metric_identity
    } else {
        format!("{subject_identity}|{metric_identity}")
    }
}

fn canonical_identity_metric(metric: &str) -> String {
    let canonical = canonical_metric_label(metric);
    let mut normalized = canonical.trim();
    for prefix in [
        "本周",
        "上周",
        "前一周",
        "上一周",
        "本月",
        "上月",
        "本季度",
        "上季度",
        "今年",
        "去年",
        "当前",
        "整体",
        "平均",
        "日均",
    ] {
        if let Some(stripped) = normalized.strip_prefix(prefix) {
            normalized = stripped.trim();
            break;
        }
    }
    normalize_identity_text(normalized)
}

fn semantic_title_for_rows(rows: &[SemanticMetricRow], subject: &str) -> String {
    let metric_title = semantic_metric_title(rows);
    if subject.is_empty() || metric_title.contains(subject) {
        metric_title
    } else if rows_describe_category_distribution(rows) && subject.ends_with("分类") {
        format!("{subject}中{metric_title}")
    } else {
        format!("{subject} {metric_title}")
    }
}

fn semantic_metric_title(rows: &[SemanticMetricRow]) -> String {
    let mut metrics = rows
        .iter()
        .map(|row| canonical_metric_label(row.metric.trim()))
        .filter(|metric| !metric.is_empty())
        .collect::<Vec<_>>();
    metrics.sort();
    metrics.dedup();
    let joined = metrics.join(" ").to_lowercase();
    let has_comparison = rows
        .iter()
        .filter(|row| !row.dimension.trim().is_empty())
        .map(|row| row.dimension.trim())
        .collect::<HashSet<_>>()
        .len()
        >= 2;

    if metrics.len() == 1 {
        match canonical_identity_metric(&metrics[0]).as_str() {
            "内存" => return "内存占用".to_string(),
            "cpu" => return "CPU 使用情况".to_string(),
            "存储" => return "存储占用".to_string(),
            _ => {}
        }
    }

    if joined.contains("gpu") && joined.contains("利用率") {
        return if has_comparison {
            "GPU 利用率对比".to_string()
        } else {
            "GPU 资源利用情况".to_string()
        };
    }
    if joined.contains("cpu") && joined.contains("利用率") {
        return if has_comparison {
            "CPU 利用率对比".to_string()
        } else {
            "CPU 资源利用情况".to_string()
        };
    }
    let change_metrics = metrics
        .iter()
        .filter(|metric| is_change_metric(metric))
        .collect::<Vec<_>>();
    let primary_metrics = metrics
        .iter()
        .filter(|metric| !is_change_metric(metric))
        .collect::<Vec<_>>();
    if primary_metrics.len() == 1 && !change_metrics.is_empty() {
        let mut labels = vec![display_identity_metric(primary_metrics[0])];
        labels.extend(
            change_metrics
                .iter()
                .map(|metric| display_identity_metric(metric)),
        );
        labels.dedup();
        let combined = labels.join("与");
        if combined.chars().count() <= 48 {
            return combined;
        }
        let change = if change_metrics.iter().any(|metric| metric.contains("环比")) {
            "环比变化"
        } else if change_metrics.iter().any(|metric| metric.contains("同比")) {
            "同比变化"
        } else {
            "变化趋势"
        };
        return format!("{}与{change}", display_identity_metric(primary_metrics[0]));
    }
    if metrics.len() > 1 && metrics.iter().all(|metric| is_business_metric(metric)) {
        return "经营业绩指标".to_string();
    }
    if metrics.len() > 1 && metrics.iter().all(|metric| is_reliability_metric(metric)) {
        return "系统运行质量指标".to_string();
    }
    if metrics.len() > 1 && metrics.iter().all(|metric| is_resource_metric(metric)) {
        return "资源使用与容量指标".to_string();
    }
    if let Some(metric) = primary_metrics.first().copied().or_else(|| metrics.first()) {
        let metric = display_identity_metric(metric);
        if has_comparison {
            format!("{metric}对比")
        } else if metrics.len() == 1 {
            metric
        } else {
            let mut labels = metrics
                .iter()
                .take(3)
                .map(|value| display_identity_metric(value))
                .collect::<Vec<_>>();
            labels.dedup();
            let combined = labels.join("与");
            if combined.chars().count() <= 36 {
                combined
            } else {
                format!("{metric}等指标")
            }
        }
    } else {
        "数据指标概况".to_string()
    }
}

fn semantic_subject(
    statement: &str,
    semantic_context: &str,
    rows: &[SemanticMetricRow],
) -> SemanticSubject {
    let statement_lower = statement.to_lowercase();
    let normalized_statement = normalize_identity_text(statement);
    let requires_subject = rows_require_subject(rows);

    // 成本、收益等结果型指标的业务对象经常位于动作短语中，而不是紧挨着
    // 数值或“成本”二字。例如“生服模特库在电商 AIGC 中的复用……节省
    // 6.28 万成本”。必须先恢复完整动作对象，再处理 AIGC 这类宽泛主题，
    // 否则标题会退化成没有独立解释能力的“AIGC 成本”。
    if let Some(outcome_subject) = explicit_outcome_subject(statement, rows) {
        return SemanticSubject {
            display: outcome_subject.clone(),
            identity: outcome_subject,
        };
    }

    let is_application_resource = !rows.is_empty()
        && rows
            .iter()
            .all(|row| is_bare_application_resource_metric(&row.metric))
        && [
            "观察到",
            "系统提示",
            "资源占用",
            "用量高",
            "占用",
            "使用量",
            "负载",
            "压力",
        ]
        .iter()
        .any(|marker| statement_lower.contains(marker));
    if is_application_resource {
        if let Some(application) = semantic_context_value(semantic_context, "application:") {
            let application = clip_text(application, 48);
            return SemanticSubject {
                display: application.clone(),
                identity: application,
            };
        }
    }

    if let Some(explicit_subject) = explicit_named_plan_subject(statement) {
        return SemanticSubject {
            display: explicit_subject.clone(),
            identity: explicit_subject,
        };
    }

    if let Some(explicit_subject) = explicit_product_subject(statement) {
        return SemanticSubject {
            display: explicit_subject.clone(),
            identity: explicit_subject,
        };
    }

    if let Some(previous_subject) = semantic_context_value(semantic_context, "previous_subject:")
        .and_then(reliable_subject_label)
        .filter(|subject| !subject_matches_document_window(subject, semantic_context))
        .filter(|subject| normalized_statement.contains(&normalize_identity_text(subject.as_str())))
    {
        let normalized_subject = normalize_identity_text(&previous_subject);
        let identity = if !normalized_subject.is_empty() {
            previous_subject.clone()
        } else {
            String::new()
        };
        return SemanticSubject {
            display: previous_subject,
            identity,
        };
    }

    if let Some(category_subject) = semantic_category_subject(statement, semantic_context, rows) {
        return SemanticSubject {
            display: category_subject.clone(),
            identity: category_subject,
        };
    }

    // 只有当前事实本身明确提到 AIGC 时才使用该主题，避免旧主题通过
    // previous_subject 污染 OfoxAI、Doro AI 等无关页面的数据。
    let raw_context_mentions_aigc = semantic_context.lines().any(|line| {
        ![
            "window_title:",
            "timeline_topic:",
            "application:",
            "previous_subject:",
        ]
        .iter()
        .any(|prefix| line.trim().starts_with(prefix))
            && line.to_lowercase().contains("aigc")
    });
    if statement_lower.contains("aigc") {
        let (display, explicitly_named) = if statement_lower.contains("垂类场景") {
            ("AIGC 垂类场景", true)
        } else if statement_lower.contains("推理成本") && statement_lower.contains("视频") {
            ("AIGC 视频生成", statement_lower.contains("aigc"))
        } else {
            ("AIGC", statement_lower.contains("aigc"))
        };
        return SemanticSubject {
            display: display.to_string(),
            identity: if explicitly_named {
                display.to_string()
            } else {
                String::new()
            },
        };
    }

    let timeline_title = stable_timeline_topic(semantic_context);
    if raw_context_mentions_aigc && timeline_title.is_none() {
        let display = if statement_lower.contains("垂类场景") {
            "AIGC 垂类场景"
        } else if statement_lower.contains("推理成本") && statement_lower.contains("视频") {
            "AIGC 视频生成"
        } else {
            "AIGC"
        };
        return SemanticSubject {
            display: display.to_string(),
            identity: if statement_lower.contains("垂类场景") {
                display.to_string()
            } else {
                String::new()
            },
        };
    }

    if let Some(explicit_subject) = explicit_metric_subject(statement, rows) {
        return SemanticSubject {
            display: explicit_subject.clone(),
            identity: explicit_subject,
        };
    }

    // 指标已经明确包含对象时，不再把页面或文档名称硬塞进标题。来源名称仍保留在
    // data_sources/source_window_title、关联关系和 provenance 中用于追溯与召回。
    if !requires_subject {
        return SemanticSubject::default();
    }

    // “耗时 5 分钟”无法说明究竟是哪一步或哪项任务。即使时间线有一个宽泛主题，
    // 也不能把该主题硬拼成业务对象；只有“同步请求耗时”等原文明示对象的指标才保留。
    if rows.iter().all(|row| {
        matches!(
            canonical_identity_metric(&row.metric).as_str(),
            "耗时" | "总耗时" | "时长" | "任务耗时" | "任务总耗时"
        )
    }) {
        return SemanticSubject::default();
    }

    // 非文档型页面（产品台、报表页等）的稳定页面名可以作为业务对象；云文档、
    // 周报、月会等标题只是来源载体，不能进入可复用标题和描述。
    if !semantic_context_has_document_window(semantic_context) {
        if let Some(source_title) = stable_window_title(semantic_context) {
            return SemanticSubject {
                display: source_title.clone(),
                identity: source_title,
            };
        }
    }

    // 稳定产品页可为裸属性提供对象，但宽泛的时间线摘要不可以。否则同一时间线里
    // 单独出现的“单价低”“准确率 70%”会被错误挂到整段工作摘要上。
    if rows
        .iter()
        .all(|row| metric_needs_inline_subject(&row.metric))
    {
        return SemanticSubject::default();
    }

    if !semantic_context_has_document_window(semantic_context) {
        if let Some(source_title) = timeline_title {
            return SemanticSubject {
                display: contextual_subject_title(&source_title, rows),
                identity: String::new(),
            };
        }
    }

    SemanticSubject::default()
}

fn semantic_context_has_document_window(semantic_context: &str) -> bool {
    let Some(window_title) = semantic_context_value(semantic_context, "window_title:") else {
        return false;
    };
    let normalized = normalize_identity_text(window_title);
    window_title.contains("云文档")
        || window_title.contains("在线文档")
        || matches!(normalized.as_str(), "docs" | "googledocs")
}

fn subject_matches_document_window(subject: &str, semantic_context: &str) -> bool {
    let Some(window_title) = stable_window_title(semantic_context) else {
        return false;
    };
    let subject = normalize_identity_text(subject);
    let window_title = normalize_identity_text(&window_title);
    !subject.is_empty()
        && !window_title.is_empty()
        && (subject.starts_with(&window_title) || window_title.starts_with(&subject))
}

fn semantic_category_subject(
    statement: &str,
    semantic_context: &str,
    rows: &[SemanticMetricRow],
) -> Option<String> {
    if !rows_describe_category_distribution(rows) {
        return None;
    }
    let context = format!("{statement}\n{semantic_context}").to_lowercase();
    let metrics = rows
        .iter()
        .map(|row| normalize_identity_text(&row.metric))
        .collect::<Vec<_>>()
        .join("|");
    let describes_generation_and_understanding =
        metrics.contains("生成") && (metrics.contains("理解") || metrics.contains("音视频"));
    if context.contains("ai") && describes_generation_and_understanding {
        let subject = if context.contains("建设资产") {
            "AI 建设资产分类"
        } else if context.contains("能力") {
            "AI 能力分类"
        } else {
            "AI 资产分类"
        };
        return Some(subject.to_string());
    }
    None
}

fn stable_window_title(semantic_context: &str) -> Option<String> {
    semantic_context_value(semantic_context, "window_title:").and_then(reliable_source_title)
}

fn stable_timeline_topic(semantic_context: &str) -> Option<String> {
    semantic_context_value(semantic_context, "timeline_topic:").and_then(reliable_topic_title)
}

fn explicit_product_subject(statement: &str) -> Option<String> {
    // “智能风控体系RiskOS”这类写法中，产品名位于体系/系统/平台等载体词之后。
    // 优先恢复这个显式对象，避免把相邻的 HC -300、HITL 等数值片段或方法论
    // 缩写误当成数据主题。
    let mut ascii_span_start = None;
    for (index, ch) in statement
        .char_indices()
        .chain(std::iter::once((statement.len(), ' ')))
    {
        if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_') {
            ascii_span_start.get_or_insert(index);
            continue;
        }
        let Some(start) = ascii_span_start.take() else {
            continue;
        };
        let candidate = &statement[start..index];
        if candidate.chars().count() < 4
            || !candidate
                .chars()
                .any(|candidate_ch| candidate_ch.is_ascii_alphabetic())
        {
            continue;
        }
        let prefix = statement[..start].trim_end_matches(|prefix_ch: char| {
            prefix_ch.is_whitespace() || "，,。；;：:（）()【】[]".contains(prefix_ch)
        });
        let suffix = statement[index..].trim_start_matches(|suffix_ch: char| {
            suffix_ch.is_whitespace() || "，,。；;：:（）()【】[]".contains(suffix_ch)
        });
        if ["体系", "系统", "平台", "项目", "产品", "服务"]
            .iter()
            .any(|marker| prefix.ends_with(marker) || suffix.starts_with(marker))
        {
            return Some(candidate.to_string());
        }
    }

    for suffix in ["平台", "系统"] {
        let Some(position) = statement.find(suffix) else {
            continue;
        };
        let prefix = statement[..position].trim();
        let candidate = ["切换至", "切换到", "进入", "使用", "打开", "在"]
            .iter()
            .filter_map(|marker| {
                prefix
                    .rfind(marker)
                    .map(|index| (index, &prefix[index + marker.len()..]))
            })
            .max_by_key(|(index, _)| *index)
            .map(|(_, candidate)| candidate)
            .unwrap_or(prefix)
            .trim_matches(|ch: char| ch.is_whitespace() || "，,。；;：:（）()".contains(ch));
        let normalized = normalize_identity_text(candidate);
        if candidate.chars().count() >= 3
            && candidate.chars().count() <= 24
            && candidate.chars().any(|ch| ch.is_ascii_alphabetic())
            && !["用户", "该", "这个", "当前"]
                .iter()
                .any(|generic| normalized == *generic)
        {
            return Some(candidate.to_string());
        }
    }
    None
}

fn explicit_named_plan_subject(statement: &str) -> Option<String> {
    let words = statement
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_')))
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>();
    for pair in words.windows(2) {
        let tier = pair[1].to_ascii_lowercase();
        if matches!(
            tier.as_str(),
            "standard" | "plus" | "basic" | "pro" | "premium" | "enterprise"
        ) && pair[0].chars().any(|ch| ch.is_ascii_alphabetic())
            && !pair[0].chars().all(|ch| ch.is_ascii_digit())
        {
            return Some(format!("{} {}", pair[0], pair[1]));
        }
    }
    None
}

fn explicit_metric_subject(statement: &str, rows: &[SemanticMetricRow]) -> Option<String> {
    if rows.iter().any(|row| row.metric.contains("视频推理成本")) {
        return Some("视频推理".to_string());
    }

    if rows_require_subject(rows) {
        if let Some(subject) = explicit_subject_before_generic_metric(statement, rows) {
            return Some(subject);
        }
    }

    let mut candidates = statement
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '-'))
        .filter(|candidate| candidate.chars().count() >= 4)
        .filter(|candidate| {
            candidate
                .chars()
                .next()
                .is_some_and(|ch| ch.is_ascii_alphabetic())
        })
        .filter(|candidate| {
            let uppercase_count = candidate
                .chars()
                .filter(|ch| ch.is_ascii_uppercase())
                .count();
            candidate.contains('-') || uppercase_count >= 2
        })
        .filter(|candidate| {
            ![
                "AIGC", "GPU", "CPU", "GMV", "TOKEN", "COT", "YOY", "QPS", "GPUTL", "SMACC",
                "SMACT", "SMOCC",
            ]
            .iter()
            .any(|generic| candidate.to_uppercase() == *generic)
        })
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    candidates.sort();
    candidates.dedup();
    candidates.truncate(2);
    match candidates.as_slice() {
        [] => None,
        [only] => Some(only.clone()),
        [first, second] => Some(format!("{first} 与 {second}")),
        _ => None,
    }
}

fn explicit_outcome_subject(statement: &str, rows: &[SemanticMetricRow]) -> Option<String> {
    let has_cost_outcome = rows.iter().any(|row| row.metric.contains("成本"))
        && ["节省", "节约", "省下", "减少", "降低", "下降"]
            .iter()
            .any(|marker| statement.contains(marker));
    if !has_cost_outcome {
        return None;
    }

    for (open, close) in [('（', '）'), ('(', ')')] {
        let mut remainder = statement;
        while let Some(start) = remainder.find(open) {
            let after_open = &remainder[start + open.len_utf8()..];
            let Some(end) = after_open.find(close) else {
                break;
            };
            let candidate = trim_outcome_subject_prefix(&after_open[..end]);
            if outcome_subject_is_reliable(&candidate) {
                return Some(candidate);
            }
            remainder = &after_open[end + close.len_utf8()..];
        }
    }

    let outcome_position = [
        "带来节省",
        "带来节约",
        "节省",
        "节约",
        "省下",
        "减少",
        "降低",
    ]
    .iter()
    .filter_map(|marker| statement.find(marker))
    .min()?;
    let outcome_prefix = statement[..outcome_position]
        .trim_end_matches(|ch: char| ch.is_whitespace() || "，,。；;：:".contains(ch));
    let prefix = outcome_prefix
        .rsplit(['。', '！', '？', '；', ';', '，', ',', '：', ':'])
        .next()
        .unwrap_or_default();
    let candidate = trim_outcome_subject_prefix(prefix);
    outcome_subject_is_reliable(&candidate).then_some(candidate)
}

fn trim_outcome_subject_prefix(value: &str) -> String {
    let mut candidate = value
        .trim_matches(|ch: char| ch.is_whitespace() || "，,。；;：:（）()【】[]“”\"'".contains(ch))
        .to_string();
    loop {
        let previous = candidate.clone();
        for prefix in [
            "典型场景为",
            "典型场景",
            "复用场景为",
            "复用场景",
            "例如",
            "比如",
            "其中",
            "通过",
            "如",
        ] {
            if let Some(stripped) = candidate.strip_prefix(prefix) {
                candidate = stripped
                    .trim_matches(|ch: char| ch.is_whitespace() || "：:，,".contains(ch))
                    .to_string();
                break;
            }
        }
        if candidate == previous {
            break;
        }
    }
    for suffix in ["已成功合并", "成功合并", "已完成", "完成"] {
        if let Some(stripped) = candidate.strip_suffix(suffix) {
            candidate = stripped.trim().to_string();
            break;
        }
    }
    candidate = candidate
        .replace("AIGC中", "AIGC 中")
        .replace("AIGC场景", "AIGC 场景");
    clip_text(&candidate, 64)
}

fn outcome_subject_is_reliable(candidate: &str) -> bool {
    let normalized = normalize_identity_text(candidate);
    (4..=64).contains(&candidate.chars().count())
        && ["复用", "迁移", "替换", "优化", "合并", "共享"]
            .iter()
            .any(|marker| candidate.contains(marker))
        && !["用户", "文档", "数据", "指标", "场景"]
            .iter()
            .any(|generic| normalized == *generic)
}

fn explicit_subject_before_generic_metric(
    statement: &str,
    rows: &[SemanticMetricRow],
) -> Option<String> {
    let numeric_start = numeric_tokens(statement)
        .first()
        .map(|token| token.start)
        .unwrap_or(statement.len());
    let metric_prefix = &statement[..numeric_start];
    let mut markers = rows
        .iter()
        .flat_map(|row| {
            [
                canonical_metric_label(&row.metric),
                generic_metric_family(&row.metric).unwrap_or_default(),
            ]
        })
        .filter(|marker| !marker.trim().is_empty())
        .collect::<Vec<_>>();
    markers.sort_by_key(|marker| std::cmp::Reverse(marker.len()));
    markers.dedup();
    let marker_position = markers
        .iter()
        .filter_map(|marker| {
            metric_prefix
                .rfind(marker)
                .map(|position| (position, marker.len()))
        })
        // “API余额”与“余额”可能同时命中；优先选择结束位置相同的完整指标，
        // 避免把指标内部的 API 错当成归属对象。
        .max_by_key(|(position, marker_len)| (position + marker_len, *marker_len))?;
    let mut candidate = metric_prefix[..marker_position.0]
        .rsplit(['。', '！', '？', '；', ';', '，', ',', '：', ':'])
        .next()
        .unwrap_or_default()
        .trim()
        .to_string();

    for prefix in [
        "背景显示",
        "数据显示",
        "数据表明",
        "结果显示",
        "文档显示",
        "文档记录",
        "记录显示",
        "其中",
    ] {
        if let Some(stripped) = candidate.strip_prefix(prefix) {
            candidate = stripped.trim().to_string();
        }
    }

    // 负责人、维护人等是来源上下文，不是指标归属对象。例如
    // “冯志刚主导的阿里云 paraformer ASR 单价”应保留后半段模型对象。
    if let Some(position) = candidate.rfind('的') {
        let owner_clause = &candidate[..position];
        let object_clause = candidate[position + '的'.len_utf8()..].trim();
        if ["主导", "负责", "维护", "提供", "建设", "研发", "推出"]
            .iter()
            .any(|marker| owner_clause.contains(marker))
            && object_clause.chars().count() >= 2
        {
            candidate = object_clause.to_string();
        }
    }
    candidate = candidate
        .trim_matches(|ch: char| {
            ch.is_whitespace() || "，,。；;：:（）()【】[]“”\"'的".contains(ch)
        })
        .to_string();

    let normalized = normalize_identity_text(&candidate);
    let is_context_fragment = [
        "用户",
        "他们",
        "当前",
        "其中",
        "此外",
        "最后",
        "文档",
        "数据",
        "指标",
        "整个任务",
        "本次任务",
        "该任务",
        "任务总",
    ]
    .iter()
    .any(|prefix| normalized.starts_with(prefix))
        || ["包含", "涉及", "提及", "记录", "显示", "看到", "指出"]
            .iter()
            .any(|marker| candidate.contains(marker));
    if is_context_fragment || !(2..=48).contains(&candidate.chars().count()) {
        return None;
    }
    reliable_subject_label(&candidate)
}

fn semantic_context_for_source(
    source: &DataSourceRecord,
    observed_title: Option<&str>,
    previous_subject: Option<&str>,
) -> String {
    let mut context = source.title.trim().to_string();
    if !source.title.trim().is_empty() {
        context.push_str(&format!("\ntimeline_topic:{}", source.title.trim()));
    }
    if let Some(window_title) = observed_title
        .or(source.source_window_title.as_deref())
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        context.push_str(&format!("\nwindow_title:{window_title}"));
    }
    if let Some(application) = source
        .source_app_name
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        context.push_str(&format!("\napplication:{application}"));
    }
    if let Some(previous_subject) = previous_subject
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        context.push_str(&format!("\nprevious_subject:{previous_subject}"));
    }
    context
}

fn contextual_subject_title(source_title: &str, rows: &[SemanticMetricRow]) -> String {
    if rows_describe_category_distribution(rows) {
        let suffix = "中的类别分布";
        let max_base_chars = 48usize.saturating_sub(suffix.chars().count());
        let base = source_title
            .trim()
            .chars()
            .take(max_base_chars)
            .collect::<String>();
        format!("{base}{suffix}")
    } else {
        clip_text(source_title, 48)
    }
}

fn semantic_context_value<'a>(context: &'a str, prefix: &str) -> Option<&'a str> {
    context.lines().rev().find_map(|line| {
        line.trim()
            .strip_prefix(prefix)
            .map(str::trim)
            .filter(|value| !value.is_empty())
    })
}

fn reliable_subject_label(value: &str) -> Option<String> {
    let value = value.trim();
    let normalized = normalize_identity_text(value);
    if normalized.is_empty()
        || normalized.chars().count() < 2
        || value
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_digit() || matches!(ch, '+' | '-'))
        || value.chars().any(|ch| ch == '%' || ch == '％')
        || [
            "小于",
            "大于",
            "低于",
            "高于",
            "相关核心",
            "等相关",
            "http等",
        ]
        .iter()
        .any(|fragment| value.to_lowercase().contains(fragment))
        || generic_metric_family(value).is_some()
    {
        None
    } else {
        Some(clip_text(value, 48))
    }
}

fn reliable_source_title(value: &str) -> Option<String> {
    let mut title = value.trim().to_string();
    for suffix in [
        " - Google Chrome",
        " — Google Chrome",
        " - Chrome",
        " — Chrome",
        " - Safari",
        " — Safari",
        " - 云文档",
        " — 云文档",
        " - 飞书云文档",
        " — 飞书云文档",
        "（副本）",
        " (副本)",
        " - 副本",
        " 副本",
    ] {
        if let Some(stripped) = title.strip_suffix(suffix) {
            title = stripped.trim().to_string();
        }
    }
    if title.contains('|') {
        let mut breadcrumbs = Vec::new();
        for part in title
            .split('|')
            .map(str::trim)
            .filter(|part| !part.is_empty())
        {
            let normalized_part = normalize_identity_text(part);
            if matches!(normalized_part.as_str(), "personalhome" | "home" | "首页")
                || breadcrumbs.last().is_some_and(|previous: &String| {
                    normalize_identity_text(previous) == normalized_part
                })
            {
                continue;
            }
            breadcrumbs.push(part.to_string());
        }
        if !breadcrumbs.is_empty() {
            title = breadcrumbs.join(" | ");
        }
    }
    if let Some((brand, tagline)) = title.split_once(" - ") {
        let brand = brand.trim();
        let tagline = tagline.trim();
        if (2..=32).contains(&brand.chars().count())
            && tagline.chars().count() >= 4
            && !brand.contains("文档")
        {
            title = brand.to_string();
        }
    }
    let normalized = normalize_identity_text(&title);
    let generic_titles = [
        "chatgpt",
        "kimi",
        "googlechrome",
        "chrome",
        "safari",
        "memorybread",
        "terminal",
        "iterm",
        "访达",
        "finder",
        "docs",
        "googledocs",
        "知识库",
        "新标签页",
        "无标题",
    ];
    if normalized.is_empty()
        || normalized.chars().count() < 4
        || generic_platform_scope(&normalized)
        || generic_titles
            .iter()
            .any(|candidate| normalized == *candidate)
        || generic_metric_family(&title).is_some()
    {
        None
    } else {
        Some(clip_text(&title, 48))
    }
}

fn reliable_topic_title(value: &str) -> Option<String> {
    let mut title = value.trim().trim_end_matches('…').trim().to_string();
    for prefix in [
        "用户参与了关于",
        "用户正在推进",
        "用户正在设计",
        "用户正在处理",
        "用户成功解决了",
        "记录了",
    ] {
        if let Some(stripped) = title.strip_prefix(prefix) {
            title = stripped.trim().to_string();
            break;
        }
    }
    if let Some(position) = title.find("的会议讨论") {
        title.truncate(position);
    } else if let Some(position) = title.find(['，', '。']) {
        title.truncate(position);
    }
    title = title
        .trim_matches(|ch: char| ch.is_whitespace() || "，,。；;：:".contains(ch))
        .to_string();
    let normalized = normalize_identity_text(&title);
    if normalized.is_empty()
        || normalized.chars().count() < 4
        || generic_platform_scope(&normalized)
        || generic_metric_family(&title).is_some()
    {
        None
    } else {
        Some(clip_text(&title, 48))
    }
}

fn generic_platform_scope(normalized: &str) -> bool {
    [
        "projectsgitlab",
        "gitlabprojects",
        "mediaplayer",
        "视频会议主窗口",
    ]
    .iter()
    .any(|scope| normalized.contains(scope))
}

fn rows_require_subject(rows: &[SemanticMetricRow]) -> bool {
    let all_generic = !rows.is_empty()
        && rows
            .iter()
            .all(|row| generic_metric_family(&row.metric).is_some())
        && !rows
            .iter()
            .any(|row| dimension_names_object(&row.dimension));
    all_generic
        || rows
            .iter()
            .any(|row| metric_requires_parent_scope(&row.metric))
}

fn metric_requires_parent_scope(metric: &str) -> bool {
    let normalized = normalize_identity_text(&canonical_metric_label(metric));
    let deictic = ["其中", "两者", "上述", "该类", "这类", "前两类"]
        .iter()
        .any(|marker| normalized.starts_with(marker))
        || matches!(normalized.as_str(), "两类合计占比" | "两类合计比例");
    let generic_ai_capability_pair = rows_metric_is_category_distribution(&normalized)
        && normalized.contains("生成")
        && (normalized.contains("理解") || normalized.contains("音视频"))
        && !normalized.contains("ai建设资产分类");
    deictic || generic_ai_capability_pair
}

fn rows_describe_category_distribution(rows: &[SemanticMetricRow]) -> bool {
    !rows.is_empty()
        && rows.iter().all(|row| {
            rows_metric_is_category_distribution(&normalize_identity_text(&canonical_metric_label(
                &row.metric,
            )))
        })
}

fn rows_metric_is_category_distribution(normalized_metric: &str) -> bool {
    (normalized_metric.contains("占比") || normalized_metric.contains("比例"))
        && ((normalized_metric.contains('类')
            && (normalized_metric.contains("合计")
                || normalized_metric.contains("两类")
                || normalized_metric.contains("三类")
                || normalized_metric.contains("各类")))
            || normalized_metric.contains('与')
            || normalized_metric.contains('和'))
}

fn dimension_names_object(dimension: &str) -> bool {
    let dimension = dimension.trim();
    !dimension.is_empty()
        && !matches!(
            dimension,
            "本周"
                | "上周"
                | "前一周"
                | "上一周"
                | "本月"
                | "上月"
                | "本季度"
                | "上季度"
                | "今年"
                | "去年"
                | "当前"
                | "此前"
                | "之前"
                | "昨日"
                | "今日"
                | "日峰"
                | "峰值"
                | "平均"
                | "整体"
                | "目标"
                | "基准"
                | "优化前"
                | "优化后"
        )
}

fn is_bare_application_resource_metric(metric: &str) -> bool {
    matches!(
        generic_metric_family(metric).as_deref(),
        Some("内存") | Some("cpu") | Some("存储") | Some("负载")
    ) || ["浏览器内存", "浏览器CPU", "浏览器存储", "浏览器负载"]
        .iter()
        .any(|resource| normalize_identity_text(metric) == normalize_identity_text(resource))
}

fn generic_metric_family(metric: &str) -> Option<String> {
    let identity = canonical_identity_metric(metric);
    let mut base = identity.as_str();
    for suffix in [
        "同比降幅",
        "环比降幅",
        "同比增幅",
        "环比增幅",
        "同比下降",
        "环比下降",
        "同比增长",
        "环比增长",
        "降幅",
        "增幅",
    ] {
        if let Some(stripped) = base.strip_suffix(suffix) {
            base = stripped;
            break;
        }
    }
    for prefix in [
        "单个", "每个", "单位", "总体", "综合", "平均", "整体", "当前",
    ] {
        if let Some(stripped) = base.strip_prefix(prefix) {
            base = stripped;
            break;
        }
    }
    let family = match base {
        "gmv" => "gmv",
        "收入" => "收入",
        "营收" => "营收",
        "成本" => "成本",
        "利润" => "利润",
        "毛利" => "毛利",
        "订单" | "订单数" => "订单",
        "销量" => "销量",
        "销售额" => "销售额",
        "库存" => "库存",
        "预算" => "预算",
        "金额" => "金额",
        "余额" | "api余额" => "余额",
        "单价" => "单价",
        "汇率" => "汇率",
        "占比" => "占比",
        "比例" => "比例",
        "准确率" => "准确率",
        "召回率" => "召回率",
        "成功率" => "成功率",
        "失败率" => "失败率",
        "错误率" => "错误率",
        "命中率" => "命中率",
        "完成率" => "完成率",
        "达成率" => "达成率",
        "增长率" => "增长率",
        "下降率" => "下降率",
        "利用率" => "利用率",
        "点击率" => "点击率",
        "留存率" => "留存率",
        "用户数" => "用户数",
        "客户数" => "客户数",
        "内存" | "内存占用" => "内存",
        "cpu" | "cpu占用" | "cpu使用率" => "cpu",
        "存储" | "存储占用" | "存储容量" => "存储",
        "负载" => "负载",
        "容量" => "容量",
        "用量" => "用量",
        "延迟" => "延迟",
        "耗时" | "总耗时" | "时长" | "任务耗时" | "任务总耗时" => "耗时",
        "token规模" | "token总量" | "token用量" => "token规模",
        "qps" => "qps",
        "pv" => "pv",
        "uv" => "uv",
        "dau" => "dau",
        "mau" => "mau",
        "" if [
            "同比降幅",
            "环比降幅",
            "同比增幅",
            "环比增幅",
            "同比下降",
            "环比下降",
            "同比增长",
            "环比增长",
            "降幅",
            "增幅",
        ]
        .iter()
        .any(|change| identity == *change) =>
        {
            "变化"
        }
        _ => return None,
    };
    Some(family.to_string())
}

fn metric_needs_inline_subject(metric: &str) -> bool {
    matches!(
        generic_metric_family(metric).as_deref(),
        Some(
            "成本"
                | "收入"
                | "营收"
                | "利润"
                | "毛利"
                | "销量"
                | "销售额"
                | "库存"
                | "预算"
                | "金额"
                | "余额"
                | "单价"
                | "汇率"
                | "占比"
                | "比例"
                | "准确率"
                | "召回率"
                | "成功率"
                | "失败率"
                | "错误率"
                | "命中率"
                | "完成率"
                | "达成率"
                | "增长率"
                | "下降率"
                | "利用率"
                | "点击率"
                | "留存率"
                | "延迟"
                | "变化"
        )
    )
}

fn display_identity_metric(metric: &str) -> String {
    let normalized = metric.trim();
    for prefix in [
        "本周",
        "上周",
        "前一周",
        "上一周",
        "本月",
        "上月",
        "本季度",
        "上季度",
        "今年",
        "去年",
        "当前",
        "整体",
        "平均",
        "日均",
    ] {
        if let Some(stripped) = normalized.strip_prefix(prefix) {
            let stripped = stripped.trim();
            if !stripped.is_empty() {
                return stripped.to_string();
            }
        }
    }
    normalized.to_string()
}

fn is_change_metric(metric: &str) -> bool {
    ["同比", "环比", "增幅", "降幅", "增长率", "下降率"]
        .iter()
        .any(|marker| metric.contains(marker))
}

fn is_business_metric(metric: &str) -> bool {
    [
        "收入",
        "营收",
        "GMV",
        "销售",
        "订单",
        "销量",
        "客单价",
        "成本",
        "利润",
        "毛利",
    ]
    .iter()
    .any(|marker| metric.to_lowercase().contains(&marker.to_lowercase()))
        || is_change_metric(metric)
}

fn is_reliability_metric(metric: &str) -> bool {
    [
        "错误",
        "成功",
        "失败",
        "请求",
        "工单",
        "告警",
        "延迟",
        "耗时",
        "响应时间",
        "QPS",
    ]
    .iter()
    .any(|marker| metric.to_lowercase().contains(&marker.to_lowercase()))
}

fn is_resource_metric(metric: &str) -> bool {
    [
        "CPU",
        "GPU",
        "内存",
        "存储",
        "容量",
        "用量",
        "负载",
        "利用率",
        "SMACC",
        "SMACT",
        "SMOCC",
        "GPUTL",
    ]
    .iter()
    .any(|marker| metric.to_lowercase().contains(&marker.to_lowercase()))
}

fn semantic_summary(title: &str, rows: &[SemanticMetricRow], insight: Option<&str>) -> String {
    let row_summary = summarize_rows(rows, insight);
    if row_summary.is_empty() {
        title.to_string()
    } else {
        format!("{title}：{row_summary}")
    }
}

fn semantic_view_content(view: &SemanticDataView) -> String {
    view.statements
        .iter()
        .filter_map(|item| item.get("statement").and_then(Value::as_str))
        .filter(|statement| !statement.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn rejected_semantic_view_json(reason: &str) -> Value {
    json!({
        "extraction_version": DATA_MEMORY_VERSION,
        "semantic_origin": "legacy_parser",
        "title": "",
        "summary": "",
        "semantic_subject": "",
        "semantic_identity": "",
        "metric_rows": [],
        "metric_statements": [],
        "rejection_reason": reason,
    })
}

fn semantic_view_to_json(view: SemanticDataView) -> Value {
    let semantic_origin = if view.statements.iter().any(|statement| {
        statement
            .get("fact_contract")
            .and_then(Value::as_str)
            .is_some()
    }) {
        "model_structured_fact"
    } else {
        "legacy_parser"
    };
    json!({
        "extraction_version": DATA_MEMORY_VERSION,
        "semantic_origin": semantic_origin,
        "title": view.title,
        "summary": view.summary,
        "semantic_subject": view.subject,
        "semantic_identity": view.identity,
        "metric_rows": view.rows.into_iter().map(|row| json!({
            "dimension": row.dimension,
            "metric": row.metric,
            "value": row.value,
            "note": row.note,
            "statement": row.statement,
            "observed_at": row.observed_at,
        })).collect::<Vec<_>>(),
        "metric_statements": view.statements,
    })
}

fn semantic_view_for_snapshot(
    snapshot: &DataSnapshotRecord,
    semantic_context: &str,
) -> Option<Value> {
    semantic_view_for_content(
        &snapshot.content_text,
        &snapshot.structured_data,
        snapshot.observed_at,
        semantic_context,
    )
    .map(semantic_view_to_json)
}

fn semantic_view_for_content(
    content_text: &str,
    structured_data: &Value,
    observed_at: Option<i64>,
    semantic_context: &str,
) -> Option<SemanticDataView> {
    semantic_views_for_content(content_text, structured_data, observed_at, semantic_context)
        .into_iter()
        .max_by_key(|view| view.rows.len() * 100 + view.summary.chars().count().min(260))
}

fn semantic_views_for_content(
    content_text: &str,
    structured_data: &Value,
    observed_at: Option<i64>,
    semantic_context: &str,
) -> Vec<SemanticDataView> {
    if let Some(existing) = semantic_view_from_existing_v3(structured_data) {
        return vec![existing];
    }
    let mut statements = structured_data
        .get("metric_statements")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if let Some(labels) = structured_data
        .get("metric_labels")
        .and_then(Value::as_array)
    {
        statements.extend(labels.iter().cloned());
    }
    statements.extend(
        content_text
            .split(['\n', '。', '；', ';'])
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .take(160)
            .map(|statement| json!({"statement": statement, "observed_at": observed_at})),
    );
    let views = semantic_views_from_statements(&statements, semantic_context);
    if !views.is_empty() {
        return views;
    }

    semantic_view_from_tables(structured_data, observed_at)
        .into_iter()
        .collect()
}

fn semantic_view_from_existing_v3(structured: &Value) -> Option<SemanticDataView> {
    if structured.get("extraction_version").and_then(Value::as_str) != Some(DATA_MEMORY_VERSION) {
        return None;
    }
    let title = structured.get("title").and_then(Value::as_str)?.trim();
    let summary = structured.get("summary").and_then(Value::as_str)?.trim();
    let is_validated_model_fact =
        structured.get("semantic_origin").and_then(Value::as_str) == Some("model_structured_fact");
    let raw_rows = structured.get("metric_rows")?.as_array()?;
    let rows = raw_rows
        .iter()
        .filter_map(|row| {
            let metric = row.get("metric")?.as_str()?.trim();
            let value = row.get("value")?.as_str()?.trim();
            if (!(is_validated_model_fact && metric_is_subject(metric))
                && !metric_is_meaningful(metric))
                || value.is_empty()
                || !value.chars().any(|ch| ch.is_ascii_digit())
            {
                return None;
            }
            Some(SemanticMetricRow {
                dimension: row
                    .get("dimension")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_string(),
                metric: metric.to_string(),
                value: value.to_string(),
                note: row
                    .get("note")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_string(),
                statement: row
                    .get("statement")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_string(),
                observed_at: row.get("observed_at").and_then(Value::as_i64),
            })
        })
        .collect::<Vec<_>>();
    if rows.is_empty() {
        return None;
    }
    let subject = structured
        .get("semantic_subject")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    let identity = structured
        .get("semantic_identity")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .unwrap_or_else(|| semantic_identity_for_rows(&rows, &subject));
    let view = SemanticDataView {
        title: title.to_string(),
        subject,
        identity,
        summary: summary.to_string(),
        latest_observed_at: rows.iter().filter_map(|row| row.observed_at).max(),
        rows,
        statements: structured
            .get("metric_statements")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
    };
    semantic_view_is_self_contained(&view).then_some(view)
}

fn semantic_view_from_tables(
    structured: &Value,
    observed_at: Option<i64>,
) -> Option<SemanticDataView> {
    let tables = structured.get("tables")?.as_array()?;
    let mut semantic_rows = Vec::new();
    for table in tables.iter().take(20) {
        let Some(raw_rows) = table.as_array() else {
            continue;
        };
        let rows = raw_rows
            .iter()
            .filter_map(Value::as_array)
            .map(|row| {
                row.iter()
                    .map(|cell| value_as_text(cell).trim().to_string())
                    .collect::<Vec<_>>()
            })
            .filter(|row| row.iter().any(|cell| !cell.is_empty()))
            .collect::<Vec<_>>();
        if rows.is_empty() {
            continue;
        }
        let header = rows.first().filter(|row| {
            row.iter()
                .all(|cell| !cell.chars().any(|ch| ch.is_ascii_digit()))
        });
        let data_rows = if header.is_some() {
            &rows[1..]
        } else {
            &rows[..]
        };
        for row in data_rows.iter().take(200) {
            let Some(label) = row.iter().find(|cell| {
                metric_is_meaningful(cell) && !cell.chars().any(|ch| ch.is_ascii_digit())
            }) else {
                continue;
            };
            for (index, value) in row.iter().enumerate() {
                if !value.chars().any(|ch| ch.is_ascii_digit()) {
                    continue;
                }
                let metric = header
                    .and_then(|cells| cells.get(index))
                    .filter(|cell| metric_is_meaningful(cell))
                    .cloned()
                    .unwrap_or_else(|| label.clone());
                let dimension = if metric == *label {
                    String::new()
                } else {
                    label.clone()
                };
                semantic_rows.push(SemanticMetricRow {
                    dimension,
                    metric,
                    value: value.clone(),
                    note: String::new(),
                    statement: row.join(" / "),
                    observed_at,
                });
            }
        }
    }
    if semantic_rows.is_empty() {
        return None;
    }
    semantic_rows.truncate(120);
    let subject = String::new();
    let title = semantic_title_for_rows(&semantic_rows, &subject);
    let identity = semantic_identity_for_rows(&semantic_rows, &subject);
    let summary = semantic_summary(&title, &semantic_rows, None);
    Some(SemanticDataView {
        title,
        subject,
        identity,
        summary,
        rows: semantic_rows,
        statements: Vec::new(),
        latest_observed_at: observed_at,
    })
}

fn value_as_text(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Bool(value) => value.to_string(),
        _ => String::new(),
    }
}

fn score_data_source(
    source: DataSourceRecord,
    history: Vec<DataSnapshotRecord>,
    query: &str,
    terms: &[String],
    need_fresh: bool,
    as_of_ms: i64,
) -> DataSearchResult {
    let snapshot = source.latest_snapshot.as_ref();
    let content = snapshot
        .map(|item| item.content_text.as_str())
        .unwrap_or("");
    let structured_text = snapshot
        .map(|item| item.structured_data.to_string())
        .unwrap_or_default();
    let historical_text = history
        .iter()
        .map(|item| format!("{}\n{}", item.content_text, item.structured_data))
        .collect::<Vec<_>>()
        .join("\n");
    let content_relevance = relevance_score(
        &format!(
            "{}\n{}\n{}\n{}",
            source.title, content, structured_text, historical_text
        ),
        terms,
    );
    let identity_relevance = source_identity_score(&source, query);
    let relevance_score = content_relevance.max(identity_relevance);
    let collected_at = snapshot.map(|item| item.collected_at);
    let observed_at = snapshot
        .and_then(|item| item.observed_at)
        .or(Some(source.last_seen_at));
    let reference_time = collected_at.or(observed_at);
    let age_seconds = reference_time
        .map(|timestamp| as_of_ms.saturating_sub(timestamp) / 1000)
        .unwrap_or(i64::MAX);
    let (freshness_class, freshness_score) = freshness_for(&source.source_kind, age_seconds);
    let refresh_required = source.source_kind == "report_url"
        && source.refresh_policy != "never"
        && (collected_at.is_none() || age_seconds > REPORT_FRESH_SECONDS);
    let can_use = snapshot.is_some() && !(need_fresh && refresh_required);
    let source_score = if source.source_kind == "report_url" {
        0.95
    } else {
        0.68
    };
    // Top-K 统一按“与当前任务的相关性”排序。refresh_required 是进入
    // Top-K 后的动作状态，不能反过来把唯一可刷新的来源挤出候选。
    let final_score = relevance_score * 0.78 + freshness_score * 0.12 + source_score * 0.10;
    DataSearchResult {
        source_id: source.id,
        title: source.title,
        source_kind: source.source_kind,
        source_url: source.source_url,
        access_mode: source.access_mode,
        refresh_policy: source.refresh_policy,
        observed_at,
        collected_at,
        freshness_class: freshness_class.to_string(),
        freshness_score,
        relevance_score,
        final_score: final_score.clamp(0.0, 1.0),
        refresh_required,
        can_use,
        content_excerpt: snapshot.map(|item| clip_text(&item.content_text, 2400)),
        structured_data: snapshot.map(|item| item.structured_data.clone()),
        provenance: snapshot.map(|item| item.provenance.clone()),
        history,
    }
}

fn source_identity_score(source: &DataSourceRecord, query: &str) -> f64 {
    let normalized_title = normalize_identity_text(&source.title);
    let normalized_query = normalize_identity_text(query);
    let query_lines = query
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>();

    if let Some(source_url) = source.source_url.as_deref().and_then(canonical_data_url) {
        let query_urls = extract_http_urls(query)
            .into_iter()
            .filter_map(|url| canonical_data_url(&url));
        if query_urls.into_iter().any(|url| url == source_url) {
            return 1.0;
        }
    }

    if !normalized_title.is_empty()
        && query_lines
            .iter()
            .any(|line| normalize_identity_text(line) == normalized_title)
    {
        return 0.96;
    }

    // Skill 的步骤目标通常写成自然语言，例如“获取电商GPU信息平台的最新
    // 算力…”，而数据标题还会继续带上具体指标。稳定的数据对象前缀已经是
    // 高强度身份信号；只接受至少 8 个字符的前缀，避免“GPU”“本周”等
    // 短词把无关数据抬进 Top-K。
    if normalized_title.chars().count() >= 8 && !normalized_query.is_empty() {
        let title_prefix = normalized_title.chars().take(8).collect::<String>();
        if normalized_query.contains(&title_prefix) {
            return 0.84;
        }
    }
    0.0
}

fn normalize_identity_text(value: &str) -> String {
    value
        .chars()
        .filter(|ch| !ch.is_whitespace() && !ch.is_ascii_punctuation())
        .flat_map(char::to_lowercase)
        .collect()
}

fn semantic_source_scope(
    window_title: Option<&str>,
    app_name: Option<&str>,
    fallback: &str,
) -> String {
    let mut title = window_title.unwrap_or_default().trim().to_string();
    for suffix in [
        " - Google Chrome",
        " — Google Chrome",
        " - Chrome",
        " — Chrome",
        " - Safari",
        " — Safari",
    ] {
        if let Some(stripped) = title.strip_suffix(suffix) {
            title = stripped.trim().to_string();
            break;
        }
    }
    let normalized = normalize_identity_text(&title);
    let normalized_app = normalize_identity_text(app_name.unwrap_or_default());
    let generic_titles = [
        "chatgpt",
        "kim",
        "googlechrome",
        "chrome",
        "safari",
        "memorybread",
        "terminal",
        "iterm",
        "访达",
        "finder",
        "知识库",
    ];
    let is_generic = normalized.is_empty()
        || normalized.chars().count() < 4
        || normalized == normalized_app
        || generic_titles.iter().any(|value| normalized == *value);
    if is_generic {
        fallback.to_string()
    } else {
        format!("window:{normalized}")
    }
}

fn freshness_for(source_kind: &str, age_seconds: i64) -> (&'static str, f64) {
    if age_seconds == i64::MAX {
        return ("missing", 0.0);
    }
    if source_kind == "report_url" {
        match age_seconds {
            age if age <= REPORT_FRESH_SECONDS => ("live", 1.0),
            age if age <= 2 * 3600 => ("fresh", 0.78),
            age if age <= 24 * 3600 => ("aging", 0.46),
            _ => ("stale", 0.16),
        }
    } else {
        match age_seconds {
            age if age <= 24 * 3600 => ("fresh", 0.82),
            age if age <= 7 * 24 * 3600 => ("aging", 0.60),
            age if age <= 30 * 24 * 3600 => ("aging", 0.34),
            _ => ("stale", 0.10),
        }
    }
}

fn relevance_score(text: &str, terms: &[String]) -> f64 {
    if terms.is_empty() {
        return 0.25;
    }
    let lowered = text.to_lowercase();
    let matched = terms
        .iter()
        .filter(|term| lowered.contains(&term.to_lowercase()))
        .count();
    if matched == 0 {
        return 0.0;
    }
    (0.28 + 0.72 * matched as f64 / terms.len() as f64).min(1.0)
}

fn keyword_terms(query: &str) -> Vec<String> {
    let mut terms = query
        .split(|ch: char| {
            ch.is_whitespace() || ch.is_ascii_punctuation() || "，。；：、（）【】《》".contains(ch)
        })
        .map(str::trim)
        .filter(|value| value.chars().count() >= 2)
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    if terms.len() == 1
        && terms[0].chars().count() >= 3
        && terms[0].chars().any(|ch| !ch.is_ascii())
    {
        let chars = terms[0].chars().collect::<Vec<_>>();
        for window in chars.windows(2) {
            let term = window.iter().collect::<String>();
            if !terms.contains(&term) {
                terms.push(term);
            }
        }
    }
    terms.truncate(24);
    terms
}

fn candidate_title(candidate: &CaptureCandidate) -> &str {
    candidate
        .webpage_title
        .as_deref()
        .or(candidate.win_title.as_deref())
        .or(candidate.timeline_summary.as_deref())
        .unwrap_or("数据来源")
}

fn looks_like_data_url(url: &str, title: &str, text: &str) -> bool {
    let url_lower = url.to_lowercase();
    let title_lower = title.to_lowercase();
    let url_markers = [
        "dashboard",
        "report",
        "analytics",
        "metric",
        "grafana",
        "tableau",
        "powerbi",
        "metabase",
        "superset",
        "quickbi",
        "datastudio",
        "/bi/",
        "bi.",
        "/chart",
        "/board",
        "/monitor",
        "feishu.cn/base",
    ];
    let title_markers = [
        "报表",
        "看板",
        "仪表盘",
        "数据平台",
        "数据中心",
        "经营分析",
        "业务分析",
        "指标",
        "监控",
        "dashboard",
        "report",
        "analytics",
    ];
    url_markers.iter().any(|marker| url_lower.contains(marker))
        || title_markers
            .iter()
            .any(|marker| title_lower.contains(marker))
        || (is_concrete_data_statement(text)
            && ["sheet", "spreadsheet", "base", "table"]
                .iter()
                .any(|marker| url_lower.contains(marker)))
}

fn is_concrete_data_statement(text: &str) -> bool {
    semantic_statement(text, None).is_some()
}

#[derive(Debug, Clone)]
struct NumericToken {
    start: usize,
    end: usize,
    value: String,
    has_explicit_unit: bool,
}

fn semantic_statement(
    text: &str,
    observed_at: Option<i64>,
) -> Option<(Vec<SemanticMetricRow>, String)> {
    let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let char_count = normalized.chars().count();
    if !(4..=280).contains(&char_count) || !normalized.chars().any(|ch| ch.is_ascii_digit()) {
        return None;
    }

    let lower = normalized.to_lowercase();
    let ui_noise = [
        "comments (",
        "go to the first comment",
        "reply...",
        "upload log",
        "help center",
        "keyboard shortcuts",
        "saved to cloud",
        "type '/' for",
    ];
    if ui_noise.iter().any(|marker| lower.contains(marker))
        || statement_looks_like_navigation_noise(&lower)
        || statement_has_corrupted_numeric_shorthand(&lower)
    {
        return None;
    }
    if !statement_is_data_assertion(&lower) {
        return None;
    }
    let mut rows = Vec::new();
    let mut inherited_metric = String::new();
    let mut inherited_dimension = String::new();
    for clause in split_metric_clauses(&normalized) {
        // 维度只在同一分句内继承，避免“此前规模”污染下一分句的“单位成本”。
        inherited_dimension.clear();
        let clause = clause.trim();
        if clause.is_empty() {
            continue;
        }
        for token in numeric_tokens(clause) {
            let prefix = &clause[..token.start];
            let dimension = detect_dimension(prefix)
                .or_else(|| (!inherited_dimension.is_empty()).then(|| inherited_dimension.clone()))
                .unwrap_or_default();
            let Some(metric) = metric_for_token(clause, &token, &dimension, &inherited_metric)
            else {
                continue;
            };
            if !metric_is_meaningful(&metric) {
                continue;
            }
            if !dimension.is_empty() {
                inherited_dimension = dimension.clone();
            }
            inherited_metric = metric.clone();
            rows.push(SemanticMetricRow {
                dimension,
                metric,
                value: token.value,
                note: String::new(),
                statement: normalized.clone(),
                observed_at,
            });
        }
    }
    normalize_semantic_rows(&normalized, &mut rows);
    rows.retain(semantic_metric_row_is_plausible);
    rows.dedup_by(|left, right| {
        left.dimension.eq_ignore_ascii_case(&right.dimension)
            && left.metric.eq_ignore_ascii_case(&right.metric)
            && left.value.eq_ignore_ascii_case(&right.value)
    });
    if rows.is_empty() {
        return None;
    }
    let has_ambiguous_values = rows.iter().enumerate().any(|(index, row)| {
        rows.iter().skip(index + 1).any(|other| {
            other.dimension.eq_ignore_ascii_case(&row.dimension)
                && other.metric.eq_ignore_ascii_case(&row.metric)
                && other.value != row.value
        })
    });
    if has_ambiguous_values {
        return None;
    }
    let insight = extract_statement_insight(&normalized);
    if let (Some(first), Some(insight)) = (rows.first_mut(), insight.as_ref()) {
        first.note = insight.clone();
    }
    let summary = summarize_rows(&rows, insight.as_deref());
    (!summary.trim().is_empty()).then_some((rows, summary))
}

fn split_metric_clauses(text: &str) -> Vec<&str> {
    let mut clauses = Vec::new();
    let mut start = 0;
    let indices = text.char_indices().collect::<Vec<_>>();
    for (position, (byte_index, ch)) in indices.iter().enumerate() {
        if !matches!(ch, '，' | ',' | '；' | ';') {
            continue;
        }
        let is_number_separator = *ch == ','
            && position > 0
            && position + 1 < indices.len()
            && indices[position - 1].1.is_ascii_digit()
            && indices[position + 1].1.is_ascii_digit();
        if is_number_separator {
            continue;
        }
        clauses.push(&text[start..*byte_index]);
        start = *byte_index + ch.len_utf8();
    }
    clauses.push(&text[start..]);
    clauses
}

fn statement_looks_like_navigation_noise(lower: &str) -> bool {
    let navigation_marker_count = [
        "返回首页",
        "个人中心",
        "查看订单",
        "查看明细",
        "返回我的",
        "预约成功",
        "keyboard shortcuts",
    ]
    .iter()
    .filter(|marker| lower.contains(**marker))
    .count();
    navigation_marker_count >= 2
}

fn statement_has_corrupted_numeric_shorthand(lower: &str) -> bool {
    let chars = lower.chars().collect::<Vec<_>>();
    chars
        .windows(3)
        .any(|window| window[0].is_ascii_digit() && window[1] == 'w' && window[2].is_ascii_digit())
}

fn statement_is_data_assertion(lower: &str) -> bool {
    let assertion = lower
        .trim_start_matches(|ch: char| ch.is_whitespace() || matches!(ch, '•' | '-' | '*' | '·'));
    // 缺少比较主体的残句无法独立解释。“约为 X 的 N 倍”只有参照物、没有
    // 被比较对象，强行提炼会把 X 错写成指标主体。
    if ["约为", "约等于", "相当于"]
        .iter()
        .any(|marker| assertion.starts_with(marker))
    {
        return false;
    }

    const NEGATED_OR_HYPOTHETICAL: &[&str] = &[
        "并不是",
        "并非",
        "不是文档中的",
        "不属于",
        "而不是",
        "而非",
        "仅用于举例",
        "示例数据",
        "数字来自另一",
        "错误拼接",
        "例如",
        "比如",
        "譬如",
        "举例",
        "示例",
        "假设",
        "数据库副本验证",
        "测试夹具",
        "test fixture",
    ];
    if NEGATED_OR_HYPOTHETICAL
        .iter()
        .any(|marker| lower.contains(marker))
    {
        return false;
    }

    // 这些词表达建议、命令、禁止或待执行配置，而不是已发生/已观测的数据事实。
    const DIRECTIVE_OR_MODAL: &[&str] = &[
        "不要",
        "别把",
        "别将",
        "应当",
        "应该",
        "不应",
        "不得",
        "请将",
        "请把",
        "需将",
        "需把",
        "需要将",
        "需要把",
        "务必",
        "必须将",
        "必须把",
        "可以将",
        "可以把",
        "可将",
        "可把",
    ];
    if DIRECTIVE_OR_MODAL
        .iter()
        .any(|marker| lower.contains(marker))
    {
        return false;
    }

    // 配置动作中的数字常常是准备尝试的目标值，而不是已经发生或观测到的
    // 指标。只有句子明确带有完成态/观测态证据时才允许继续解析；这覆盖群聊、
    // 工单和会议记录里的“我先把…调到…看看”“帮我把…设为…”等通用表达。
    const CONFIGURATION_ACTIONS: &[&str] = &[
        "把",
        "将",
        "设置",
        "设为",
        "设成",
        "设定",
        "调整",
        "调到",
        "调至",
        "改为",
        "改成",
        "降到",
        "降至",
        "提高到",
        "提升到",
        "控制在",
        "限制为",
    ];
    const PENDING_ACTION_MARKERS: &[&str] = &[
        "我先",
        "我们先",
        "先把",
        "先将",
        "准备",
        "计划",
        "打算",
        "尝试",
        "试着",
        "帮我",
        "帮忙",
        "麻烦",
        "能不能",
        "是否可以",
        "要不要",
        "看看",
        "试试",
        "再看",
        "再观察",
        "稍后",
        "待会",
        "等会",
    ];
    const OBSERVED_STATE_MARKERS: &[&str] = &[
        "已经",
        "已将",
        "已把",
        "已设置",
        "已调整",
        "调整后",
        "变更后",
        "修改后",
        "当前",
        "目前",
        "现为",
        "实际",
        "实测",
        "观测",
        "监控显示",
        "结果显示",
        "稳定在",
    ];
    let has_configuration_action = CONFIGURATION_ACTIONS
        .iter()
        .any(|action| lower.contains(action));
    let is_pending_action = PENDING_ACTION_MARKERS
        .iter()
        .any(|marker| lower.contains(marker));
    let has_observed_state = OBSERVED_STATE_MARKERS
        .iter()
        .any(|marker| lower.contains(marker));
    if has_configuration_action && is_pending_action && !has_observed_state {
        return false;
    }

    let is_checklist_or_threshold = [
        "检查清单",
        "切换前检查",
        "预案",
        "验收条件",
        "准入条件",
        "阈值",
        "容量评估",
    ]
    .iter()
    .any(|marker| lower.contains(marker));
    let contains_threshold_expression = ['<', '>', '≤', '≥']
        .iter()
        .any(|marker| lower.contains(*marker))
        || ["小于", "大于", "低于", "高于", "不超过", "不少于"]
            .iter()
            .any(|marker| lower.contains(marker));
    if is_checklist_or_threshold && contains_threshold_expression && !has_observed_state {
        return false;
    }

    const ADVISORY_MARKERS: &[&str] = &["建议", "最好"];
    !ADVISORY_MARKERS.iter().any(|advisory| {
        lower.find(advisory).is_some_and(|position| {
            let remainder = &lower[position + advisory.len()..];
            CONFIGURATION_ACTIONS
                .iter()
                .any(|action| remainder.contains(action))
        })
    })
}

fn explicit_first_two_category_metric(statement: &str) -> Option<String> {
    let reference_position = statement
        .find("其中前两类")
        .or_else(|| statement.find("前两类合计"))?;
    let prefix = &statement[..reference_position];
    let list_start = ["分为", "划分为", "包括", "包含"]
        .iter()
        .filter_map(|marker| prefix.rfind(marker).map(|position| position + marker.len()))
        .max()?;
    let category_list = prefix[list_start..].replace('和', "、");
    let categories = category_list
        .split(['、', '，', ','])
        .filter_map(|item| {
            let label = item
                .split(['（', '('])
                .next()
                .unwrap_or_default()
                .trim_matches(|ch: char| {
                    ch.is_whitespace() || "，,。；;：:（）()【】[]".contains(ch)
                });
            (label.chars().count() >= 2
                && label.chars().count() <= 18
                && !label.chars().any(|ch| ch.is_ascii_digit()))
            .then(|| label.to_string())
        })
        .take(2)
        .collect::<Vec<_>>();
    match categories.as_slice() {
        [first, second] => Some(format!("{first}与{second}两类合计占比")),
        _ => None,
    }
}

fn normalize_semantic_rows(statement: &str, rows: &mut Vec<SemanticMetricRow>) {
    let first_two_categories = explicit_first_two_category_metric(statement);
    for row in rows.iter_mut() {
        if row.metric.contains("前两类合计占比") {
            if let Some(metric) = first_two_categories.as_ref() {
                row.metric = metric.clone();
            }
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && row.metric.contains("生成")
            && (row.metric.contains("理解") || row.metric.contains("音视频"))
            && !row.metric.contains('类')
        {
            if let Some(prefix) = row.metric.strip_suffix("占比") {
                row.metric = format!("{}两类合计占比", prefix.trim_end_matches("合计"));
            } else if let Some(prefix) = row.metric.strip_suffix("比例") {
                row.metric = format!("{}两类合计比例", prefix.trim_end_matches("合计"));
            }
        }
        if row.metric.contains("本地模型分析") && row.metric.contains("同步耗时") {
            row.metric = "本地模型分析同步耗时".to_string();
        }
        if row.metric == "存储"
            && ["提供", "总存储", "存储空间", "容量"]
                .iter()
                .any(|marker| statement.contains(marker))
        {
            row.metric = "存储容量".to_string();
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && statement.to_lowercase().contains("llm")
            && statement.contains("成本浪费")
        {
            row.metric = "LLM 成本浪费比例".to_string();
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && statement.contains("按订单金额")
            && statement.contains("收取")
        {
            row.metric = "订单金额收费比例".to_string();
        }
        if row.metric.contains("成本")
            && !row.value.contains('%')
            && ["节省", "节约", "省下", "减少", "降低"]
                .iter()
                .any(|marker| statement.contains(marker))
        {
            row.metric = "成本节省金额".to_string();
        }
        if statement.contains("GPU成本年化减少") {
            row.metric = "GPU 成本年化节省金额".to_string();
        }
        if statement.contains("整轮")
            && statement.contains("模型上游推理")
            && ["毫秒", "秒", "分钟", "小时"]
                .iter()
                .any(|unit| row.value.contains(unit))
        {
            if row.metric == "耗时" || row.metric == "总耗时" {
                row.metric = "整轮耗时".to_string();
            } else if row.metric.contains("消耗") || row.metric.contains("推理") {
                row.metric = "模型上游推理耗时".to_string();
            }
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && statement.contains("批次预算")
            && row.metric.contains("预算")
        {
            row.metric = "批次预算占比".to_string();
        }
        if row.metric.contains("垂类场景")
            && (row.metric.contains("效率") || row.metric.contains("增幅"))
        {
            row.dimension = "目标".to_string();
            row.metric = "优化效率增幅".to_string();
        }
        if row.value.trim().ends_with('人')
            && row.metric.contains("成本")
            && (statement.contains("降低人力成本") || statement.contains("人力成本降低"))
        {
            row.dimension = "目标".to_string();
            row.metric = "人力缩减目标".to_string();
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && row.metric.contains("成本")
            && ["压低", "降低", "下降"]
                .iter()
                .any(|marker| statement.contains(marker))
        {
            row.metric = format!("{}降幅", row.metric.trim_end_matches("降幅"));
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && row.metric.contains("成本")
            && statement.to_lowercase().contains("yoy")
        {
            row.metric = format!("{}同比降幅", row.metric.trim_end_matches("降幅"));
        }
        if (row.value.contains('%') || row.value.contains('％'))
            && statement.contains("视频推理成本")
            && statement.contains("降幅")
        {
            row.dimension = "目标".to_string();
            row.metric = "视频推理成本降幅".to_string();
        }
        if row.dimension == "目标" && row.metric.contains("目标") {
            row.dimension.clear();
        }
        if let Some(metric) = row.metric.strip_prefix("浏览器") {
            if !metric.trim().is_empty() && generic_metric_family(metric.trim()).is_some() {
                row.metric = metric.trim().to_string();
            }
        }
        if !row.dimension.is_empty() {
            if let Some(metric) = row.metric.strip_prefix(&row.dimension) {
                if !metric.trim().is_empty() && metric_is_meaningful(metric.trim()) {
                    row.metric = metric.trim().to_string();
                }
            }
        }
    }

    let budget_transition = statement.contains("输出预算")
        && statement.contains('从')
        && (statement.contains("降到") || statement.contains("降至"));
    if budget_transition {
        let mut budget_index = 0usize;
        for row in rows.iter_mut().filter(|row| row.metric.contains("预算")) {
            row.metric = "输出预算".to_string();
            row.dimension = if budget_index == 0 {
                "调整前".to_string()
            } else {
                "调整后".to_string()
            };
            budget_index += 1;
        }
    } else if statement.contains("后续重试") && statement.contains("预算") {
        for row in rows.iter_mut().filter(|row| row.metric.contains("预算")) {
            row.metric = "后续重试输出预算".to_string();
        }
    }

    if statement.contains("人审量降低比例") && rows.len() >= 2 {
        let mut ratio_index = 0usize;
        for row in rows
            .iter_mut()
            .filter(|row| row.value.contains('%') || row.value.contains('％'))
        {
            row.metric = "人审量降低比例".to_string();
            row.dimension = if ratio_index == 0 {
                "当前".to_string()
            } else {
                "目标".to_string()
            };
            ratio_index += 1;
        }
    }

    let has_previous_token_scale = rows.iter().any(|row| {
        row.metric.contains("Token 规模")
            && !row.metric.ends_with("增幅")
            && matches!(row.dimension.as_str(), "之前" | "此前")
    });
    if has_previous_token_scale {
        for row in rows.iter_mut().filter(|row| {
            row.metric.contains("Token 规模")
                && !row.metric.ends_with("增幅")
                && row.dimension.trim().is_empty()
        }) {
            row.dimension = "当前".to_string();
        }
    }
    for row in rows.iter_mut().filter(|row| {
        row.metric.contains("Token 规模")
            && row.metric.ends_with("增幅")
            && row.value.ends_with('倍')
    }) {
        row.dimension.clear();
    }

    let Some(cost_start) = statement.find("推理成本") else {
        return;
    };
    let cost_clause = &statement[cost_start..];
    let Some(from_start) = cost_clause.find('从') else {
        return;
    };
    let before_transition = &cost_clause[..from_start];
    let transition = &cost_clause[from_start + '从'.len_utf8()..];
    let Some(to_start) = transition.find("降至") else {
        return;
    };
    let before_text = transition[..to_start].trim();
    let after_text = transition[to_start + "降至".len()..].trim();
    let Some(before_token) = numeric_tokens(before_text).into_iter().next() else {
        return;
    };
    let Some(after_token) = numeric_tokens(after_text).into_iter().next() else {
        return;
    };
    let rate = numeric_tokens(before_transition)
        .into_iter()
        .rev()
        .find(|token| token.value.contains('%') || token.value.contains('％'));
    let scope = if cost_clause.contains("视频") {
        "视频推理成本"
    } else {
        "推理成本"
    };
    let unit_suffix = if after_text[after_token.end..]
        .trim_start()
        .starts_with("/秒")
    {
        "/秒"
    } else {
        ""
    };
    let after_unit = after_token
        .value
        .chars()
        .skip_while(|ch| ch.is_ascii_digit() || matches!(ch, '.' | ','))
        .collect::<String>();
    let before_value = if before_token.has_explicit_unit {
        format!("{}{}", before_token.value, unit_suffix)
    } else {
        format!("{}{}{}", before_token.value, after_unit, unit_suffix)
    };
    let after_value = format!("{}{}", after_token.value, unit_suffix);

    rows.retain(|row| !row.metric.contains("成本"));
    if let Some(rate) = rate {
        rows.push(SemanticMetricRow {
            dimension: "目标".to_string(),
            metric: format!("{scope}降幅"),
            value: rate.value,
            note: String::new(),
            statement: statement.to_string(),
            observed_at: rows.first().and_then(|row| row.observed_at),
        });
    }
    let observed_at = rows.first().and_then(|row| row.observed_at);
    rows.push(SemanticMetricRow {
        dimension: "优化前".to_string(),
        metric: scope.to_string(),
        value: before_value,
        note: String::new(),
        statement: statement.to_string(),
        observed_at,
    });
    rows.push(SemanticMetricRow {
        dimension: "优化后".to_string(),
        metric: scope.to_string(),
        value: after_value,
        note: String::new(),
        statement: statement.to_string(),
        observed_at,
    });
}

fn numeric_tokens(text: &str) -> Vec<NumericToken> {
    const UNITS: &[&str] = &[
        "个百分点",
        "/百万token",
        "/百万 token",
        "tokens",
        "token",
        "元/秒",
        "万元",
        "亿元",
        "万张",
        "毫秒",
        "分钟",
        "分",
        "小时",
        "人民币",
        "美元",
        "％",
        "%",
        "gb",
        "tb",
        "mb",
        "kb",
        "qps",
        "亿",
        "万",
        "元",
        "核",
        "个",
        "次",
        "条",
        "类",
        "人",
        "集",
        "倍",
        "秒",
    ];
    let chars = text.char_indices().collect::<Vec<_>>();
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        let (start, ch) = chars[index];
        if !ch.is_ascii_digit()
            || (index > 0
                && (chars[index - 1].1.is_ascii_alphanumeric() || chars[index - 1].1 == '.'))
        {
            index += 1;
            continue;
        }
        let mut cursor = index + 1;
        while cursor < chars.len() {
            let current = chars[cursor].1;
            if current.is_ascii_digit()
                || matches!(current, '.' | ',' | '，')
                || (matches!(current, '-' | '~' | '～')
                    && chars
                        .get(cursor + 1)
                        .is_some_and(|(_, next)| next.is_ascii_digit()))
            {
                cursor += 1;
            } else {
                break;
            }
        }
        let number_end = chars
            .get(cursor)
            .map(|(byte, _)| *byte)
            .unwrap_or(text.len());
        let whitespace_len = text[number_end..].len() - text[number_end..].trim_start().len();
        let unit_start = number_end + whitespace_len;
        let unit_tail = text[unit_start..].to_lowercase();
        let quantity_modifier = ["多", "余"]
            .iter()
            .find(|modifier| unit_tail.starts_with(**modifier))
            .copied()
            .unwrap_or_default();
        let unit_search_tail = unit_tail
            .strip_prefix(quantity_modifier)
            .unwrap_or(&unit_tail);
        let unit = UNITS
            .iter()
            .filter(|unit| unit_search_tail.starts_with(**unit))
            .max_by_key(|unit| unit.len())
            .copied()
            .unwrap_or_default();
        let mut end = if unit.is_empty() {
            number_end
        } else {
            unit_start + quantity_modifier.len() + unit.len()
        };
        // 复合时长是一个不可拆分的业务值。旧解析会把“16分31秒”拆成
        // 16（无单位）与 31秒，最终只留下 31秒。按从大到小的时间单位
        // 吸收连续分量，同时避免把并列的两个独立耗时误并到一起。
        if let Some(mut previous_rank) = duration_unit_rank(unit) {
            loop {
                let remaining = text[end..].trim_start();
                let whitespace = text[end..].len().saturating_sub(remaining.len());
                let digit_end = remaining
                    .char_indices()
                    .take_while(|(_, ch)| ch.is_ascii_digit() || matches!(ch, '.' | ','))
                    .last()
                    .map(|(position, ch)| position + ch.len_utf8())
                    .unwrap_or(0);
                if digit_end == 0 {
                    break;
                }
                let unit_tail = remaining[digit_end..].trim_start();
                let unit_whitespace = remaining[digit_end..].len().saturating_sub(unit_tail.len());
                let Some((next_unit, next_rank)) = ["毫秒", "分钟", "小时", "分", "秒"]
                    .iter()
                    .filter_map(|candidate| {
                        unit_tail.starts_with(candidate).then(|| {
                            (
                                *candidate,
                                duration_unit_rank(candidate).unwrap_or_default(),
                            )
                        })
                    })
                    .max_by_key(|(candidate, _)| candidate.len())
                else {
                    break;
                };
                if next_rank >= previous_rank || next_rank == 0 {
                    break;
                }
                end += whitespace + digit_end + unit_whitespace + next_unit.len();
                previous_rank = next_rank;
            }
        }
        let range_tail = text[end..].trim_start();
        if matches!(range_tail.chars().next(), Some('-' | '~' | '～')) {
            let separator_len = range_tail.chars().next().map(char::len_utf8).unwrap_or(0);
            let after_separator = range_tail[separator_len..].trim_start();
            let range_digits = after_separator
                .char_indices()
                .take_while(|(_, current)| current.is_ascii_digit() || matches!(current, '.' | ','))
                .last()
                .map(|(position, current)| position + current.len_utf8())
                .unwrap_or(0);
            if range_digits > 0 {
                let range_unit_source = &after_separator[range_digits..];
                let range_unit_whitespace =
                    range_unit_source.len() - range_unit_source.trim_start().len();
                let range_unit_start = range_digits + range_unit_whitespace;
                let range_unit_tail = after_separator[range_unit_start..].to_lowercase();
                let range_unit = UNITS
                    .iter()
                    .filter(|candidate| range_unit_tail.starts_with(**candidate))
                    .max_by_key(|candidate| candidate.len())
                    .copied()
                    .unwrap_or_default();
                if !unit.is_empty() || !range_unit.is_empty() {
                    let absolute_separator = text.len() - range_tail.len();
                    let absolute_after_separator = text.len() - after_separator.len();
                    end = absolute_after_separator + range_unit_start + range_unit.len();
                    if range_unit.is_empty() && !unit.is_empty() {
                        end = absolute_after_separator + range_digits;
                    }
                    if absolute_separator >= start {
                        // `end` now spans the complete range, such as `10%-31%`.
                    }
                }
            }
        }
        let value = text[start..end].trim().to_string();
        let suffix = text[end..].trim_start();
        let looks_like_list_ordinal =
            unit.is_empty() && matches!(suffix.chars().next(), Some('、' | ')' | '）'));
        let looks_like_identifier = unit.is_empty()
            && (suffix
                .chars()
                .next()
                .is_some_and(|ch| ch.is_ascii_alphabetic())
                || text[..start].ends_with('+'));
        if !value.is_empty() && !looks_like_list_ordinal && !looks_like_identifier {
            tokens.push(NumericToken {
                start,
                end,
                value,
                has_explicit_unit: !unit.is_empty(),
            });
        }
        while index < chars.len() && chars[index].0 < end {
            index += 1;
        }
    }
    tokens
}

fn duration_unit_rank(unit: &str) -> Option<u8> {
    match unit.to_lowercase().as_str() {
        "小时" => Some(4),
        "分钟" | "分" => Some(3),
        "秒" => Some(2),
        "毫秒" => Some(1),
        _ => None,
    }
}

fn metric_for_token(
    clause: &str,
    token: &NumericToken,
    dimension: &str,
    inherited_metric: &str,
) -> Option<String> {
    let prefix = clause[..token.start].trim_end();
    let suffix = clause[token.end..].trim_start();
    for (marker, change) in [
        ("下降约", "降幅"),
        ("降低约", "降幅"),
        ("下降了", "降幅"),
        ("降低了", "降幅"),
        ("下降", "降幅"),
        ("降低", "降幅"),
        ("降了", "降幅"),
        ("增长约", "增幅"),
        ("增加约", "增幅"),
        ("提升约", "增幅"),
        ("增长了", "增幅"),
        ("增加了", "增幅"),
        ("提升了", "增幅"),
        ("增长", "增幅"),
        ("增加", "增幅"),
        ("提升", "增幅"),
    ] {
        if let Some(raw_metric) = prefix.strip_suffix(marker) {
            let raw_metric = clean_metric_label(raw_metric, dimension);
            if matches!(raw_metric.as_str(), "同比" | "环比") {
                continue;
            }
            if metric_is_meaningful(&raw_metric) {
                return Some(format!("{}{change}", canonical_metric_label(&raw_metric)));
            }
            if let Some(known_metric) =
                known_metric_near(&raw_metric, raw_metric.len(), raw_metric.len())
            {
                return Some(format!(
                    "{}{change}",
                    canonical_metric_label(&known_metric).trim_end_matches(change)
                ));
            }
            if !inherited_metric.is_empty() {
                return Some(format!(
                    "{}{change}",
                    canonical_metric_label(inherited_metric).trim_end_matches(change)
                ));
            }
        }
    }
    if ["需达到", "目标", "基准", "要求达到"]
        .iter()
        .any(|marker| prefix.ends_with(marker))
        && !inherited_metric.is_empty()
    {
        return Some(format!("{}目标", inherited_metric.trim_end_matches("目标")));
    }

    if token.has_explicit_unit
        && !["并", "和", "及", "同时", "且", "、"]
            .iter()
            .any(|marker| suffix.starts_with(marker))
    {
        if let Some(metric) = known_metric_near(suffix, 0, 14) {
            return Some(metric);
        }
    }

    let relation_markers = [
        "压低最多",
        "降低至",
        "下降至",
        "被认为",
        "要求达到",
        "高于",
        "超过",
        "仅为",
        "只有",
        "约为",
        "达到",
        "最多",
        "近",
        "约",
        "为",
        "占",
        "是",
    ];
    for relation in relation_markers {
        let Some(index) = prefix.rfind(relation) else {
            continue;
        };
        if prefix[index + relation.len()..].chars().count() > 3 {
            continue;
        }
        let raw_label = clean_metric_label(&prefix[..index], dimension);
        if raw_label.is_empty() && !inherited_metric.is_empty() {
            return Some(inherited_metric.to_string());
        }
        if relation == "占" && metric_is_subject(&raw_label) {
            return Some(format!("{raw_label}占比"));
        }
        if metric_is_meaningful(&raw_label) {
            return Some(canonical_metric_label(&raw_label));
        }
    }

    if let Some(metric) = known_metric_near(prefix, prefix.len(), 36) {
        let contextual = clean_metric_label(prefix, dimension);
        if contextual.chars().count() <= 36
            && metric_is_meaningful(&contextual)
            && normalize_identity_text(&contextual).contains(&normalize_identity_text(&metric))
        {
            return Some(canonical_metric_label(&contextual));
        }
        return Some(metric);
    }
    if (!dimension.is_empty()
        || ["为", "约", "达到", "仅", "占"]
            .iter()
            .any(|marker| prefix.ends_with(marker)))
        && !inherited_metric.is_empty()
    {
        return Some(inherited_metric.to_string());
    }
    None
}

fn known_metric_near(text: &str, token_start: usize, max_distance: usize) -> Option<String> {
    const MARKERS: &[(&str, &str)] = &[
        ("gpu 利用率", "GPU 利用率"),
        ("gpu利用率", "GPU 利用率"),
        ("gpu 等待时间", "GPU 等待时间"),
        ("gpu等待时间", "GPU 等待时间"),
        ("等待时间", "等待时间"),
        ("smacc", "SMACC"),
        ("smact", "SMACT"),
        ("smocc", "SMOCC"),
        ("gputl", "GPUTL"),
        ("同比增长", "同比增长"),
        ("环比增长", "环比增长"),
        ("同比下降", "同比下降"),
        ("环比下降", "环比下降"),
        ("转化率", "转化率"),
        ("完成率", "完成率"),
        ("达成率", "达成率"),
        ("完成度", "任务完成度"),
        ("增长率", "增长率"),
        ("下降率", "下降率"),
        ("利用率", "利用率"),
        ("点击率", "点击率"),
        ("错误率", "错误率"),
        ("成功率", "成功率"),
        ("命中率", "命中率"),
        ("留存率", "留存率"),
        ("准确率", "准确率"),
        ("占比", "占比"),
        ("比例", "比例"),
        ("销售额", "销售额"),
        ("客单价", "客单价"),
        ("订单数", "订单数"),
        ("新增用户", "新增用户"),
        ("活跃用户", "活跃用户"),
        ("用户数", "用户数"),
        ("客户数", "客户数"),
        ("错误数", "错误数"),
        ("成功数", "成功数"),
        ("失败数", "失败数"),
        ("请求数", "请求数"),
        ("工单数", "工单数"),
        ("告警数", "告警数"),
        ("响应时间", "响应时间"),
        ("处理时长", "处理时长"),
        ("执行时长", "执行时长"),
        ("时长", "时长"),
        ("token 总量", "Token 规模"),
        ("token总量", "Token 规模"),
        ("token 规模", "Token 规模"),
        ("token规模", "Token 规模"),
        ("挽回资损", "资损挽回金额"),
        ("资损挽回", "资损挽回金额"),
        ("挽损", "资损挽回金额"),
        ("资损", "资损挽回金额"),
        ("账户余额", "账户余额"),
        ("累计消耗", "累计消耗"),
        ("消耗", "累计消耗"),
        ("收入", "收入"),
        ("营收", "营收"),
        ("成本", "成本"),
        ("利润", "利润"),
        ("毛利", "毛利"),
        ("订单", "订单"),
        ("销量", "销量"),
        ("库存", "库存"),
        ("预算", "预算"),
        ("金额", "金额"),
        ("余额", "余额"),
        ("汇率", "汇率"),
        ("单价", "单价"),
        ("延迟", "延迟"),
        ("耗时", "耗时"),
        ("cpu", "CPU"),
        ("内存", "内存"),
        ("存储", "存储"),
        ("容量", "容量"),
        ("用量", "用量"),
        ("负载", "负载"),
        ("dau", "DAU"),
        ("mau", "MAU"),
        ("gmv", "GMV"),
        ("qps", "QPS"),
        ("pv", "PV"),
        ("uv", "UV"),
    ];
    let lower = text.to_lowercase();
    MARKERS
        .iter()
        .filter_map(|(marker, display)| {
            lower
                .match_indices(marker)
                .filter_map(|(position, _)| {
                    let distance = position.abs_diff(token_start);
                    (distance <= max_distance).then_some((distance, marker.len(), *display))
                })
                .min()
        })
        .min_by_key(|(distance, marker_len, _)| (*distance, std::cmp::Reverse(*marker_len)))
        .map(|(_, _, display)| display.to_string())
}

fn detect_dimension(prefix: &str) -> Option<String> {
    const DIMENSIONS: &[&str] = &[
        "优化前",
        "优化后",
        "目标",
        "基准",
        "方案一",
        "方案二",
        "国内",
        "海外",
        "本周",
        "上周",
        "前一周",
        "上一周",
        "本月",
        "上月",
        "本季度",
        "上季度",
        "今年",
        "去年",
        "当前",
        "昨日",
        "今日",
        "日峰",
        "峰值",
        "平均",
        "整体",
        "之前",
        "此前",
    ];
    DIMENSIONS
        .iter()
        .filter(|dimension| prefix.contains(**dimension))
        .max_by_key(|dimension| dimension.chars().count())
        .map(|dimension| (*dimension).to_string())
}

fn clean_metric_label(raw: &str, dimension: &str) -> String {
    let mut label = raw
        .trim_matches(|ch: char| ch.is_whitespace() || "：:|/（）()【】[]•●".contains(ch))
        .to_string();
    for prefix in [
        "背景显示",
        "数据显示",
        "数据表明",
        "结果显示",
        "显示",
        "对比发现",
        "其中",
        "其次",
        "另外",
        "此外还提及了",
        "此外，还提及了",
        "此外还涉及了",
        "此外，还涉及了",
        "还涉及了",
    ] {
        if label.starts_with(prefix) {
            label = label[prefix.len()..].trim().to_string();
        }
    }
    if !dimension.is_empty() {
        label = label
            .strip_prefix(dimension)
            .unwrap_or(&label)
            .trim()
            .to_string();
    }
    for suffix in ["被认为", "认为需", "认为", "需要", "仅", "大约"] {
        if label.ends_with(suffix) {
            label.truncate(label.len() - suffix.len());
            label = label.trim().to_string();
        }
    }
    if label.chars().count() > 48 {
        label = label
            .chars()
            .rev()
            .take(48)
            .collect::<String>()
            .chars()
            .rev()
            .collect();
    }
    label
}

fn metric_is_subject(value: &str) -> bool {
    let meaningful_chars = value.chars().filter(|ch| ch.is_alphabetic()).count();
    meaningful_chars >= 2 && !matches!(value.trim(), "数据" | "指标" | "类别" | "类型")
}

fn metric_is_meaningful(value: &str) -> bool {
    let value = value.trim();
    if !metric_is_subject(value) || value.chars().any(|ch| ch.is_ascii_digit()) {
        return false;
    }
    let lower = value.to_lowercase();
    if matches!(
        lower.as_str(),
        "id" | "ip" | "背景" | "数据显示" | "数据" | "类别" | "类型" | "类"
    ) {
        return false;
    }
    if ["地址", "端口", "步骤", "文件", "第几", "编号", "版本"]
        .iter()
        .any(|marker| lower.contains(marker))
    {
        return false;
    }
    const SEMANTIC_HINTS: &[&str] = &[
        "率",
        "占比",
        "比例",
        "收入",
        "营收",
        "成本",
        "利润",
        "毛利",
        "订单",
        "销量",
        "销售额",
        "客单价",
        "活跃",
        "留存",
        "同比",
        "环比",
        "增幅",
        "降幅",
        "库存",
        "预算",
        "金额",
        "余额",
        "汇率",
        "单价",
        "错误",
        "成功",
        "失败",
        "请求",
        "工单",
        "告警",
        "延迟",
        "耗时",
        "时长",
        "cpu",
        "gpu",
        "内存",
        "存储",
        "容量",
        "用量",
        "负载",
        "dau",
        "mau",
        "gmv",
        "qps",
        "pv",
        "uv",
        "smacc",
        "smact",
        "smocc",
        "gputl",
        "token",
        "完成度",
        "消耗",
    ];
    SEMANTIC_HINTS.iter().any(|hint| lower.contains(hint))
}

fn canonical_metric_label(value: &str) -> String {
    let normalized_action = strip_metric_action_affixes(value);
    let trimmed = normalized_action.trim();
    for change in ["同比降幅", "环比降幅", "同比增幅", "环比增幅"] {
        if let Some(base) = trimmed.strip_suffix(change) {
            let base = canonical_metric_label(base);
            if !base.is_empty() {
                return format!("{base}{change}");
            }
        }
    }
    for change in ["降幅", "增幅"] {
        if let Some(base) = trimmed.strip_suffix(change) {
            let base = canonical_metric_label(base);
            if !base.is_empty() {
                return format!("{base}{change}");
            }
        }
    }
    if let Some(exact) = canonical_metric_label_exact(trimmed) {
        return exact;
    }
    if let Some(known) = known_metric_near(trimmed, trimmed.len(), trimmed.len()) {
        let compact = compact_contextual_metric(trimmed, &known);
        let normalized = strip_metric_action_affixes(&compact);
        return canonical_metric_label_exact(&normalized).unwrap_or(normalized);
    }
    trimmed.to_string()
}

fn strip_metric_action_affixes(value: &str) -> String {
    let mut label = value.trim().to_string();
    const ACTION_PREFIXES: &[&str] = &[
        "累计产生",
        "累计实现",
        "累计达成",
        "累计贡献",
        "已经产生",
        "已经实现",
        "已经达成",
        "已经贡献",
        "已产生",
        "已实现",
        "已达成",
        "已贡献",
        "共产生",
        "共实现",
        "共达成",
        "共贡献",
        "产生",
        "实现",
        "达成",
        "贡献",
        "创造",
        "带来",
        "取得",
        "同时把",
        "同时将",
        "把",
        "将",
    ];
    loop {
        let Some(stripped) = ACTION_PREFIXES
            .iter()
            .find_map(|prefix| label.strip_prefix(prefix))
            .map(str::trim)
            .filter(|remainder| metric_tail_is_recognizable(remainder))
        else {
            break;
        };
        label = stripped.to_string();
    }
    label
}

fn metric_tail_is_recognizable(value: &str) -> bool {
    canonical_metric_label_exact(value).is_some()
        || known_metric_near(value, value.len(), value.len()).is_some()
}

fn compact_contextual_metric(value: &str, known_metric: &str) -> String {
    let mut label = value
        .trim_matches(|ch: char| {
            ch.is_whitespace() || "：:|/（）()【】[]“”\"'，,。；;".contains(ch)
        })
        .to_string();
    if let Some(position) = label.rfind(|ch| matches!(ch, '：' | ':')) {
        let separator_len = label[position..].chars().next().unwrap().len_utf8();
        let before = label[..position].trim();
        let after = label[position + separator_len..].trim();
        label = if normalize_identity_text(before).contains(&normalize_identity_text(known_metric))
        {
            before.to_string()
        } else {
            after.to_string()
        };
    }
    for marker in [
        "背景显示",
        "数据显示",
        "数据表明",
        "结果显示",
        "对比发现",
        "观察到",
        "注意到",
        "查看到",
        "发现",
    ] {
        if let Some(position) = label.rfind(marker) {
            label = label[position + marker.len()..].trim().to_string();
        }
    }
    loop {
        let previous = label.clone();
        for prefix in [
            "经过优化后",
            "系统提示",
            "指出",
            "显示",
            "表明",
            "说明",
            "可见",
            "其中",
            "其次",
            "另外",
            "约为",
            "约",
            "但",
        ] {
            if let Some(stripped) = label.strip_prefix(prefix) {
                label = stripped
                    .trim_matches(|ch: char| ch.is_whitespace() || "：:，,。；;“”\"'".contains(ch))
                    .to_string();
                break;
            }
        }
        if label == previous {
            break;
        }
    }
    if label.starts_with("包括") {
        if let Some(position) = label.rfind('、') {
            let candidate = label[position + '、'.len_utf8()..].trim();
            if normalize_identity_text(candidate).contains(&normalize_identity_text(known_metric)) {
                label = candidate.to_string();
            }
        }
    }
    let lower = label.to_lowercase();
    let marker = known_metric.to_lowercase();
    if let Some(position) = lower.rfind(&marker) {
        let prefix = label[..position].trim();
        if contextual_metric_prefix_is_narrative(prefix) {
            return known_metric.to_string();
        }
        let end = position + marker.len();
        if label.is_char_boundary(end) {
            label.truncate(end);
        }
    }
    if !normalize_identity_text(&label).contains(&normalize_identity_text(known_metric))
        && known_metric.chars().count() >= 4
    {
        return known_metric.to_string();
    }
    label = label
        .trim_matches(|ch: char| {
            ch.is_whitespace() || "：:|/（）()【】[]“”\"'，,。；;的".contains(ch)
        })
        .to_string();
    if label.is_empty() || label.chars().count() > 24 {
        known_metric.to_string()
    } else {
        canonical_metric_label_exact(&label).unwrap_or(label)
    }
}

fn contextual_metric_prefix_is_narrative(prefix: &str) -> bool {
    let normalized = normalize_identity_text(prefix);
    [
        "用户",
        "随后用户",
        "接着用户",
        "然后用户",
        "整个任务",
        "本次任务",
        "该任务",
        "任务总",
        "具体的生成参数",
    ]
    .iter()
    .any(|marker| normalized.starts_with(&normalize_identity_text(marker)))
        || ["配置了", "设定了", "设置了", "选择了"]
            .iter()
            .any(|marker| normalized.contains(&normalize_identity_text(marker)))
}

fn canonical_metric_label_exact(value: &str) -> Option<String> {
    match value.trim().to_lowercase().as_str() {
        "cpu" => Some("CPU".to_string()),
        "gpu" | "gpu利用率" | "gpu 利用率" => Some("GPU 利用率".to_string()),
        "dau" => Some("DAU".to_string()),
        "mau" => Some("MAU".to_string()),
        "gmv" => Some("GMV".to_string()),
        "qps" => Some("QPS".to_string()),
        "pv" => Some("PV".to_string()),
        "uv" => Some("UV".to_string()),
        "smacc" => Some("SMACC".to_string()),
        "smact" => Some("SMACT".to_string()),
        "smocc" => Some("SMOCC".to_string()),
        "gputl" => Some("GPUTL".to_string()),
        _ => None,
    }
}

fn extract_statement_insight(statement: &str) -> Option<String> {
    let marker = ["但", "表明", "说明", "意味着", "因此", "所以"]
        .iter()
        .filter_map(|marker| statement.find(marker).map(|position| (position, *marker)))
        .min_by_key(|(position, _)| *position)?;
    let mut insight = statement[marker.0 + marker.1.len()..]
        .trim_matches(|ch: char| ch.is_whitespace() || "，,：:。；;".contains(ch))
        .to_string();
    insight = insight
        .replace("存在掩盖低效的事实", "可能掩盖实际低效")
        .replace("存在掩盖低效的情况", "可能掩盖实际低效");
    if ["的实", "的情", "的状", "以及", "并且"]
        .iter()
        .any(|ending| insight.ends_with(ending))
    {
        return None;
    }
    (!insight.is_empty()).then_some(clip_text(&insight, 120))
}

fn summarize_rows(rows: &[SemanticMetricRow], insight: Option<&str>) -> String {
    let mut summary = String::new();
    if let Some(first) = rows.first() {
        let comparable = rows
            .iter()
            .filter(|row| row.metric == first.metric && !row.dimension.is_empty())
            .take(4)
            .collect::<Vec<_>>();
        if comparable.len() >= 2 {
            summary = format!(
                "{}：{}",
                first.metric,
                comparable
                    .iter()
                    .map(|row| format!("{} {}", row.dimension, row.value))
                    .collect::<Vec<_>>()
                    .join("，")
            );
            let extras = rows
                .iter()
                .filter(|row| row.metric != first.metric)
                .take(3)
                .map(|row| {
                    if row.dimension.is_empty() {
                        format!("{} {}", row.metric, row.value)
                    } else {
                        format!("{}{} {}", row.dimension, row.metric, row.value)
                    }
                })
                .collect::<Vec<_>>();
            if !extras.is_empty() {
                summary.push_str("，");
                summary.push_str(&extras.join("，"));
            }
        }
    }
    if summary.is_empty() {
        summary = rows
            .iter()
            .take(4)
            .map(|row| {
                if row.dimension.is_empty() {
                    format!("{} {}", row.metric, row.value)
                } else {
                    format!("{}{} {}", row.dimension, row.metric, row.value)
                }
            })
            .collect::<Vec<_>>()
            .join("，");
    }
    if let Some(insight) = insight.filter(|value| !value.is_empty()) {
        summary.push_str("；");
        summary.push_str(insight);
    }
    clip_text(&summary, 220)
}

fn metric_statements(text: &str, observed_at: i64) -> Vec<Value> {
    text.split(['\n', '。', '；', ';'])
        .map(str::trim)
        .filter(|line| semantic_statement(line, Some(observed_at)).is_some())
        .take(80)
        .map(|line| json!({"statement": clip_text(line, 500), "observed_at": observed_at}))
        .collect()
}

fn extract_http_urls(text: &str) -> Vec<String> {
    let mut results = Vec::new();
    let mut remaining = text;
    while let Some(start) = [remaining.find("https://"), remaining.find("http://")]
        .into_iter()
        .flatten()
        .min()
    {
        let tail = &remaining[start..];
        let end = tail
            .char_indices()
            .find(|(_, ch)| ch.is_whitespace() || "\"'<>（）()【】[]，。；、".contains(*ch))
            .map(|(index, _)| index)
            .unwrap_or(tail.len());
        let url = tail[..end]
            .trim_end_matches(['.', ',', ';', ':'])
            .to_string();
        if !url.is_empty() && !results.contains(&url) {
            results.push(url);
        }
        remaining = &tail[end..];
        if end == 0 {
            break;
        }
    }
    results
}

fn canonical_data_url(raw: &str) -> Option<String> {
    let mut parsed = reqwest::Url::parse(raw.trim()).ok()?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return None;
    }
    let filtered_pairs = parsed
        .query_pairs()
        .filter(|(key, _)| !is_sensitive_url_parameter(key))
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect::<Vec<_>>();
    if parsed.query().is_some() {
        parsed.set_query(None);
        if !filtered_pairs.is_empty() {
            parsed.query_pairs_mut().extend_pairs(&filtered_pairs);
        }
    }
    if parsed
        .fragment()
        .map(|fragment| {
            let lowered = fragment.to_lowercase();
            lowered.contains("access_token=")
                || lowered.contains("id_token=")
                || lowered.contains("api_key=")
                || lowered.contains("signature=")
        })
        .unwrap_or(false)
    {
        parsed.set_fragment(None);
    }
    if parsed.path().len() > 1 && parsed.path().ends_with('/') {
        let trimmed = parsed.path().trim_end_matches('/').to_string();
        parsed.set_path(&trimmed);
    }
    Some(parsed.to_string())
}

fn is_sensitive_url_parameter(key: &str) -> bool {
    let key = key.to_lowercase();
    matches!(
        key.as_str(),
        "token"
            | "access_token"
            | "id_token"
            | "refresh_token"
            | "api_key"
            | "apikey"
            | "authorization"
            | "password"
            | "passwd"
            | "secret"
            | "signature"
            | "sig"
            | "credential"
            | "oauth_code"
    ) || key.ends_with("_token")
        || key.contains("signature")
        || key.contains("credential")
}

fn source_exists(conn: &Connection, key: &str) -> Result<bool, StorageError> {
    conn.query_row(
        "SELECT COUNT(*) > 0 FROM data_sources WHERE canonical_key = ?1",
        [key],
        |row| row.get(0),
    )
    .map_err(Into::into)
}

fn source_id_for_key(conn: &Connection, key: &str) -> Result<i64, StorageError> {
    conn.query_row(
        "SELECT id FROM data_sources WHERE canonical_key = ?1",
        [key],
        |row| row.get(0),
    )
    .map_err(Into::into)
}

fn latest_snapshot(
    conn: &Connection,
    source: &DataSourceRecord,
) -> Result<Option<DataSnapshotRecord>, StorageError> {
    let mut snapshot = conn
        .query_row(
            "SELECT id, source_id, collected_at, observed_at, collector, content_text,
                structured_data, content_hash, freshness_ttl_seconds, provenance,
                source_capture_ids, source_timeline_ids, status, period_granularity,
                period_key, period_start_at, period_end_at
         FROM data_snapshots WHERE source_id = ?1
         ORDER BY collected_at DESC, id DESC LIMIT 1",
            [source.id],
            map_data_snapshot_row,
        )
        .optional()?;
    if let Some(snapshot) = &mut snapshot {
        let mut stmt = conn.prepare(
            "SELECT capture_id, timeline_id
             FROM data_source_links
             WHERE source_id = ?1
             ORDER BY observed_at DESC, id DESC",
        )?;
        let links = stmt.query_map([source.id], |row| {
            Ok((row.get::<_, Option<i64>>(0)?, row.get::<_, Option<i64>>(1)?))
        })?;
        for link in links {
            let (capture_id, timeline_id) = link?;
            if let Some(capture_id) = capture_id {
                snapshot.source_capture_ids.push(capture_id);
            }
            if let Some(timeline_id) = timeline_id {
                snapshot.source_timeline_ids.push(timeline_id);
            }
        }
        snapshot.source_capture_ids.sort_unstable();
        snapshot.source_capture_ids.dedup();
        snapshot.source_timeline_ids.sort_unstable();
        snapshot.source_timeline_ids.dedup();
        let semantic_context = semantic_context_for_source(
            source,
            None,
            snapshot
                .structured_data
                .get("semantic_subject")
                .and_then(Value::as_str),
        );
        let semantic = semantic_view_for_snapshot(snapshot, &semantic_context)
            .unwrap_or_else(|| rejected_semantic_view_json("no_semantic_metric"));
        merge_semantic_view(&mut snapshot.structured_data, semantic);
    }
    Ok(snapshot)
}

fn merge_semantic_view(structured: &mut Value, semantic: Value) {
    if !structured.is_object() {
        *structured = json!({});
    }
    let Some(target) = structured.as_object_mut() else {
        return;
    };
    let Some(fields) = semantic.as_object() else {
        return;
    };
    for key in [
        "extraction_version",
        "title",
        "summary",
        "semantic_subject",
        "semantic_identity",
        "metric_rows",
        "metric_statements",
        "rejection_reason",
    ] {
        if let Some(value) = fields.get(key) {
            target.insert(key.to_string(), value.clone());
        }
    }
    if fields
        .get("metric_rows")
        .and_then(Value::as_array)
        .is_some_and(|rows| !rows.is_empty())
    {
        target.remove("rejection_reason");
    }
}

fn map_data_source_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<DataSourceRecord> {
    Ok(DataSourceRecord {
        id: row.get(0)?,
        title: row.get(1)?,
        source_kind: row.get(2)?,
        source_url: row.get(3)?,
        access_mode: row.get(4)?,
        refresh_policy: row.get(5)?,
        realtime_level: row.get(6)?,
        source_app_name: row.get(7)?,
        source_window_title: row.get(8)?,
        tags: parse_json_strings(row.get::<_, String>(9)?),
        first_seen_at: row.get(10)?,
        last_seen_at: row.get(11)?,
        last_collected_at: row.get(12)?,
        last_success_at: row.get(13)?,
        last_error_code: row.get(14)?,
        status: row.get(15)?,
        created_at: row.get(16)?,
        updated_at: row.get(17)?,
        latest_snapshot: None,
    })
}

fn parse_json_value(raw: String, fallback: Value) -> Value {
    serde_json::from_str(&raw).unwrap_or(fallback)
}

fn parse_json_strings(raw: String) -> Vec<String> {
    serde_json::from_str(&raw).unwrap_or_default()
}

fn parse_json_i64(raw: String) -> Vec<i64> {
    serde_json::from_str(&raw).unwrap_or_default()
}

fn hash_text(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

fn clip_text(text: &str, max_chars: usize) -> String {
    let normalized = text.trim();
    if normalized.chars().count() <= max_chars {
        return normalized.to_string();
    }
    normalized.chars().take(max_chars).collect::<String>() + "…"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_numeric_ranges_with_whitespace_before_the_second_unit() {
        let tokens = numeric_tokens("视频推理成本从 0.2元/秒降至 0.02 元/秒");
        assert_eq!(
            tokens
                .iter()
                .map(|token| token.value.as_str())
                .collect::<Vec<_>>(),
            vec!["0.2元/秒", "0.02 元/秒"]
        );

        let range = numeric_tokens("延迟区间为 79秒 - 136 秒");
        assert_eq!(range.len(), 1);
        assert_eq!(range[0].value, "79秒 - 136 秒");
    }

    #[test]
    fn keeps_compound_durations_as_one_value() {
        let compact = numeric_tokens("任务总耗时约16分31秒");
        assert_eq!(compact.len(), 1);
        assert_eq!(compact[0].value, "16分31秒");

        let verbose = numeric_tokens("整轮处理耗时1小时20分钟5秒");
        assert_eq!(verbose.len(), 1);
        assert_eq!(verbose[0].value, "1小时20分钟5秒");
    }

    #[test]
    fn legacy_parser_rejects_narrative_parameter_and_task_fragments() {
        for statement in [
            "用户设定了视频时长为15秒",
            "随后用户配置了具体的生成参数：时长设为15秒",
            "整个任务耗时约16分31秒完成",
            "任务总耗时约16分31秒",
        ] {
            let views = semantic_views_from_statements(
                &[json!({"statement": statement, "observed_at": 1_i64})],
                "window_title:ChatGPT\napplication:ChatGPT",
            );
            assert!(
                views.is_empty(),
                "narrative fragment must not become a data title: {statement}; views={views:?}"
            );
        }
    }

    #[test]
    fn detects_report_urls_and_metric_statements() {
        assert!(looks_like_data_url(
            "https://bi.example.com/dashboard/weekly",
            "经营看板",
            ""
        ));
        assert!(is_concrete_data_statement("本周订单 1200，环比增长 8%"));
        assert!(is_concrete_data_statement(
            "数据库实例配置为 4 核 CPU、8GB 内存和 50GB 存储"
        ));
        assert!(is_concrete_data_statement(
            "服务器内存用量为 864MB，当前负载较高"
        ));
        assert!(!is_concrete_data_statement("完成订单模块重构"));
        assert!(!is_concrete_data_statement("规划 1000 集历史短剧生产流程"));
        assert!(!is_concrete_data_statement("鲜嘉麒、王海威等 5 人参加会议"));
        assert!(!is_concrete_data_statement("第 2/5 步，7 个文件已更改"));
        assert!(!is_concrete_data_statement(
            "用户在 2026 年 8 月 1 日 13:29 打开访达"
        ));
        assert!(!is_concrete_data_statement(
            "Comments (1) Go to the first comment Reply... 0 words Upload Log Help Center Keyboard Shortcuts"
        ));
        assert!(!is_concrete_data_statement("9类 43%"));
        assert!(!is_concrete_data_statement("7类33%"));
        assert!(!is_concrete_data_statement("文本推理资产 5类|24%"));
        assert!(is_concrete_data_statement(
            "生成与理解占76%，是跨BU复用的首要抓手"
        ));
    }

    #[test]
    fn preserves_stable_source_scope_for_parent_dependent_metrics() {
        let views = semantic_views_from_statements(
            &[json!({
                "statement": "总体上，生成与理解两类合计占76%，作为跨BU复用的第一阶段主战场，文本推理类则以框架共享、知识隔离和效果评测为主要抓手",
                "observed_at": 1785813798828_i64,
            })],
            "针对商业化、电商及本地生活三大业务线制定 AI 建设资产复用方案，将能力按最终产出归为生成式、音视频理解和文本推理三类\nwindow_title:商业体系-AI建设资产复用方案 - 云文档\napplication:Google Chrome",
        );

        assert_eq!(views.len(), 1);
        let view = &views[0];
        assert_eq!(view.subject, "AI 建设资产分类");
        assert_eq!(view.title, "AI 建设资产分类中生成与理解两类合计占比");
        assert!(!view.summary.contains("商业体系-AI建设资产复用方案"));
        assert!(view.summary.contains("生成与理解两类合计占比 76%"));
        assert!(semantic_view_is_self_contained(view));

        let compact_wording = semantic_views_from_statements(
            &[json!({
                "statement": "生成与理解占76%，是跨BU复用的首要抓手",
                "observed_at": 1785813798828_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );
        assert_eq!(compact_wording.len(), 1);
        assert_eq!(compact_wording[0].subject, "AI 建设资产分类");
        assert_eq!(
            compact_wording[0].title,
            "AI 建设资产分类中生成与理解两类合计占比"
        );

        let resolved_reference = semantic_views_from_statements(
            &[json!({
                "statement": "方案将 AI 能力分为生成式（9类）、音视频理解（7类）和文本推理（5类），其中前两类合计占比76%，作为复用主战场",
                "observed_at": 1785813798828_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );
        assert_eq!(resolved_reference.len(), 1);
        assert_eq!(
            resolved_reference[0].rows[0].metric,
            "生成式与音视频理解两类合计占比"
        );
        assert!(resolved_reference[0]
            .title
            .contains("生成式与音视频理解两类合计占比"));
        assert!(!resolved_reference[0]
            .title
            .contains("商业体系-AI建设资产复用方案"));

        let cross_domain = semantic_views_from_statements(
            &[json!({
                "statement": "直营与代理两类合计占68%",
                "observed_at": 1785813798828_i64,
            })],
            "window_title:渠道策略复盘 - 云文档",
        );
        assert_eq!(cross_domain.len(), 1);
        assert_eq!(cross_domain[0].subject, "");
        assert_eq!(cross_domain[0].title, "直营与代理两类合计占比");

        let ordinary_metric = semantic_views_from_statements(
            &[json!({
                "statement": "工单成功率为92%",
                "observed_at": 1785813798828_i64,
            })],
            "window_title:客服质量周报 - 云文档",
        );
        assert_eq!(ordinary_metric.len(), 1);
        assert_eq!(ordinary_metric[0].subject, "");
        assert_eq!(ordinary_metric[0].title, "工单成功率");

        let self_contained_metric = semantic_views_from_statements(
            &[json!({
                "statement": "AI 图生成功能每日产出约 6 万张图片",
                "observed_at": 1785819162753_i64,
            })],
            "timeline_topic:用户查阅了商业体系 AI 建设资产复用方案的云文档\nwindow_title:商业体系-AI建设资产复用方案 - 云文档\napplication:Google Chrome\nprevious_subject:商业体系-AI建设资产复用方案",
        );
        assert_eq!(self_contained_metric.len(), 1);
        assert_eq!(self_contained_metric[0].subject, "");
        assert_eq!(self_contained_metric[0].title, "AI 图生成功能每日产出");
        assert_eq!(
            self_contained_metric[0].summary,
            "AI 图生成功能每日产出：AI 图生成功能每日产出 6 万张"
        );

        let missing_scope = semantic_views_from_statements(
            &[json!({
                "statement": "总体上，生成与理解两类合计占76%",
                "observed_at": 1785813798828_i64,
            })],
            "application:Google Chrome",
        );
        assert!(missing_scope.is_empty());
    }

    #[test]
    fn repairs_discourse_metrics_and_rejects_unit_or_navigation_noise() {
        assert_eq!(
            generic_metric_family("Token 规模").as_deref(),
            Some("token规模")
        );
        let token_scale = semantic_views_from_statements(
            &[json!({
                "statement": "此外，还涉及了 token@鲜嘉麒的数据更新请求以及马达关于 Token 总量同比变化的汇报（前一周为 2000 多亿）",
                "observed_at": 1785816879736_i64,
            })],
            "timeline_topic:用户参与了关于 AIGC 工程突击和商业化架构的会议讨论，并查看了相关文档\nwindow_title:Kim\nprevious_subject:AIGC",
        );
        assert_eq!(token_scale.len(), 1);
        assert_eq!(token_scale[0].subject, "AIGC 工程突击和商业化架构");
        assert_eq!(token_scale[0].rows[0].metric, "Token 规模");
        assert_eq!(token_scale[0].rows[0].value, "2000 多亿");
        assert_eq!(token_scale[0].title, "AIGC 工程突击和商业化架构 Token 规模");

        let risk_recovery = semantic_views_from_statements(
            &[json!({
                "statement": "此外还提及了 RiskOS 风控体系建设以降低人力成本并挽回资损超过 3.96 亿的目标",
                "observed_at": 1785817532850_i64,
            })],
            "timeline_topic:记录了商业化技术部 AI 业务月会纪要，总结了效果投放、线索广告及品牌营销\nwindow_title:商业化技术部AI业务月会-2026年7月 副本 - 云文档",
        );
        assert_eq!(risk_recovery.len(), 1);
        assert_eq!(risk_recovery[0].subject, "RiskOS");
        assert_eq!(risk_recovery[0].rows[0].metric, "资损挽回金额");
        assert_eq!(risk_recovery[0].rows[0].value, "3.96 亿");
        assert_eq!(risk_recovery[0].title, "RiskOS 资损挽回金额");

        let workforce = semantic_views_from_statements(
            &[json!({
                "statement": "RiskOS 预计降低人力成本超 300 人并挽回资损近 4 亿元",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:商业化技术部AI业务月会-2026年7月 - 云文档",
        );
        assert_eq!(workforce.len(), 1);
        assert!(workforce[0]
            .rows
            .iter()
            .any(|row| row.metric == "人力缩减目标" && row.value == "300 人"));
        assert!(workforce[0]
            .rows
            .iter()
            .any(|row| row.metric == "资损挽回金额" && row.value == "4 亿元"));
        assert!(!workforce[0].summary.contains("目标人力缩减目标"));

        let explicit_system = semantic_views_from_statements(
            &[json!({
                "statement": "O2 建设行业领先的大模型+Agent智能风控体系RiskOS，推动人机协同（HITL）范式变革，全年HC降低>300，商业化年度成本资损挽回>3.96亿KR1：【KwaiBLM】商审大模型，统一内容理解+生成，构建Deepfake检测能力，账户/投中/复审全机审，HC -300，人+机总成本yoy-1%",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:Docs\napplication:Google Chrome",
        );
        assert_eq!(explicit_system.len(), 1);
        assert_eq!(explicit_system[0].subject, "RiskOS");
        assert_eq!(
            explicit_system[0].title,
            "RiskOS 资损挽回金额与人+机总成本同比降幅"
        );

        let missing_comparison_subject = semantic_views_from_statements(
            &[json!({
                "statement": "约为 OR-LLM-Agent 成本的7倍、耗时的3",
                "observed_at": 1785817532850_i64,
            })],
            "timeline_topic:MemoryBread 项目的数据模块设计与采集工具落地工作\nwindow_title:ChatGPT",
        );
        assert!(missing_comparison_subject.is_empty());

        let application_memory = semantic_views_from_statements(
            &[json!({
                "statement": "用户阅读过程中注意到浏览器内存占用较高（1.0GB）",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档\napplication:Google Chrome",
        );
        assert_eq!(application_memory.len(), 1);
        assert_eq!(application_memory[0].subject, "Google Chrome");
        assert_eq!(application_memory[0].rows[0].metric, "内存");

        let storage_capacity = semantic_views_from_statements(
            &[json!({
                "statement": "规格方面选用了2核4GB通用型实例，存储空间暂定为60GB",
                "observed_at": 1785817532850_i64,
            })],
            "timeline_topic:MemoryBread 的数据采集与服务端架构\nwindow_title:ChatGPT\nprevious_subject:60GB",
        );
        assert_eq!(storage_capacity.len(), 1);
        assert_eq!(
            storage_capacity[0].subject,
            "MemoryBread 的数据采集与服务端架构"
        );
        assert!(!storage_capacity[0].title.contains("60GB"));

        let breadcrumb_title = semantic_views_from_statements(
            &[json!({
                "statement": "登录后系统显示新账户已创建但处于初始状态：API余额为0美元",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:Personal Home | OfoxAI | OfoxAI",
        );
        assert_eq!(breadcrumb_title.len(), 1);
        assert_eq!(breadcrumb_title[0].subject, "OfoxAI");
        assert_eq!(breadcrumb_title[0].title, "OfoxAI API余额");

        let batch_budget = semantic_views_from_statements(
            &[json!({
                "statement": "近期优先只控制数据索引进度，最多75%批次预算处理新增记录，其余持续回填历史",
                "observed_at": 1785817532850_i64,
            })],
            "timeline_topic:MemoryBread 的数据采集与服务端架构\nwindow_title:ChatGPT",
        );
        assert_eq!(batch_budget.len(), 1);
        assert_eq!(batch_budget[0].rows[0].metric, "批次预算占比");

        let conflicting_unit = semantic_views_from_statements(
            &[json!({
                "statement": "石山团队自部署模型的压测成本测算（约800 RH/10秒）",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:Kim",
        );
        assert!(conflicting_unit.is_empty());

        let navigation = semantic_views_from_statements(
            &[json!({
                "statement": "体检预约成功，请您按时体检！返回首页 查看订单 个人中心 1",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:订单确认",
        );
        assert!(navigation.is_empty());

        let decimal_rate = semantic_views_from_statements(
            &[json!({
                "statement": "图可达性任务上的准确率对比：Coconut（连续潜在推理）达到 0.98，明显超过 CoT（0.76）、加长版 CoT*（0.83）和完全不推理的 No CoT（0.75）",
                "observed_at": 1785817532850_i64,
            })],
            "window_title:vedio-aigc",
        );
        assert_eq!(decimal_rate.len(), 1);
        assert_eq!(decimal_rate[0].rows[0].metric, "图可达性任务上的准确率");
        assert_eq!(decimal_rate[0].rows[0].value, "0.98");

        let token_progress = semantic_views_from_statements(
            &[json!({
                "statement": "马达团队提供的关键指标：电商单日 Token 规模达到 3732.62 亿（从之前的 778 亿增长约 5 倍），单位 Token 成本下降 80.7%",
                "observed_at": 1785817532850_i64,
            })],
            "timeline_topic:AIGC 工程突击和商业化架构\nwindow_title:Kim",
        );
        assert_eq!(token_progress.len(), 1);
        assert!(token_progress[0].rows.iter().any(|row| {
            row.dimension == "当前"
                && row.metric == "电商单日 Token 规模"
                && row.value == "3732.62 亿"
        }));
        assert!(token_progress[0].rows.iter().any(|row| {
            row.dimension == "之前" && row.metric == "电商单日 Token 规模" && row.value == "778 亿"
        }));
        assert!(token_progress[0]
            .rows
            .iter()
            .any(|row| row.metric == "Token 规模增幅" && row.value == "5 倍"));
        assert!(token_progress[0]
            .rows
            .iter()
            .any(|row| { row.metric == "单位 Token 成本降幅" && row.value == "80.7%" }));
        assert!(token_progress[0].title.contains("单位 Token 成本降幅"));
        assert!(token_progress[0].summary.contains("5 倍"));
        assert!(token_progress[0].summary.contains("80.7%"));
    }

    #[test]
    fn normalizes_recent_noisy_metrics_without_inventing_source_subjects() {
        let category = semantic_views_from_statements(
            &[json!({
                "statement": "其中生成式和音视频理解能力合计占76%，被视为跨BU复用的主战场",
                "observed_at": 1_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );
        assert_eq!(category.len(), 1);
        assert_eq!(
            category[0].title,
            "AI 建设资产分类中生成式和音视频理解能力两类合计占比"
        );
        assert!(!category[0].title.contains("合计两类合计"));

        let budget = semantic_views_from_statements(
            &[json!({
                "statement": "后两次紧凑重试把输出预算从8192降到 4096，连续撞到长度上限",
                "observed_at": 1_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );
        assert_eq!(budget.len(), 1);
        assert_eq!(budget[0].rows.len(), 2);
        assert!(budget[0].rows.iter().any(|row| {
            row.dimension == "调整前" && row.metric == "输出预算" && row.value == "8192"
        }));
        assert!(budget[0].rows.iter().any(|row| {
            row.dimension == "调整后" && row.metric == "输出预算" && row.value == "4096"
        }));

        let retry_budget = semantic_views_from_statements(
            &[json!({
                "statement": "具体表现为首次调用即无效，后续重试将预算降至 4096 后连续撞上限",
                "observed_at": 1_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );
        assert_eq!(retry_budget.len(), 1);
        assert_eq!(retry_budget[0].title, "后续重试输出预算");

        let request_timing = semantic_views_from_statements(
            &[json!({
                "statement": "本地模型分析实际成功但因同步耗时过长（79-136秒）导致Webview连接中断",
                "observed_at": 1_i64,
            })],
            "window_title:Kim",
        );
        assert_eq!(request_timing.len(), 1);
        assert_eq!(request_timing[0].title, "本地模型分析同步耗时");

        let fee = semantic_views_from_statements(
            &[json!({"statement": "按订单金额的0.4%收取", "observed_at": 1_i64})],
            "window_title:Kim",
        );
        assert_eq!(fee.len(), 1);
        assert_eq!(fee[0].title, "订单金额收费比例");

        let review_target = semantic_views_from_statements(
            &[json!({
                "statement": "将人审量降低比例由当前 16% 提升至 30% 左右",
                "observed_at": 1_i64,
            })],
            "window_title:Kim",
        );
        assert_eq!(review_target.len(), 1);
        assert_eq!(review_target[0].title, "人审量降低比例对比");
        assert!(review_target[0]
            .rows
            .iter()
            .any(|row| row.dimension == "目标" && row.value == "30%"));

        let llm_waste = semantic_views_from_statements(
            &[json!({
                "statement": "一次任务，34 个 LLM turn，总输入 42.6 万 tokens，其中出现了约 96% 的成本浪费",
                "observed_at": 1_i64,
            })],
            "window_title:Projects · GitLab",
        );
        assert_eq!(llm_waste.len(), 1);
        assert_eq!(llm_waste[0].title, "LLM 成本浪费比例");
        assert!(!llm_waste[0].title.contains("GitLab"));

        let gpu_savings = semantic_views_from_statements(
            &[json!({
                "statement": "会议明确了北极星指标为GPU成本年化减少3533.4万元",
                "observed_at": 1_i64,
            })],
            "window_title:MediaPlayer",
        );
        assert_eq!(gpu_savings.len(), 1);
        assert_eq!(gpu_savings[0].title, "GPU 成本年化节省金额");

        let unattributed_resources = semantic_views_from_statements(
            &[json!({
                "statement": "CPU、内存及各类资源的获取量与饱和度均为 0%",
                "observed_at": 1_i64,
            })],
            "window_title:组织管理",
        );
        assert!(unattributed_resources.is_empty());

        let bare_duration = semantic_views_from_statements(
            &[json!({"statement": "耗时约 5 分钟", "observed_at": 1_i64})],
            "timeline_topic:用户正在设计 MemoryBread 的数据采集与服务端架构\nwindow_title:ChatGPT",
        );
        assert!(bare_duration.is_empty());
    }

    #[test]
    fn builds_explainable_gpu_summary_and_metric_table() {
        let statement = "背景显示国内日均 GPU 利用率为 42%，海外为 47%，但 GPUTL 无法反映硅片内 SM 的实际使用情况，存在掩盖低效的事实";
        let (rows, summary) = semantic_statement(statement, Some(1700000000000)).unwrap();

        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].dimension, "国内");
        assert_eq!(rows[0].metric, "日均 GPU 利用率");
        assert_eq!(rows[0].value, "42%");
        assert_eq!(rows[1].dimension, "海外");
        assert_eq!(rows[1].metric, "日均 GPU 利用率");
        assert_eq!(rows[1].value, "47%");
        assert_eq!(
            summary,
            "日均 GPU 利用率：国内 42%，海外 47%；GPUTL 无法反映硅片内 SM 的实际使用情况，可能掩盖实际低效"
        );

        let (clipped_rows, clipped_summary) = semantic_statement(
            "显示国内日均GPU 利用率为42%，海外为47%，但GPUTL 无法反映硅片内SM的实",
            Some(1700000000000),
        )
        .unwrap();
        assert_eq!(clipped_rows[0].metric, "日均GPU 利用率");
        assert_eq!(clipped_rows[1].metric, "日均GPU 利用率");
        assert!(!clipped_summary.contains("硅片内SM的实"));
    }

    #[test]
    fn keeps_comparison_values_as_semantic_table_rows() {
        let (rows, _) = semantic_statement("本周订单 1200，环比增长 8%", None).unwrap();

        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].metric, "订单");
        assert_eq!(rows[0].value, "1200");
        assert_eq!(rows[1].metric, "环比增长");
        assert_eq!(rows[1].value, "8%");
    }

    #[test]
    fn builds_value_free_title_and_explanatory_summary() {
        let views = semantic_views_from_statements(
            &[json!({
                "statement": "背景显示国内日均 GPU 利用率为 42%，海外为 47%，但 GPUTL 可能掩盖实际低效",
                "observed_at": 1700000000000_i64,
            })],
            "",
        );

        assert_eq!(views.len(), 1);
        assert_eq!(views[0].title, "GPU 利用率对比");
        assert!(!views[0].title.chars().any(|ch| ch.is_ascii_digit()));
        assert!(views[0].summary.starts_with("GPU 利用率对比："));
        assert!(views[0].summary.contains("国内 42%"));
        assert!(views[0].summary.contains("海外 47%"));
        let structured = semantic_view_to_json(views[0].clone());
        assert_eq!(structured["semantic_subject"], "");
        assert_eq!(structured["extraction_version"], DATA_MEMORY_VERSION);
        assert_eq!(structured["title"], "GPU 利用率对比");
    }

    #[test]
    fn normalizes_sentence_fragments_into_concise_metric_titles() {
        let views = semantic_views_from_statements(
            &[json!({
                "statement": "数据显示电商单日 Token 用量从年初的 700，同比增长率高达 55.75%",
                "observed_at": 1700000000000_i64,
            })],
            "",
        );

        assert_eq!(views.len(), 1);
        assert_eq!(views[0].title, "电商单日 Token 用量与同比增长率");
        assert!(!views[0].title.contains("数据显示"));
        assert!(!views[0].title.contains("从年初"));
        assert!(views[0].summary.contains("700"));
        assert!(views[0].summary.contains("55.75%"));

        let cost = semantic_views_from_statements(
            &[json!({
                "statement": "单位 Token 成本降了 80.7%",
            })],
            "",
        );
        assert_eq!(cost[0].title, "单位 Token 成本降幅");
    }

    #[test]
    fn separates_result_actions_from_metric_identity_for_deduplication() {
        let views = semantic_views_from_statements(
            &[
                json!({
                    "statement": "近一周日均发布素材约19.97万条，产生GMV 42.14万元",
                    "observed_at": 2_i64,
                }),
                json!({
                    "statement": "相关素材近一周日均发布19.97万、日均GMV 42.14万元",
                    "observed_at": 1_i64,
                }),
            ],
            "AI 基座中心 AIGC 绩效\napplication:Google Chrome",
        );

        let gmv_views = views
            .iter()
            .filter(|view| view.identity == "gmv")
            .collect::<Vec<_>>();
        assert_eq!(gmv_views.len(), 1);
        assert_eq!(gmv_views[0].title, "AIGC GMV");
        assert_eq!(gmv_views[0].rows[0].metric, "GMV");

        for (actionized, canonical) in [
            ("实现营收", "营收"),
            ("达成订单数", "订单数"),
            ("贡献利润", "利润"),
        ] {
            assert_eq!(
                canonical_identity_metric(actionized),
                canonical_identity_metric(canonical)
            );
        }
    }

    #[test]
    fn adds_application_subject_to_generic_resource_titles() {
        let views = semantic_views_from_statements(
            &[json!({
                "statement": "用户因多人在线编辑切换至只读模式，期间还观察到内存占用较高（约1GB）",
                "observed_at": 1_i64,
            })],
            "商业体系 AI 建设资产复用方案\napplication:Google Chrome",
        );

        assert_eq!(views.len(), 1);
        assert_eq!(views[0].subject, "Google Chrome");
        assert_eq!(views[0].title, "Google Chrome 内存占用");
    }

    #[test]
    fn requires_a_reliable_subject_for_bare_metrics() {
        let rejected = semantic_views_from_statements(
            &[json!({"statement": "内存约1GB", "observed_at": 1_i64})],
            "application:Google Chrome",
        );
        assert!(rejected.is_empty());

        let from_document_name_only = semantic_views_from_statements(
            &[json!({
                "statement": "日均GMV 42.14万元",
                "observed_at": 1_i64,
            })],
            "window_title:AI基座中心绩效26年H1 - 云文档\napplication:Google Chrome",
        );
        assert!(from_document_name_only.is_empty());

        let generic_window = semantic_views_from_statements(
            &[json!({"statement": "日均GMV 42.14万元", "observed_at": 1_i64})],
            "window_title:ChatGPT\napplication:Google Chrome",
        );
        assert!(generic_window.is_empty());
    }

    #[test]
    fn keeps_explicit_object_for_generic_property_metrics() {
        let views = semantic_views_from_statements(
            &[json!({
                "statement": "冯志刚主导的阿里云 paraformer ASR 单价低至 0.000016 元/秒",
                "observed_at": 1_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );

        assert_eq!(views.len(), 1);
        assert_eq!(views[0].subject, "阿里云 paraformer ASR");
        assert_eq!(views[0].title, "阿里云 paraformer ASR 单价");
        assert_eq!(views[0].identity, "阿里云paraformerasr|单价");
        assert!(views[0].summary.contains("阿里云 paraformer ASR 单价"));
        assert!(views[0].summary.contains("0.000016 元/秒"));
    }

    #[test]
    fn rejects_generic_property_metrics_without_an_explicit_object() {
        for statement in [
            "单价低：0.000016元/秒",
            "成本下降近40%",
            "其中包含召回率60%、准确率70%",
            "他们现在一秒钟的成本约为0.3元",
        ] {
            assert!(
                semantic_views_from_statements(
                    &[json!({"statement": statement, "observed_at": 1_i64})],
                    "window_title:商业体系-AI建设资产复用方案 - 云文档",
                )
                .is_empty(),
                "generic property should require an explicit object: {statement}"
            );
        }

        assert!(semantic_views_from_statements(
            &[json!({"statement": "单价低：0.000016元/秒", "observed_at": 1_i64})],
            "用户查看了关于商业体系 AI 建设资产复用的内部文档\ntimeline_topic:用户查看了关于商业体系 AI 建设资产复用的内部文档\nwindow_title:Kim",
        )
        .is_empty());
    }

    #[test]
    fn keeps_same_property_separate_for_different_explicit_objects() {
        let paraformer = semantic_views_from_statements(
            &[json!({"statement": "paraformer ASR 单价为0.000016元/秒"})],
            "",
        );
        let nova = semantic_views_from_statements(
            &[json!({"statement": "Nova ASR 单价为0.00002元/秒"})],
            "",
        );

        assert_eq!(paraformer[0].title, "paraformer ASR 单价");
        assert_eq!(nova[0].title, "Nova ASR 单价");
        assert_ne!(paraformer[0].identity, nova[0].identity);
    }

    #[test]
    fn replaces_document_subject_with_reusable_business_scope_in_place() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, source_app_name, source_window_title, tags,
                        first_seen_at, last_seen_at, last_collected_at, last_success_at,
                        status, created_at, updated_at
                    ) VALUES (
                        62, 'memory:semantic:data-memory.v7:legacy',
                        '审阅商业体系 AI 建设资产复用方案并分析三类资产占比',
                        'work_memory', 'memory_only', 'never', 'observed', 'Google Chrome',
                        '商业体系-AI建设资产复用方案 - 云文档', '["work_memory"]',
                        1, 2, 2, 2, 'active', 1, 2
                    );
                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    ) VALUES (
                        62, 2, 2, 'memory_extract',
                        '总体上，生成与理解两类合计占76%，作为跨BU复用的第一阶段主战场',
                        '{"extraction_version":"data-memory.v7","title":"商业体系 AI 建设资产分类 生成与理解两类合计占比","summary":"商业体系 AI 建设资产分类 生成与理解两类合计占比：生成与理解两类合计占比 76%","semantic_subject":"商业体系 AI 建设资产分类","semantic_identity":"生成与理解两类合计占比","metric_rows":[{"dimension":"","metric":"生成与理解两类合计占比","value":"76%","note":"","statement":"总体上，生成与理解两类合计占76%，作为跨BU复用的第一阶段主战场","observed_at":2}],"metric_statements":[{"statement":"总体上，生成与理解两类合计占76%，作为跨BU复用的第一阶段主战场","observed_at":2}]}',
                        'legacy', 0, '{}', '[]', '[2378]', 'success', 2
                    );
                    "#,
                )?;
                Ok(())
            })
            .unwrap();

        let extraction = storage.regenerate_historical_data_memories(100).unwrap();
        let source = storage.get_data_source(62).unwrap().unwrap();
        let structured = &source.latest_snapshot.as_ref().unwrap().structured_data;

        assert_eq!(extraction.historical_regenerated_count, 1);
        assert_eq!(source.id, 62);
        assert_eq!(structured["extraction_version"], DATA_MEMORY_VERSION);
        assert_eq!(structured["semantic_subject"], "AI 建设资产分类");
        assert_eq!(
            structured["title"],
            "AI 建设资产分类中生成与理解两类合计占比"
        );
        assert!(!structured["summary"]
            .as_str()
            .unwrap()
            .contains("商业体系-AI建设资产复用方案"));
        assert!(structured["summary"]
            .as_str()
            .unwrap()
            .contains("生成与理解两类合计占比 76%"));
        assert_eq!(
            storage
                .regenerate_historical_data_memories(100)
                .unwrap()
                .historical_regenerated_count,
            0
        );
    }

    #[test]
    fn gives_aigc_cost_data_an_explicit_subject_and_unambiguous_dimensions() {
        let statement = "项目背景部分阐述了为突破通用模型效果瓶颈和高推理成本阻碍而发起专项建设的必要性，整体目标包括构建垂类场景优化效率提升50%+、建立标准化评测体系以及通过步数蒸馏等技术实现推理成本下降90%（视频从 0.2降至 0.02元/秒）";
        let views = semantic_views_from_statements(
            &[json!({"statement": statement, "observed_at": 1785665801543_i64})],
            "万擎与电商商业化 AIGC 创新专项",
        );
        assert_eq!(views.len(), 1);
        let view = &views[0];
        assert_eq!(view.subject, "AIGC 垂类场景");
        assert!(view.title.starts_with("AIGC 垂类场景"));
        assert!(view.identity.starts_with("aigc垂类场景|"));
        assert!(view.rows.iter().any(|row| {
            row.dimension == "目标" && row.metric == "优化效率增幅" && row.value == "50%"
        }));
        assert!(view.rows.iter().any(|row| {
            row.dimension == "目标" && row.metric == "视频推理成本降幅" && row.value == "90%"
        }));
        assert!(view.rows.iter().any(|row| {
            row.dimension == "优化前" && row.metric == "视频推理成本" && row.value == "0.2元/秒"
        }));
        assert!(view.rows.iter().any(|row| {
            row.dimension == "优化后" && row.metric == "视频推理成本" && row.value == "0.02元/秒"
        }));
        let structured = semantic_view_to_json(view.clone());
        assert_eq!(structured["semantic_subject"], "AIGC 垂类场景");
        assert_eq!(
            view.rows
                .iter()
                .filter(|row| row.metric == "视频推理成本")
                .count(),
            2
        );
    }

    #[test]
    fn keeps_the_reuse_object_in_cost_saving_titles() {
        let statement =
            "其中11%的场景（如生服模特库在电商AIGC中的复用）已成功合并，节省约6.28万成本";
        let views = semantic_views_from_statements(
            &[json!({"statement": statement, "observed_at": 1785943846089_i64})],
            "timeline_topic:用户查阅了商业体系 AI 建设资产复用方案的云文档\nwindow_title:商业体系-AI建设资产复用方案 - 云文档",
        );

        assert_eq!(views.len(), 1);
        let view = &views[0];
        assert_eq!(view.subject, "生服模特库在电商AIGC 中的复用");
        assert_eq!(view.title, "生服模特库在电商AIGC 中的复用 成本节省金额");
        assert_eq!(view.rows.len(), 1);
        assert_eq!(view.rows[0].metric, "成本节省金额");
        assert_eq!(view.rows[0].value, "6.28万");
        assert!(view.summary.contains("生服模特库在电商AIGC 中的复用"));
        assert!(view.summary.contains("6.28万"));
        assert_eq!(view.identity, "生服模特库在电商aigc中的复用|成本节省金额");

        let direct_outcome = semantic_views_from_statements(
            &[json!({
                "statement": "典型场景：生服模特库、音色库在电商AIGC场景的复用，减少电商关于模特素材的重复采买和AI生成成本约6.28万",
                "observed_at": 1785943846089_i64,
            })],
            "window_title:商业体系-AI建设资产复用方案 - 云文档",
        );
        assert_eq!(direct_outcome.len(), 1);
        assert_eq!(
            direct_outcome[0].title,
            "生服模特库、音色库在电商AIGC 场景的复用 成本节省金额"
        );
        assert_eq!(direct_outcome[0].rows[0].metric, "成本节省金额");
        assert_eq!(direct_outcome[0].rows[0].value, "6.28万");
    }

    #[test]
    fn uses_model_structured_fact_without_rewriting_its_business_context() {
        let evidence =
            "其中11%的场景（如生服模特库在电商AIGC中的复用）已成功合并，节省约6.28万成本";
        let views = semantic_views_from_model_facts(
            &[ModelDataFact {
                title: "生服模特库在电商AIGC中复用的成本节省金额".to_string(),
                subject: "生服模特库".to_string(),
                action: "复用".to_string(),
                target_context: "电商AIGC".to_string(),
                dimension: String::new(),
                metric: "成本节省金额".to_string(),
                value: "6.28".to_string(),
                unit: "万".to_string(),
                statement: "生服模特库在电商AIGC中的复用节省约6.28万成本。".to_string(),
                evidence_quote: evidence.to_string(),
                confidence: "high".to_string(),
                observed_at: Some(1_785_943_846_089),
            }],
            evidence,
            CURRENT_TIMELINE_DATA_FACT_VERSION,
        );

        assert_eq!(views.len(), 1);
        assert_eq!(views[0].title, "生服模特库在电商AIGC中复用的成本节省金额");
        assert_eq!(views[0].subject, "生服模特库");
        assert_eq!(views[0].rows[0].metric, "成本节省金额");
        assert_eq!(views[0].rows[0].value, "6.28万");
        assert_eq!(
            semantic_view_to_json(views[0].clone())["semantic_origin"],
            "model_structured_fact"
        );
    }

    #[test]
    fn keeps_arbitrary_validated_model_metrics_without_domain_hint_lists() {
        let structured = json!({
            "extraction_version": DATA_MEMORY_VERSION,
            "semantic_origin": "model_structured_fact",
            "title": "服务质量概览 P95 处理延迟",
            "summary": "服务质量概览的 P95 处理延迟为 18.6 ms",
            "semantic_subject": "服务质量概览",
            "semantic_identity": "服务质量概览|p95处理延迟",
            "metric_rows": [{
                "dimension": "当前",
                "metric": "P95 处理延迟",
                "value": "18.6ms",
                "note": "",
                "statement": "服务质量概览当前 P95 处理延迟为 18.6 ms。",
                "observed_at": 1_i64
            }],
            "metric_statements": []
        });

        let view = semantic_view_from_existing_v3(&structured).expect("validated model fact");
        assert_eq!(view.rows.len(), 1);
        assert_eq!(view.rows[0].metric, "P95 处理延迟");
    }

    #[test]
    fn model_fact_generic_anchor_requires_a_specific_scene() {
        let evidence = "duration Kling 3.0 支持 3-15 秒";
        let generic = ModelDataFact {
            title: "Kling 3.0 模型支持时长范围".to_string(),
            subject: "duration".to_string(),
            action: String::new(),
            target_context: "视频参数配置".to_string(),
            dimension: String::new(),
            metric: "支持时长范围".to_string(),
            value: "3-15".to_string(),
            unit: "秒".to_string(),
            statement: "Kling 3.0 支持生成视频时长3到15秒。".to_string(),
            evidence_quote: evidence.to_string(),
            confidence: "high".to_string(),
            observed_at: Some(1),
        };
        assert!(semantic_views_from_model_facts(
            std::slice::from_ref(&generic),
            evidence,
            CURRENT_TIMELINE_DATA_FACT_VERSION,
        )
        .is_empty());

        let contextual = ModelDataFact {
            target_context: "MemoryBread 官网首屏静音背景视频生成".to_string(),
            ..generic
        };
        assert_eq!(
            semantic_views_from_model_facts(
                &[contextual],
                evidence,
                CURRENT_TIMELINE_DATA_FACT_VERSION,
            )
            .len(),
            1
        );

        let execution_shell = ModelDataFact {
            title: "vedio-aigc系统请求参数时长".to_string(),
            subject: "15秒".to_string(),
            action: "配置".to_string(),
            target_context: "Kling 3.0模型生成控制".to_string(),
            dimension: String::new(),
            metric: "请求参数时长".to_string(),
            value: "15".to_string(),
            unit: "秒".to_string(),
            statement: "vedio-aigc系统请求参数时长为15秒。".to_string(),
            evidence_quote: "15秒 Kling 3.0模型生成控制".to_string(),
            confidence: "high".to_string(),
            observed_at: Some(1),
        };
        assert!(semantic_views_from_model_facts(
            &[execution_shell],
            "15秒 Kling 3.0模型生成控制",
            CURRENT_TIMELINE_DATA_FACT_VERSION,
        )
        .is_empty());

        let concrete_config = ModelDataFact {
            title: "MemoryBread 官网首屏视频生成时长".to_string(),
            subject: "MemoryBread 官网首屏视频".to_string(),
            action: "配置".to_string(),
            target_context: "MemoryBread 官网首屏视频参数配置".to_string(),
            dimension: String::new(),
            metric: "生成时长".to_string(),
            value: "15".to_string(),
            unit: "秒".to_string(),
            statement: "MemoryBread 官网首屏视频生成时长为15秒。".to_string(),
            evidence_quote: "MemoryBread 官网首屏视频已配置为15秒".to_string(),
            confidence: "high".to_string(),
            observed_at: Some(1),
        };
        assert_eq!(
            semantic_views_from_model_facts(
                &[concrete_config],
                "MemoryBread 官网首屏视频已配置为15秒",
                CURRENT_TIMELINE_DATA_FACT_VERSION,
            )
            .len(),
            1
        );
    }

    #[test]
    fn current_fact_contract_never_falls_back_to_narrative_statements() {
        let context = TimelineDataContext {
            capture_ids: vec![1],
            source_urls: Vec::new(),
            observed_at: 1,
            metric_statements: vec![json!({
                "statement": "AIGC成本节省约6.28万",
                "observed_at": 1,
            })],
            model_fact_contract: Some(CURRENT_TIMELINE_DATA_FACT_VERSION.to_string()),
            model_facts: Vec::new(),
            evidence_text: "AIGC成本节省约6.28万".to_string(),
        };

        assert!(semantic_views_for_timeline_context(&context, "AIGC").is_empty());

        let legacy_context = TimelineDataContext {
            capture_ids: vec![1],
            source_urls: Vec::new(),
            observed_at: 1,
            metric_statements: vec![json!({
                "statement": "AIGC成本节省约6.28万",
                "observed_at": 1,
            })],
            model_fact_contract: Some("timeline-data-fact.v2".to_string()),
            model_facts: Vec::new(),
            evidence_text: "AIGC成本节省约6.28万".to_string(),
        };
        assert!(!semantic_views_for_timeline_context(&legacy_context, "AIGC").is_empty());

        // 存在合法模型事实时仍优先使用模型事实，不混入语句路径产物。
        let evidence =
            "其中11%的场景（如生服模特库在电商AIGC中的复用）已成功合并，节省约6.28万成本";
        let with_facts = TimelineDataContext {
            model_facts: vec![ModelDataFact {
                title: "成本节省金额".to_string(),
                subject: "生服模特库".to_string(),
                action: String::new(),
                target_context: String::new(),
                dimension: String::new(),
                metric: "成本节省金额".to_string(),
                value: "6.28".to_string(),
                unit: "万".to_string(),
                statement: "生服模特库复用节省约6.28万成本。".to_string(),
                evidence_quote: evidence.to_string(),
                confidence: "high".to_string(),
                observed_at: Some(1),
            }],
            evidence_text: evidence.to_string(),
            ..context
        };
        let views = semantic_views_for_timeline_context(&with_facts, "AIGC");
        assert_eq!(views.len(), 1);
        assert_eq!(
            semantic_view_to_json(views[0].clone())["semantic_origin"],
            "model_structured_fact"
        );
    }

    #[test]
    fn keeps_named_plan_as_subject_and_rejects_preflight_thresholds() {
        let plan_views = semantic_views_from_statements(
            &[
                json!({"statement": "Sync Standard 每用户每月 4 美元，按年计费，提供 1GB 存储", "observed_at": 1_i64}),
                json!({"statement": "Sync Plus 每用户每月 8 美元，按年计费，提供 10GB 存储及更多版本历史", "observed_at": 1_i64}),
            ],
            "window_title:Obsidian - 磨砺你的思维",
        );
        assert_eq!(plan_views.len(), 2);
        assert!(plan_views.iter().any(|view| {
            view.subject == "Sync Standard"
                && view.title == "Sync Standard 存储容量"
                && view.rows.iter().any(|row| row.value == "1GB")
        }));
        assert!(plan_views.iter().any(|view| {
            view.subject == "Sync Plus"
                && view.title == "Sync Plus 存储容量"
                && view.rows.iter().any(|row| row.value == "10GB")
        }));
        assert_eq!(
            reliable_source_title("Obsidian - 磨砺你的思维").as_deref(),
            Some("Obsidian")
        );
        assert!(reliable_subject_label("率小于30%、http等相关核心").is_none());

        let checklist = "涉及业务开关操作、HB2数据前置构建、数据一致性和数据补偿等方面预案整理。切换前检查清单：源机房服务正常，错误率 < 1%，P99 < 10s；HB2 机房容量评估已完成，CPU使用率 < 40%，rpc线程池使用率小于30%。";
        assert!(semantic_views_from_statements(
            &[json!({"statement": checklist, "observed_at": 1_i64})],
            "window_title:电商北驰切流-基座中心预案梳理【网关-运行域】 - 云文档",
        )
        .is_empty());
    }

    #[test]
    fn rejects_same_metric_and_dimension_with_conflicting_values() {
        assert!(semantic_statement("整体成本为 0.2 元，整体成本为 0.3 元", None).is_none());
    }

    #[test]
    fn rejects_negated_examples_and_list_ordinals_from_data_memory() {
        assert!(!is_concrete_data_statement(
            "例如年度额度1,747、已交付1,736，并不是文档中的 GPU 利用率 42%、47%、约 25% 等数据"
        ));
        assert!(!is_concrete_data_statement(
            "21、aigc gmv从不到十万，干到了百万"
        ));
        assert!(!is_concrete_data_statement("全域巡检机器人：2商业化收入⋯"));
        assert!(!is_concrete_data_statement("GPU 利用率 42%、47%、25%"));
        assert!(!is_concrete_data_statement(
            "比如背景显示国内日均 GPU 利用率为 42%，海外为 47%"
        ));
        assert!(!is_concrete_data_statement("协议同时支持 IPv4 和 IPv6"));
        assert!(!is_concrete_data_statement("ID 440"));
        assert!(!is_concrete_data_statement(
            "随后用户点击进入了一份名为 2026-07-29 的文档"
        ));
        assert!(!is_concrete_data_statement(
            "truncated_json：不要把输出预算直接降到4096"
        ));
        assert!(!is_concrete_data_statement("建议把重试次数调到3次"));
        assert!(!is_concrete_data_statement("我先把qps调到1看看"));
        assert!(!is_concrete_data_statement("我们先将并发数设置为32试试"));
        assert!(!is_concrete_data_statement("麻烦帮我把超时时间改成5秒"));
        assert!(!is_concrete_data_statement("计划把缓存容量提升到8GB"));
        assert!(!semantic_metric_row_is_plausible(&SemanticMetricRow {
            dimension: String::new(),
            metric: "我先把qps".to_string(),
            value: "1".to_string(),
            note: String::new(),
            statement: "我先把qps调到1看看".to_string(),
            observed_at: None,
        }));
        assert!(!is_concrete_data_statement("内存不应超过1GB"));
        assert!(!is_concrete_data_statement("请将并发数设置为32"));
        assert!(is_concrete_data_statement("当前输出预算为4096"));
        assert!(is_concrete_data_statement(
            "QPS 已调整到 100，当前错误率为 0.2%"
        ));
        assert!(is_concrete_data_statement("AI 建议采纳率为80%"));
    }

    #[test]
    fn same_semantic_data_from_different_timelines_updates_one_latest_snapshot() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, ax_text,
                        is_sensitive, pii_scrubbed
                    ) VALUES
                        (1, 1700000000000, 'Docs', 'GPU 周报', 'manual',
                         '国内日均 GPU 利用率为 42%，海外为 47%', 0, 0),
                        (2, 1700000060000, 'Docs', 'GPU 周报', 'manual',
                         '国内日均 GPU 利用率为 55%，海外为 60%', 0, 0);
                    INSERT INTO timelines (
                        id, capture_id, summary, overview, details, entities, category,
                        importance, created_at_ms, updated_at_ms
                    ) VALUES
                        (11, 1, '容器云 GPU 周报', '国内日均 GPU 利用率为 42%，海外为 47%',
                         '', '[]', 'work', 4, 1700000000000, 1700000000000),
                        (12, 2, '容器云 GPU 周报', '国内日均 GPU 利用率为 55%，海外为 60%',
                         '', '[]', 'work', 4, 1700000060000, 1700000060000);
                    UPDATE captures SET timeline_id = 10 + id WHERE id IN (1, 2);
                    "#,
                )?;
                Ok(())
            })
            .unwrap();

        let extraction = storage.extract_data_candidates(100).unwrap();
        let (sources, total) = storage.list_data_sources(None, 20, 0).unwrap();

        assert_eq!(total, 1);
        assert_eq!(sources.len(), 1);
        assert_eq!(extraction.source_created_count, 1);
        assert_eq!(extraction.source_updated_count, 1);
        let snapshot = sources[0].latest_snapshot.as_ref().unwrap();
        assert_eq!(snapshot.structured_data["title"], "GPU 利用率对比");
        assert_eq!(sources[0].source_window_title.as_deref(), Some("GPU 周报"));
        assert!(snapshot.structured_data["summary"]
            .as_str()
            .unwrap()
            .contains("国内 55%"));
        assert_eq!(snapshot.source_timeline_ids, vec![11, 12]);
    }

    #[test]
    fn regenerates_and_merges_historical_v2_data() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, source_window_title, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                    ) VALUES
                        (1, 'memory:timeline:11', '旧 GPU 数据', 'work_memory', 'memory_only',
                         'never', 'observed', 'GPU 周报', '["work_memory"]', 1, 1, 'active', 1, 1),
                        (2, 'memory:timeline:12', '新 GPU 数据', 'work_memory', 'memory_only',
                         'never', 'observed', 'GPU 周报', '["work_memory"]', 2, 2, 'active', 2, 2),
                        (3, 'memory:timeline:13', '误识别数据', 'work_memory', 'memory_only',
                         'never', 'observed', 'GPU 周报', '["work_memory"]', 3, 3, 'active', 3, 3);
                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    ) VALUES
                        (1, 1, 1, 'memory_extract', '国内日均 GPU 利用率为 42%，海外为 47%',
                         '{"extraction_version":"data-memory.v2","metric_statements":[{"statement":"国内日均 GPU 利用率为 42%，海外为 47%","observed_at":1}]}',
                         'old', 0, '{}', '[]', '[11]', 'success', 1),
                        (2, 2, 2, 'memory_extract', '国内日均 GPU 利用率为 55%，海外为 60%',
                         '{"extraction_version":"data-memory.v2","metric_statements":[{"statement":"国内日均 GPU 利用率为 55%，海外为 60%","observed_at":2}]}',
                         'new', 0, '{}', '[]', '[12]', 'success', 2),
                        (3, 3, 3, 'memory_extract', '并不是文档中的 GPU 利用率 42%、47%、25% 等数据；旧内容提到 data-memory.v3',
                         '{"extraction_version":"data-memory.v2","metric_statements":[{"statement":"并不是文档中的 GPU 利用率 42%、47%、25% 等数据；旧内容提到 data-memory.v3","observed_at":3}]}',
                         'noise', 0, '{}', '[]', '[13]', 'success', 3);
                    "#,
                )?;
                Ok(())
            })
            .unwrap();

        let extraction = storage.extract_data_candidates(100).unwrap();
        let (sources, total) = storage.list_data_sources(None, 20, 0).unwrap();
        let rejected_version = storage
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT json_extract(structured_data, '$.extraction_version')
                     FROM data_snapshots WHERE source_id = 3",
                    [],
                    |row| row.get::<_, String>(0),
                )
                .map_err(Into::into)
            })
            .unwrap();

        assert_eq!(extraction.historical_regenerated_count, 3);
        assert_eq!(extraction.historical_merged_count, 1);
        assert_eq!(extraction.historical_rejected_count, 1);
        assert_eq!(total, 1);
        assert_eq!(sources.len(), 1);
        assert!(
            sources[0].latest_snapshot.as_ref().unwrap().structured_data["summary"]
                .as_str()
                .unwrap()
                .contains("国内 55%")
        );
        assert_eq!(rejected_version, DATA_MEMORY_VERSION);

        let second_extraction = storage.extract_data_candidates(100).unwrap();
        assert_eq!(second_extraction.historical_regenerated_count, 0);
        assert_eq!(second_extraction.historical_merged_count, 0);
        assert_eq!(second_extraction.historical_rejected_count, 0);
    }

    #[test]
    fn optional_regenerates_data_memory_e2e_database() {
        let Ok(db_path) = std::env::var("MEMORY_BREAD_DATA_REGEN_E2E_DB") else {
            return;
        };
        let storage = StorageManager::open(std::path::Path::new(&db_path)).unwrap();
        let (_, visible_before) = storage.list_data_sources(None, 5000, 0).unwrap();
        let mut regenerated = 0;
        let mut merged = 0;
        let mut rejected = 0;
        for _ in 0..20 {
            let summary = storage.regenerate_historical_data_memories(5000).unwrap();
            regenerated += summary.historical_regenerated_count;
            merged += summary.historical_merged_count;
            rejected += summary.historical_rejected_count;
            if summary.historical_regenerated_count == 0 {
                break;
            }
        }
        let (records, total) = storage.list_data_sources(None, 5000, 0).unwrap();
        let invalid_count = records
            .iter()
            .filter(|source| {
                source
                    .latest_snapshot
                    .as_ref()
                    .and_then(|snapshot| snapshot.structured_data.get("title"))
                    .and_then(Value::as_str)
                    .map(str::is_empty)
                    .unwrap_or(true)
            })
            .count();
        println!(
            "data-memory-v15 e2e: regenerated={} merged={} rejected={} visible_before={} visible_after={} invalid_visible={}",
            regenerated,
            merged,
            rejected,
            visible_before,
            total,
            invalid_count,
        );
        assert_eq!(invalid_count, 0);
    }

    #[test]
    fn upgrades_and_merges_legacy_sources_after_current_model_facts_arrive() {
        let storage = StorageManager::open_in_memory().unwrap();
        let evidence = "MemoryBread 官网首页视觉与文案优化已完成，任务总耗时约16分31秒";
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, ocr_text, timeline_id
                     ) VALUES (700, 1700000000000, 'ChatGPT', 'ChatGPT', 'auto', ?1, 500)",
                    [evidence],
                )?;
                conn.execute(
                    "INSERT INTO timelines (
                        id, capture_id, capture_ids, summary, overview, details,
                        observed_at, created_at_ms, updated_at_ms
                     ) VALUES (
                        500, 700, '[700]',
                        '用户完成 MemoryBread 官网首页视觉与文案优化工作',
                        'MemoryBread 官网首页视觉与文案优化已经完成。',
                        ?1, 1700000000000, 1700000000000, 1700000000000
                     )",
                    [evidence],
                )?;
                for (id, title) in [(1_i64, "整个任务耗时"), (2_i64, "任务总耗时")] {
                    conn.execute(
                        "INSERT INTO data_sources (
                            id, canonical_key, title, source_kind, access_mode, refresh_policy,
                            realtime_level, source_app_name, source_window_title, tags,
                            first_seen_at, last_seen_at, status, created_at, updated_at
                         ) VALUES (
                            ?1, ?2, ?3, 'work_memory', 'memory_only', 'never', 'observed',
                            'ChatGPT', 'ChatGPT', '[\"work_memory\"]', 1700000000000,
                            1700000000000, 'active', 1700000000000, 1700000000000
                         )",
                        params![id, format!("legacy:{id}"), title],
                    )?;
                    conn.execute(
                        "INSERT INTO data_snapshots (
                            source_id, collected_at, observed_at, collector, content_text,
                            structured_data, content_hash, provenance, source_capture_ids,
                            source_timeline_ids, status, created_at
                         ) VALUES (
                            ?1, 1700000000000, 1700000000000, 'memory_extract',
                            '任务总耗时约16分31秒', ?2, ?3, '{}', '[700]', '[500]',
                            'success', 1700000000000
                         )",
                        params![
                            id,
                            json!({
                                "extraction_version": DATA_MEMORY_VERSION,
                                "semantic_origin": "legacy_parser",
                                "title": title,
                                "summary": format!("{title}：31秒"),
                                "semantic_subject": "",
                                "semantic_identity": title,
                                "metric_rows": [{
                                    "dimension": "",
                                    "metric": title,
                                    "value": "31秒",
                                    "note": "",
                                    "statement": "任务总耗时约16分31秒",
                                    "observed_at": 1700000000000_i64
                                }],
                                "metric_statements": []
                            })
                            .to_string(),
                            format!("legacy-hash-{id}"),
                        ],
                    )?;
                }
                conn.execute(
                    "INSERT INTO timeline_data_fact_runs (
                        timeline_id, contract_version, accepted_count, rejected_count,
                        created_at, updated_at
                     ) VALUES (500, ?1, 1, 0, 1700000000000, 1700000000000)",
                    [CURRENT_TIMELINE_DATA_FACT_VERSION],
                )?;
                conn.execute(
                    "INSERT INTO timeline_data_facts (
                        timeline_id, fact_key, title, subject, action, target_context,
                        dimension, metric, value, unit, statement, evidence_quote,
                        confidence, observed_at, source_capture_ids, created_at, updated_at
                     ) VALUES (
                        500, 'task-duration',
                        'MemoryBread 官网首页视觉与文案优化任务耗时',
                        'MemoryBread 官网首页视觉与文案优化', '优化', '官网首页', '',
                        '任务耗时', '16分31秒', '',
                        'MemoryBread 官网首页视觉与文案优化任务耗时为16分31秒。',
                        ?1, 'high', 1700000000000, '[700]', 1700000000000, 1700000000000
                     )",
                    [evidence],
                )?;
                Ok(())
            })
            .unwrap();

        let summary = storage.regenerate_historical_data_memories(100).unwrap();
        let (sources, total) = storage.list_data_sources(None, 20, 0).unwrap();

        assert_eq!(summary.historical_regenerated_count, 2);
        assert_eq!(summary.historical_merged_count, 1);
        assert_eq!(total, 1);
        assert_eq!(sources[0].id, 1);
        assert_eq!(
            sources[0].title,
            "MemoryBread 官网首页视觉与文案优化任务耗时"
        );
        let structured = &sources[0].latest_snapshot.as_ref().unwrap().structured_data;
        assert_eq!(structured["semantic_origin"], "model_structured_fact");
        assert_eq!(structured["metric_rows"][0]["value"], "16分31秒");
    }

    #[test]
    fn reuses_linked_legacy_sources_when_one_timeline_yields_multiple_scenes() {
        let storage = StorageManager::open_in_memory().unwrap();
        let evidence = "MemoryBread 产品界面布局；生成一支15秒、16:9的动画，作为桌面软件官网首屏的静音背景视频";
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, ocr_text, timeline_id
                     ) VALUES (700, 1700000000000, 'Chrome', '视频生成', 'auto', ?1, 500)",
                    [evidence],
                )?;
                conn.execute(
                    "INSERT INTO timelines (
                        id, capture_id, capture_ids, summary, overview, details,
                        observed_at, created_at_ms, updated_at_ms
                     ) VALUES (
                        500, 700, '[700]', '生成官网首屏静音背景视频',
                        '为 MemoryBread 官网生成首屏视频。', ?1,
                        1700000000000, 1700000000000, 1700000000000
                     )",
                    [evidence],
                )?;
                for (id, title, content) in [
                    (
                        1_i64,
                        "用户设定了视频时长",
                        "用户设定了视频时长为15秒，画幅比例为横屏16:9",
                    ),
                    (
                        2_i64,
                        "随后用户配置了具体的生成参数：时长设",
                        "生成参数中的视频时长为15秒，画幅比例为横屏16:9",
                    ),
                    (
                        3_i64,
                        "SUCCEED 系统接收请求后处理耗时",
                        "系统接收请求后处理耗时301秒，任务结果为SUCCEED",
                    ),
                ] {
                    conn.execute(
                        "INSERT INTO data_sources (
                            id, canonical_key, title, source_kind, access_mode, refresh_policy,
                            realtime_level, source_app_name, source_window_title, tags,
                            first_seen_at, last_seen_at, status, created_at, updated_at
                         ) VALUES (
                            ?1, ?2, ?3, 'work_memory', 'memory_only', 'never', 'observed',
                            'Chrome', '视频生成', '[\"work_memory\"]', 1700000000000,
                            1700000000000, 'active', 1700000000000, 1700000000000
                         )",
                        params![id, format!("legacy:{id}"), title],
                    )?;
                    conn.execute(
                        "INSERT INTO data_snapshots (
                            source_id, collected_at, observed_at, collector, content_text,
                            structured_data, content_hash, provenance, source_capture_ids,
                            source_timeline_ids, status, created_at
                         ) VALUES (
                            ?1, 1700000000000, 1700000000000, 'memory_extract', ?2,
                            ?3, ?4, '{}', '[700]', '[500]', 'success', 1700000000000
                         )",
                        params![
                            id,
                            content,
                            json!({
                                "extraction_version": DATA_MEMORY_VERSION,
                                "semantic_origin": "legacy_parser",
                                "title": title,
                                "summary": title,
                                "semantic_subject": "",
                                "semantic_identity": title,
                                "metric_rows": [],
                                "metric_statements": []
                            })
                            .to_string(),
                            format!("legacy-hash-{id}"),
                        ],
                    )?;
                }
                conn.execute(
                    "INSERT INTO timeline_data_fact_runs (
                        timeline_id, contract_version, accepted_count, rejected_count,
                        created_at, updated_at
                     ) VALUES (500, ?1, 2, 0, 1700000000000, 1700000000000)",
                    [CURRENT_TIMELINE_DATA_FACT_VERSION],
                )?;
                for (key, title, metric, value, unit, statement) in [
                    (
                        "duration",
                        "MemoryBread 官网首屏静音背景视频生成时长",
                        "生成时长",
                        "15",
                        "秒",
                        "基于 MemoryBread 产品界面布局生成的官网首屏静音背景视频时长为15秒。",
                    ),
                    (
                        "aspect-ratio",
                        "MemoryBread 官网首屏静音背景视频画幅比例",
                        "画幅比例",
                        "16:9",
                        "",
                        "基于 MemoryBread 产品界面布局生成的官网首屏静音背景视频画幅比例为16:9。",
                    ),
                ] {
                    conn.execute(
                        "INSERT INTO timeline_data_facts (
                            timeline_id, fact_key, title, subject, action, target_context,
                            dimension, metric, value, unit, statement, evidence_quote,
                            confidence, observed_at, source_capture_ids, created_at, updated_at
                         ) VALUES (
                            500, ?1, ?2, 'MemoryBread 产品界面布局', '生成',
                            '桌面软件官网首屏的静音背景视频', '', ?3, ?4, ?5, ?6,
                            ?7, 'high', 1700000000000, '[700]',
                            1700000000000, 1700000000000
                         )",
                        params![key, title, metric, value, unit, statement, evidence],
                    )?;
                }
                Ok(())
            })
            .unwrap();

        let summary = storage.regenerate_historical_data_memories(100).unwrap();
        storage
            .with_conn(|conn| {
                for source_id in [1_i64, 2_i64] {
                    let snapshot = raw_latest_snapshot(conn, source_id)?.unwrap();
                    assert!(
                        semantic_view_from_existing_v3(&snapshot.structured_data).is_some(),
                        "source {source_id} is not self-contained: {}",
                        snapshot.structured_data
                    );
                }
                Ok(())
            })
            .unwrap();
        let (mut sources, total) = storage.list_data_sources(None, 20, 0).unwrap();
        sources.sort_by_key(|source| source.id);

        assert_eq!(summary.historical_regenerated_count, 2);
        assert_eq!(summary.historical_merged_count, 0);
        assert_eq!(total, 3);
        assert_eq!(sources[0].id, 1);
        assert_eq!(sources[0].title, "MemoryBread 官网首屏静音背景视频生成时长");
        assert_eq!(sources[1].id, 2);
        assert_eq!(sources[1].title, "MemoryBread 官网首屏静音背景视频画幅比例");
        assert!(sources[..2].iter().all(|source| {
            source.latest_snapshot.as_ref().is_some_and(|snapshot| {
                snapshot.structured_data["semantic_origin"] == "model_structured_fact"
            })
        }));
        assert_eq!(sources[2].id, 3);
        assert_eq!(sources[2].title, "SUCCEED 系统接收请求后处理耗时");
        assert_eq!(
            sources[2].latest_snapshot.as_ref().unwrap().structured_data["semantic_origin"],
            "legacy_parser"
        );
    }

    #[test]
    fn legacy_view_matching_requires_metric_signal_when_values_collide() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                     ) VALUES (
                        1, 'legacy:api-latency', '接口响应耗时', 'work_memory',
                        'memory_only', 'never', 'observed', '[\"work_memory\"]',
                        1, 1, 'active', 1, 1
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, provenance, source_capture_ids,
                        source_timeline_ids, status, created_at
                     ) VALUES (
                        1, 1, 1, 'memory_extract', '视频生成接口响应耗时15秒',
                        '{\"semantic_origin\":\"legacy_parser\"}', 'legacy-api', '{}',
                        '[]', '[500]', 'success', 1
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();
        let snapshot = storage
            .with_conn(|conn| raw_latest_snapshot(conn, 1))
            .unwrap()
            .unwrap();
        let view_for_metric = |metric: &str| SemanticDataView {
            title: metric.to_string(),
            subject: "视频生成".to_string(),
            identity: metric.to_string(),
            summary: format!("{metric}为15秒"),
            rows: vec![SemanticMetricRow {
                dimension: String::new(),
                metric: metric.to_string(),
                value: "15秒".to_string(),
                note: String::new(),
                statement: format!("{metric}为15秒"),
                observed_at: Some(1),
            }],
            statements: Vec::new(),
            latest_observed_at: Some(1),
        };

        assert!(!semantic_view_matches_legacy_snapshot(
            &snapshot,
            &view_for_metric("视频生成时长"),
        ));
        assert!(semantic_view_matches_legacy_snapshot(
            &snapshot,
            &view_for_metric("接口响应耗时"),
        ));
        assert!(value_is_colon_ratio("16:9"));
        assert!(value_is_colon_ratio("16：9"));
        assert!(!value_is_colon_ratio("12:30:45"));
    }

    #[test]
    fn optional_regenerates_selected_data_memory_sources() {
        let Ok(db_path) = std::env::var("MEMORY_BREAD_DATA_REGEN_SELECTED_DB") else {
            return;
        };
        let raw_ids = std::env::var("MEMORY_BREAD_DATA_REGEN_SELECTED_IDS")
            .expect("selected regeneration requires explicit source ids");
        let mut source_ids = raw_ids
            .split(',')
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| value.parse::<i64>().expect("source id must be an integer"))
            .collect::<Vec<_>>();
        assert!(!source_ids.is_empty() && source_ids.len() <= 50);
        let original_len = source_ids.len();
        let mut seen = HashSet::new();
        source_ids.retain(|source_id| seen.insert(*source_id));
        assert_eq!(source_ids.len(), original_len, "source ids must be unique");

        let storage = StorageManager::open(std::path::Path::new(&db_path)).unwrap();
        let summary = storage
            .with_conn(|conn| {
                let existing_count = source_ids.iter().try_fold(0usize, |count, source_id| {
                    let exists = conn.query_row(
                        "SELECT COUNT(*) > 0 FROM data_sources WHERE id = ?1 AND deleted_at IS NULL",
                        [source_id],
                        |row| row.get::<_, bool>(0),
                    )?;
                    Ok::<_, StorageError>(count + usize::from(exists))
                })?;
                if existing_count != source_ids.len() {
                    return Err(StorageError::NotFound(
                        "one or more selected data sources are missing or deleted".to_string(),
                    ));
                }

                conn.execute_batch("SAVEPOINT regenerate_selected_data_memory")?;
                for source_id in &source_ids {
                    conn.execute(
                        "UPDATE data_snapshots
                         SET structured_data = json_set(
                             structured_data,
                             '$.extraction_version',
                             'data-memory.force-selected-regeneration'
                         )
                         WHERE source_id = ?1",
                        [source_id],
                    )?;
                }
                match regenerate_legacy_data_memories_inner(conn, &source_ids) {
                    Ok(summary) => {
                        for source_id in &source_ids {
                            conn.execute(
                                "UPDATE data_snapshots
                                 SET structured_data = json_set(
                                     structured_data,
                                     '$.extraction_version',
                                     ?2
                                 )
                                 WHERE source_id = ?1
                                   AND json_extract(
                                       structured_data,
                                       '$.extraction_version'
                                   ) = 'data-memory.force-selected-regeneration'",
                                params![source_id, DATA_MEMORY_VERSION],
                            )?;
                        }
                        conn.execute_batch("RELEASE SAVEPOINT regenerate_selected_data_memory")?;
                        Ok(summary)
                    }
                    Err(error) => {
                        let _ = conn.execute_batch(
                            "ROLLBACK TO SAVEPOINT regenerate_selected_data_memory;
                             RELEASE SAVEPOINT regenerate_selected_data_memory;",
                        );
                        Err(error)
                    }
                }
            })
            .unwrap();
        println!(
            "selected data-memory-v15 regeneration: requested={} regenerated={} merged={} rejected={}",
            source_ids.len(),
            summary.regenerated_count,
            summary.merged_count,
            summary.rejected_count,
        );
        assert_eq!(summary.regenerated_count, source_ids.len());
    }

    #[test]
    fn deleted_data_stays_hidden_and_is_not_automatically_restored() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO data_sources (
                        canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                     ) VALUES (
                        'memory:timeline:88', 'GPU 指标', 'work_memory', 'memory_only',
                        'never', 'observed', '[\"work_memory\"]', 1700000000000,
                        1700000000000, 'active', 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                     ) VALUES (
                        1, 1700000000000, 1700000000000, 'memory_extract',
                        '国内日均 GPU 利用率为 42%',
                        '{\"metric_statements\":[{\"statement\":\"国内日均 GPU 利用率为 42%\"}]}',
                        'gpu-hash', 0, '{}', '[]', '[88]', 'success', 1700000000000
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        assert_eq!(storage.list_data_sources(None, 20, 0).unwrap().1, 1);
        assert!(storage.delete_data_source(1).unwrap());
        assert_eq!(storage.list_data_sources(None, 20, 0).unwrap().1, 0);
        assert!(storage.get_data_source(1).unwrap().is_none());
    }

    #[test]
    fn historical_ui_noise_is_not_listed_or_recalled_as_data() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO data_sources (
                        canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                     ) VALUES (
                        'memory:timeline:99', '聊天界面', 'work_memory', 'memory_only',
                        'never', 'observed', '[\"work_memory\"]', 1700000000000,
                        1700000000000, 'active', 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                     ) VALUES (
                        1, 1700000000000, 1700000000000, 'memory_extract',
                        '用户在 2026 年 8 月 1 日 13:29 打开访达',
                        '{\"metric_statements\":[{\"statement\":\"用户在 2026 年 8 月 1 日 13:29 打开访达\"}]}',
                        'noise-hash', 0, '{}', '[]', '[99]', 'success', 1700000000000
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        let (listed, total) = storage.list_data_sources(None, 20, 0).unwrap();
        let recalled = storage
            .search_data_sources("访达", false, 1700000001000, 10)
            .unwrap();

        assert_eq!(total, 0);
        assert!(listed.is_empty());
        assert!(recalled.is_empty());
    }

    #[test]
    fn data_search_can_return_the_configured_default_of_thirty_results() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                for id in 1..=35_i64 {
                    conn.execute(
                        "INSERT INTO data_sources (
                            id, canonical_key, title, source_kind, source_url, access_mode,
                            refresh_policy, realtime_level, tags, first_seen_at, last_seen_at,
                            status, created_at, updated_at
                         ) VALUES (
                            ?1, ?2, ?3, 'report_url', ?4, 'browser_session',
                            'on_demand', 'live', '[\"report\"]', 1, 1,
                            'active', 1, 1
                         )",
                        params![
                            id,
                            format!("report:https://bi.example.com/metrics/{id}"),
                            format!("经营指标看板 {id}"),
                            format!("https://bi.example.com/metrics/{id}"),
                        ],
                    )?;
                }
                Ok(())
            })
            .unwrap();

        let results = storage
            .search_data_sources("经营指标", false, 1_700_000_000_000, 30)
            .unwrap();

        assert_eq!(results.len(), 30);
    }

    #[test]
    fn canonical_url_keeps_filters_and_spa_route_but_drops_credentials() {
        assert_eq!(
            canonical_data_url("https://bi.example.com/report/?team=a&access_token=secret#chart")
                .as_deref(),
            Some("https://bi.example.com/report?team=a#chart")
        );
        assert!(canonical_data_url("file:///tmp/report.html").is_none());
    }

    #[test]
    fn freshness_weights_live_reports_and_decays_work_memory() {
        assert_eq!(freshness_for("report_url", 60), ("live", 1.0));
        assert_eq!(freshness_for("report_url", 2 * 24 * 3600).0, "stale");
        assert!(
            freshness_for("work_memory", 2 * 24 * 3600).1
                > freshness_for("work_memory", 40 * 24 * 3600).1
        );
    }

    #[test]
    fn browser_attach_snapshot_is_accepted_by_the_migration_contract() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO data_sources (
                        canonical_key, title, source_kind, source_url, access_mode,
                        refresh_policy, realtime_level, first_seen_at, last_seen_at,
                        created_at, updated_at
                     ) VALUES (
                        'url:https://bi.example.com/dashboard', '经营看板', 'report_url',
                        'https://bi.example.com/dashboard', 'browser_session', 'on_demand',
                        'live', 1700000000000, 1700000000000, 1700000000000, 1700000000000
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        let snapshot = storage
            .save_data_snapshot(
                1,
                "browser_attach",
                Some("经营看板"),
                "本周订单 1200",
                &json!({"tables": [["指标", "值"], ["订单", "1200"]]}),
                1700000001000,
            )
            .unwrap();

        assert_eq!(snapshot.collector, "browser_attach");
        assert_eq!(snapshot.content_text, "本周订单 1200");

        let replaced = storage
            .save_data_snapshot(
                1,
                "browser_attach",
                Some("经营看板"),
                "本周订单 1350",
                &json!({"tables": [["指标", "值"], ["订单", "1350"]]}),
                1700000002000,
            )
            .unwrap();
        let snapshot_count = storage
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT COUNT(*) FROM data_snapshots WHERE source_id = 1",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .map_err(Into::into)
            })
            .unwrap();
        assert_eq!(snapshot_count, 1);
        assert_eq!(replaced.id, snapshot.id);
        assert_eq!(replaced.content_text, "本周订单 1350");
    }

    #[test]
    fn report_snapshots_deduplicate_within_week_and_keep_cross_week_history() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO data_sources (
                        canonical_key, title, source_kind, source_url, access_mode,
                        refresh_policy, realtime_level, first_seen_at, last_seen_at,
                        created_at, updated_at
                     ) VALUES (
                        'url:https://bi.example.com/gpu', 'GPU 用量报表', 'report_url',
                        'https://bi.example.com/gpu', 'browser_session', 'on_demand',
                        'live', 1704067200000, 1704067200000, 1704067200000, 1704067200000
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        let week_one = 1_704_067_200_000;
        let week_two = week_one + WEEK_MILLIS;
        let first = storage
            .save_data_snapshot(
                1,
                "browser_attach",
                Some("GPU 用量报表"),
                "GPU 使用量 100 卡时",
                &json!({"metric": "GPU 使用量", "value": 100}),
                week_one,
            )
            .unwrap();
        let same_week = storage
            .save_data_snapshot(
                1,
                "browser_attach",
                Some("GPU 用量报表"),
                "GPU 使用量 120 卡时",
                &json!({"metric": "GPU 使用量", "value": 120}),
                week_one + 60_000,
            )
            .unwrap();
        let next_week = storage
            .save_data_snapshot(
                1,
                "browser_attach",
                Some("GPU 用量报表"),
                "GPU 使用量 150 卡时",
                &json!({"metric": "GPU 使用量", "value": 150}),
                week_two,
            )
            .unwrap();

        assert_eq!(same_week.id, first.id);
        assert_ne!(next_week.id, first.id);
        assert_eq!(same_week.period_granularity, "week");
        assert_ne!(same_week.period_key, next_week.period_key);

        let results = storage
            .search_data_sources("GPU 使用量", false, week_two + 60_000, 10)
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].history.len(), 2);
        assert_eq!(results[0].history[0].content_text, "GPU 使用量 150 卡时");
        assert_eq!(results[0].history[1].content_text, "GPU 使用量 120 卡时");
        assert!(results[0].history.iter().all(|snapshot| {
            snapshot.structured_data["period"]["key"] == snapshot.period_key
                && snapshot.provenance["period"]["key"] == snapshot.period_key
        }));
    }

    #[test]
    fn exact_report_url_participates_in_the_same_top_k_ranking() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, source_url, access_mode,
                        refresh_policy, realtime_level, tags, first_seen_at, last_seen_at,
                        status, created_at, updated_at
                    ) VALUES
                        (1, 'report:https://bi.example.com/dashboard/gpu?team=a', 'GPU 实时看板',
                         'report_url', 'https://bi.example.com/dashboard/gpu?team=a',
                         'browser_session', 'on_demand', 'live', '["report"]', 1, 1,
                         'active', 1, 1),
                        (2, 'memory:timeline:2', 'GPU 利用率治理复盘', 'work_memory', NULL,
                         'memory_only', 'never', 'observed', '["work_memory"]', 2, 2,
                         'active', 2, 2);
                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    ) VALUES (
                        2, 2, 2, 'memory_extract', 'GPU 利用率治理方案与历史复盘',
                        '{"metric_rows":[{"metric":"GPU 利用率","value":"42%"}]}',
                        'memory', 0, '{}', '[]', '[2]', 'success', 2
                    );
                    "#,
                )?;
                Ok(())
            })
            .unwrap();

        let results = storage
            .search_data_sources(
                "https://bi.example.com/dashboard/gpu?team=a\n请使用最新 GPU 数据",
                true,
                1700000000000,
                1,
            )
            .unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].source_id, 1);
        assert_eq!(results[0].relevance_score, 1.0);
        assert!(results[0].refresh_required);
    }

    #[test]
    fn skill_scoped_gpu_query_recalls_data_1617_and_its_live_report() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, source_url, access_mode,
                        refresh_policy, realtime_level, tags, first_seen_at, last_seen_at,
                        status, created_at, updated_at
                    ) VALUES
                        (1584, 'report:https://kwaishop.example.com/gpu/project',
                         '电商GPU信息平台 - GPU项目用量管理', 'report_url',
                         'https://kwaishop.example.com/gpu/project', 'browser_session',
                         'on_demand', 'live', '["report"]', 1786168119642,
                         1786168119642, 'active', 1786168119642, 1786168119642),
                        (1617, 'memory:gpu-platform:total-cards',
                         '电商GPU信息平台总卡数（X40折算）', 'work_memory',
                         'https://kwaishop.example.com/gpu/project', 'memory_only',
                         'never', 'observed', '["work_memory"]', 1786168119642,
                         1786168119642, 'active', 1786168119642, 1786168119642);

                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    ) VALUES (
                        1617, 1786168119642, 1786168119642, 'memory_extract',
                        '电商GPU信息平台项目总卡数为1803.59（按X40折算）。',
                        '{"extraction_version":"data-memory.v15","title":"电商GPU信息平台总卡数（X40折算）","summary":"项目总卡数为1803.59。","semantic_subject":"电商GPU信息平台","semantic_identity":"电商gpu信息平台:总卡数","metric_rows":[{"dimension":"","metric":"总卡数","value":"1803.59(X40折算)","note":""}]}',
                        'gpu-1617', 0, '{}', '[]', '[]', 'success', 1786168119642
                    );

                    WITH RECURSIVE distractor(n) AS (
                        SELECT 1 UNION ALL SELECT n + 1 FROM distractor WHERE n < 8
                    )
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                    )
                    SELECT 2000 + n, 'memory:gpu-util:' || n,
                           'GPU 利用率对比 ' || n, 'work_memory', 'memory_only', 'never',
                           'observed', '["work_memory"]', 1786250000000 + n,
                           1786250000000 + n, 'active', 1786250000000 + n,
                           1786250000000 + n
                    FROM distractor;

                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    )
                    SELECT id, 1786250000000 + id, 1786250000000 + id,
                           'memory_extract', 'GPU 利用率历史对比',
                           '{"metric_rows":[{"dimension":"","metric":"利用率","value":"42%","note":""}]}',
                           'noise-' || id, 0, '{}', '[]', '[]', 'success',
                           1786250000000 + id
                    FROM data_sources WHERE id BETWEEN 2001 AND 2008;
                    "#,
                )?;
                Ok(())
            })
            .unwrap();

        let results = storage
            .search_data_sources(
                "当前数据检索步骤：GPU算力数据\n用数据检索 Tool 获取电商GPU信息平台的最新算力、利用率、收益数据，并添加到表格中",
                true,
                1786254800000,
                6,
            )
            .unwrap();

        assert!(
            results.iter().any(|item| item.source_id == 1617),
            "Skill 明确指定电商GPU信息平台时必须召回数据 1617: {results:?}"
        );
        let report = results
            .iter()
            .find(|item| item.source_id == 1584)
            .expect("同 URL 的即时报表源必须与数据 1617 一起进入 Top-K");
        assert_eq!(report.source_kind, "report_url");
        assert!(report.refresh_required);
        assert!(!report.can_use);
    }

    #[test]
    fn optional_live_gpu_skill_search_recalls_expected_data_and_refresh_source() {
        let Ok(db_path) = std::env::var("MEMORY_BREAD_DATA_SEARCH_E2E_DB") else {
            return;
        };
        let expected_id = std::env::var("MEMORY_BREAD_DATA_SEARCH_EXPECTED_ID")
            .unwrap_or_else(|_| "1617".to_string())
            .parse::<i64>()
            .expect("expected data source id must be an integer");
        let storage = StorageManager::open(std::path::Path::new(&db_path)).unwrap();
        let results = storage
            .search_data_sources(
                "当前数据检索步骤：GPU算力数据\n用数据检索 Tool 获取电商GPU信息平台的最新算力、利用率、收益数据，并添加到表格中",
                true,
                current_ts_ms(),
                6,
            )
            .unwrap();
        let data = results
            .iter()
            .find(|item| item.source_id == expected_id)
            .unwrap_or_else(|| panic!("expected data source {expected_id} in {results:?}"));
        let canonical_url = data
            .source_url
            .as_deref()
            .and_then(canonical_data_url)
            .expect("expected data source must retain its report URL");
        assert!(results.iter().any(|item| {
            item.source_kind == "report_url"
                && item.refresh_required
                && item
                    .source_url
                    .as_deref()
                    .and_then(canonical_data_url)
                    .as_deref()
                    == Some(canonical_url.as_str())
        }));
    }

    #[test]
    fn extraction_is_idempotent_and_searches_snapshot_content() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, ax_text, url,
                        webpage_title, is_sensitive, pii_scrubbed
                     ) VALUES (
                        1, 1700000000000, 'Google Chrome', '经营看板', 'manual',
                        '本周订单 1200，环比增长 8%',
                        'https://bi.example.com/dashboard/weekly', '经营数据看板', 0, 0
                     )",
                    [],
                )?;
                conn.execute(
                    "INSERT INTO timelines (
                        id, capture_id, summary, overview, details, entities, category,
                        importance, created_at_ms, updated_at_ms
                     ) VALUES (
                        7, 1, '本周经营复盘', '订单 1200，环比增长 8%',
                        '{\"period\":\"weekly\"}', '[]', 'work', 4,
                        1700000000000, 1700000000000
                     )",
                    [],
                )?;
                conn.execute("UPDATE captures SET timeline_id = 7 WHERE id = 1", [])?;
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, ax_text, timeline_id,
                        is_sensitive, pii_scrubbed
                     ) VALUES (
                        2, 1700000060000, 'Feishu', '项目群', 'manual',
                        '讨论下周计划，不含新的指标值', 7, 0, 0
                     )",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        let first = storage.extract_data_candidates(100).unwrap();
        let second = storage.extract_data_candidates(100).unwrap();
        let (sources, total) = storage.list_data_sources(None, 20, 0).unwrap();
        let (matched, matched_total) = storage.list_data_sources(Some("订单 1200"), 20, 0).unwrap();
        let report_results = storage
            .search_data_sources("经营看板", true, 1700000001000, 10)
            .unwrap();

        assert_eq!(first.source_created_count, 2);
        assert_eq!(first.snapshot_created_count, 1);
        assert_eq!(second.source_created_count, 0);
        assert_eq!(second.snapshot_created_count, 0);
        assert_eq!(total, 1);
        assert_eq!(sources.len(), 1);
        let (pending, pending_total) = storage.list_pending_data_sources(None, 20).unwrap();
        assert_eq!(pending_total, 1);
        assert_eq!(pending.len(), 1);
        assert_eq!(matched_total, 1);
        assert_eq!(matched[0].source_kind, "work_memory");
        assert_eq!(
            matched[0].source_url.as_deref(),
            Some("https://bi.example.com/dashboard/weekly"),
            "网页采集的工作记忆数据应记录来源网址"
        );
        assert_eq!(
            matched[0]
                .latest_snapshot
                .as_ref()
                .map(|snapshot| snapshot.collected_at),
            Some(1700000000000)
        );
        let report = report_results
            .iter()
            .find(|item| item.source_kind == "report_url")
            .expect("report source is recalled");
        assert!(report.refresh_required);
        assert!(!report.can_use);
    }

    #[test]
    fn extraction_cursor_backfills_history_without_starving_new_captures() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                for id in 1_i64..=5 {
                    conn.execute(
                        "INSERT INTO captures (
                            id, ts, app_name, win_title, event_type, url,
                            webpage_title, is_sensitive, pii_scrubbed
                         ) VALUES (?1, ?2, 'Google Chrome', '经营看板', 'manual', ?3,
                                   '经营看板', 0, 0)",
                        params![
                            id,
                            1700000000000_i64 + id,
                            format!("https://bi.example.com/dashboard/{id}")
                        ],
                    )?;
                }
                Ok(())
            })
            .unwrap();

        assert_eq!(
            storage
                .extract_data_candidates(2)
                .unwrap()
                .source_created_count,
            2
        );
        assert_eq!(
            storage
                .extract_data_candidates(2)
                .unwrap()
                .source_created_count,
            2
        );

        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, url,
                        webpage_title, is_sensitive, pii_scrubbed
                     ) VALUES (6, 1700000000006, 'Google Chrome', '经营看板', 'manual',
                               'https://bi.example.com/dashboard/6', '经营看板', 0, 0)",
                    [],
                )?;
                Ok(())
            })
            .unwrap();

        let mixed_batch = storage.extract_data_candidates(2).unwrap();
        assert_eq!(mixed_batch.source_created_count, 2);
        let (_, total) = storage.list_data_sources(None, 20, 0).unwrap();
        let (_, pending_total) = storage.list_pending_data_sources(None, 20).unwrap();
        assert_eq!(total, 0);
        assert_eq!(pending_total, 6);
    }

    fn seed_discovery_capture(storage: &StorageManager, id: i64, is_sensitive: i64) {
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO captures (
                        id, ts, app_name, win_title, event_type, url,
                        webpage_title, is_sensitive, pii_scrubbed
                     ) VALUES (?1, 1700000000000, 'Google Chrome',
                               '电商GPU信息平台 - GPU使用情况一览', 'browser_navigation',
                               'https://kwaishop-sre.corp.example.com/kwaishop/gpu/info',
                               '电商GPU信息平台 - GPU使用情况一览', ?2, 0)",
                    params![id, is_sensitive],
                )?;
                conn.execute(
                    "INSERT OR IGNORE INTO timelines (
                        id, capture_id, summary, overview, details, entities, category,
                        importance, created_at_ms, updated_at_ms
                     ) VALUES (
                        2160, ?1, 'GPU 用量巡检', '电商GPU信息平台查看利用率',
                        '', '[]', 'work', 4, 1700000000000, 1700000000000
                     )",
                    [id],
                )?;
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn registers_discovered_report_source_and_is_idempotent() {
        let storage = StorageManager::open_in_memory().unwrap();
        seed_discovery_capture(&storage, 21603, 0);

        let url = "https://kwaishop-sre.corp.example.com/kwaishop/gpu/info?token=abc&page=2";
        let first = storage
            .register_discovered_report_source(url, "电商GPU信息平台", 21603, Some(2160), 0)
            .unwrap();
        let DiscoveredSourceOutcome::Registered { source_id, created } = first else {
            panic!("期望注册成功，实际: {first:?}");
        };
        assert!(created);

        // 幂等：同一 canonical URL 再注册不新建，敏感参数被规范化剔除
        let second = storage
            .register_discovered_report_source(url, "", 21603, Some(2160), 0)
            .unwrap();
        let DiscoveredSourceOutcome::Registered {
            source_id: second_id,
            created: second_created,
        } = second
        else {
            panic!("期望幂等注册，实际: {second:?}");
        };
        assert_eq!(source_id, second_id);
        assert!(!second_created);

        // 新注册源尚无快照，不出现在 list_data_sources，而是进入 pending 待采集列表
        let (_, total) = storage.list_data_sources(None, 20, 0).unwrap();
        assert_eq!(total, 0);
        let (pending, pending_total) = storage.list_pending_data_sources(None, 20).unwrap();
        assert_eq!(pending_total, 1);
        assert_eq!(pending[0].id, source_id);

        let source = storage.get_data_source(source_id).unwrap().unwrap();
        assert_eq!(source.source_kind, "report_url");
        assert_eq!(source.access_mode, "browser_session");
        assert_eq!(source.refresh_policy, "on_demand");
        assert_eq!(source.realtime_level, "live");
        assert!(source.tags.contains(&"model_classified".to_string()));
        let canonical = source.source_url.as_deref().unwrap();
        assert!(!canonical.contains("token="));
        assert!(canonical.contains("page=2"));

        let link_count: i64 = storage
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT COUNT(*) FROM data_source_links
                     WHERE source_id = ?1 AND link_kind = 'active_url'",
                    [source_id],
                    |row| row.get(0),
                )
                .map_err(Into::into)
            })
            .unwrap();
        assert_eq!(
            link_count, 1,
            "同 capture + timeline 重复注册只保留一条 link"
        );

        // 另一条 capture 再命中同一页面，追加第二条 link
        seed_discovery_capture(&storage, 21604, 0);
        let third = storage
            .register_discovered_report_source(url, "", 21604, Some(2160), 0)
            .unwrap();
        assert!(matches!(
            third,
            DiscoveredSourceOutcome::Registered { created: false, .. }
        ));
        let link_count: i64 = storage
            .with_conn(|conn| {
                conn.query_row(
                    "SELECT COUNT(*) FROM data_source_links
                     WHERE source_id = ?1 AND link_kind = 'active_url'",
                    [source_id],
                    |row| row.get(0),
                )
                .map_err(Into::into)
            })
            .unwrap();
        assert_eq!(link_count, 2, "不同 capture 各写一条 discovered link");
    }

    #[test]
    fn rejects_discovered_source_for_missing_or_sensitive_capture() {
        let storage = StorageManager::open_in_memory().unwrap();
        seed_discovery_capture(&storage, 9, 1);

        let missing = storage
            .register_discovered_report_source("https://bi.example.com/report", "", 404, None, 0)
            .unwrap();
        assert_eq!(missing, DiscoveredSourceOutcome::RejectedCaptureMissing);

        let sensitive = storage
            .register_discovered_report_source("https://bi.example.com/report", "", 9, None, 0)
            .unwrap();
        assert_eq!(sensitive, DiscoveredSourceOutcome::RejectedCaptureSensitive);

        let (_, total) = storage.list_data_sources(None, 20, 0).unwrap();
        assert_eq!(total, 0);
    }

    #[test]
    fn rejects_discovered_source_with_invalid_url() {
        let storage = StorageManager::open_in_memory().unwrap();
        seed_discovery_capture(&storage, 1, 0);

        let outcome = storage
            .register_discovered_report_source("not-a-url", "", 1, None, 0)
            .unwrap();
        assert_eq!(outcome, DiscoveredSourceOutcome::RejectedInvalidUrl);
    }

    fn seed_data_sources_for_query(storage: &StorageManager) {
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, source_app_name, source_window_title, tags,
                        first_seen_at, last_seen_at, last_collected_at, last_success_at,
                        status, created_at, updated_at
                    ) VALUES
                    (1, 'key-1', 'GPU 资源周报', 'work_memory', 'memory_only', 'never', 'observed', 'Chrome', '周报窗口', '[]', 1, 2, 2, 2, 'active', 1, 2),
                    (2, 'key-2', '完全无关的数据源', 'work_memory', 'memory_only', 'never', 'observed', 'Chrome', '无关窗口', '[]', 1, 2, 2, 2, 'active', 1, 2),
                    (3, 'key-3', 'GPU 成本明细', 'work_memory', 'memory_only', 'never', 'observed', 'Chrome', '明细窗口', '[]', 1, 2, 2, 2, 'active', 1, 2);
                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    ) VALUES
                    (1, 2, 2, 'memory_extract', '本周 GPU 使用率为85%', '{"extraction_version":"data-memory.v15","title":"GPU 使用率","summary":"GPU 使用率：85%","metric_rows":[{"dimension":"","metric":"GPU 使用率","value":"85%","note":"","statement":"本周 GPU 使用率为85%","observed_at":2}]}', 'h1', 0, '{}', '[]', '[]', 'success', 2),
                    (2, 2, 2, 'memory_extract', '与搜索词毫无关系的内容，订单成功率为92%', '{"extraction_version":"data-memory.v15","title":"订单成功率","summary":"订单成功率：92%","metric_rows":[{"dimension":"","metric":"订单成功率","value":"92%","note":"","statement":"订单成功率为92%","observed_at":2}]}', 'h2', 0, '{}', '[]', '[]', 'success', 2),
                    (3, 2, 2, 'memory_extract', '本条快照正文不含关键词，本周节省成本 1200 元', '{"extraction_version":"data-memory.v15","title":"节省成本","summary":"节省成本：1200 元","metric_rows":[{"dimension":"","metric":"节省成本","value":"1200 元","note":"","statement":"本周节省成本 1200 元","observed_at":2}]}', 'h3', 0, '{}', '[]', '[]', 'success', 2);
                    "#,
                )?;
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn list_data_sources_query_prefilter_results_match_contains() {
        let storage = StorageManager::open_in_memory().unwrap();
        seed_data_sources_for_query(&storage);

        let (records, total) = storage.list_data_sources(Some("GPU"), 20, 0).unwrap();
        assert_eq!(total, 2);
        let ids: HashSet<i64> = records.iter().map(|record| record.id).collect();
        assert_eq!(ids, HashSet::from([1, 3]));
    }

    #[test]
    fn list_data_sources_orders_by_visible_snapshot_time() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    r#"
                    INSERT INTO data_sources (
                        id, canonical_key, title, source_kind, access_mode, refresh_policy,
                        realtime_level, tags, first_seen_at, last_seen_at, status,
                        created_at, updated_at
                    ) VALUES
                        (1698, 'older-snapshot-seen-later', '旧快照近期再次出现',
                         'work_memory', 'memory_only', 'never', 'observed', '[]',
                         100, 400, 'active', 100, 400),
                        (1677, 'newer-snapshot-seen-earlier', '最新数据',
                         'work_memory', 'memory_only', 'never', 'observed', '[]',
                         200, 300, 'active', 200, 300);
                    INSERT INTO data_snapshots (
                        source_id, collected_at, observed_at, collector, content_text,
                        structured_data, content_hash, freshness_ttl_seconds, provenance,
                        source_capture_ids, source_timeline_ids, status, created_at
                    ) VALUES
                        (1698, 100, 100, 'memory_extract', '旧 GPU 使用率为 10%',
                         '{"extraction_version":"data-memory.v15","title":"旧 GPU 使用率","summary":"旧 GPU 使用率为 10%","metric_rows":[{"dimension":"","metric":"GPU 使用率","value":"10%","note":"","statement":"旧 GPU 使用率为 10%","observed_at":100}]}',
                         'older', 0, '{}', '[]', '[]', 'success', 100),
                        (1677, 200, 200, 'memory_extract', '最新 GPU 使用率为 20%',
                         '{"extraction_version":"data-memory.v15","title":"最新 GPU 使用率","summary":"最新 GPU 使用率为 20%","metric_rows":[{"dimension":"","metric":"GPU 使用率","value":"20%","note":"","statement":"最新 GPU 使用率为 20%","observed_at":200}]}',
                         'newer', 0, '{}', '[]', '[]', 'success', 200);
                    "#,
                )?;
                Ok(())
            })
            .unwrap();

        let (records, total) = storage.list_data_sources(None, 1, 0).unwrap();

        assert_eq!(total, 2);
        assert_eq!(
            records.iter().map(|record| record.id).collect::<Vec<_>>(),
            vec![1677]
        );
    }

    #[test]
    fn list_data_sources_query_falls_back_without_fts() {
        let storage = StorageManager::open_in_memory().unwrap();
        seed_data_sources_for_query(&storage);
        storage
            .with_conn(|conn| {
                conn.execute_batch(
                    "DROP TRIGGER IF EXISTS data_snapshots_fts_insert;
                     DROP TRIGGER IF EXISTS data_snapshots_fts_update;
                     DROP TRIGGER IF EXISTS data_snapshots_fts_delete;
                     DROP TABLE IF EXISTS data_snapshots_fts;",
                )?;
                Ok(())
            })
            .unwrap();

        let (records, total) = storage.list_data_sources(Some("GPU"), 20, 0).unwrap();
        assert_eq!(total, 2);
        let ids: HashSet<i64> = records.iter().map(|record| record.id).collect();
        assert_eq!(ids, HashSet::from([1, 3]));
    }
}
