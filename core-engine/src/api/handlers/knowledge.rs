//! 知识库 API 处理器
//!
//! 提供知识条目的查询、验证、删除等功能

use std::sync::Arc;

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};

use crate::{
    api::{error::ApiError, state::AppState},
    storage::TimelineRecord,
};

const FALLBACK_NOISE_OVERVIEW_PREFIX: &str = "低价值工作片段（";

/// 知识条目
#[derive(Debug, Serialize, Deserialize)]
pub struct KnowledgeEntry {
    pub id: i64,
    pub capture_id: i64,
    pub summary: String,
    pub overview: Option<String>,
    pub details: Option<String>,
    pub entities: Vec<String>,
    pub category: String,
    pub importance: i64,
    pub occurrence_count: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub history_view: bool,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub is_self_generated: bool,
    pub evidence_strength: Option<String>,
    pub user_verified: bool,
    pub user_edited: bool,
    pub created_at: String,
    pub updated_at: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub capture_ids: Option<Vec<i64>>,
    #[serde(rename = "keyTimestamps")]
    pub key_timestamps: Option<serde_json::Value>,
}

/// 查询参数
#[derive(Debug, Deserialize)]
pub struct KnowledgeQuery {
    #[serde(default = "default_limit")]
    pub limit: i64,
    #[serde(default)]
    pub offset: i64,
    pub q: Option<String>,
    pub category: Option<String>,
    pub from: Option<i64>,
    pub to: Option<i64>,
}

fn default_limit() -> i64 {
    50
}

/// 知识条目列表响应
#[derive(Debug, Serialize)]
pub struct KnowledgeListResponse {
    pub entries: Vec<KnowledgeEntry>,
    pub total: i64,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ExtractKnowledgeRequest {
    pub limit: Option<usize>,
    #[serde(default)]
    pub force_finalize_tail: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ExtractKnowledgeResponse {
    pub status: String,
    pub message: String,
    pub fetched_count: usize,
    pub processed_count: usize,
    pub remaining_estimate: usize,
    pub force_finalize_tail: bool,
    pub reason: Option<String>,
}

/// POST /api/knowledge/extract - 触发一次真实 knowledge 提炼
pub async fn extract_knowledge(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ExtractKnowledgeRequest>,
) -> Result<Json<ExtractKnowledgeResponse>, ApiError> {
    if !state.is_capture_enabled() {
        return Err(ApiError::BadRequest("采集与提炼当前已暂停".to_string()));
    }

    let client = reqwest::Client::new();
    let upstream_url = format!("{}/knowledge/extract", state.sidecar_url);

    let response = client
        .post(&upstream_url)
        .json(&body)
        .timeout(std::time::Duration::from_secs(180))
        .send()
        .await
        .map_err(|e| {
            let msg = e.to_string();
            if msg.contains("timed out") || msg.contains("timeout") {
                ApiError::Internal("知识提炼执行超时，请稍后刷新知识列表确认结果".to_string())
            } else {
                ApiError::Internal(format!(
                    "知识提炼服务不可用，请确认 AI Sidecar 已正常启动: {e}"
                ))
            }
        })?;

    if response.status().is_success() {
        let payload = response
            .json::<ExtractKnowledgeResponse>()
            .await
            .map_err(|e| ApiError::Internal(format!("解析知识提炼响应失败: {e}")))?;
        Ok(Json(payload))
    } else {
        let status = response.status();
        let body_text = response.text().await.unwrap_or_default();
        tracing::warn!(
            "knowledge extract upstream error status={} body={}",
            status,
            body_text
        );

        let (mapped_status, code) = match status.as_u16() {
            400 | 422 => (StatusCode::BAD_REQUEST, "BAD_REQUEST"),
            502 => (StatusCode::BAD_GATEWAY, "BAD_GATEWAY"),
            503 => (StatusCode::SERVICE_UNAVAILABLE, "SERVICE_UNAVAILABLE"),
            504 => (StatusCode::GATEWAY_TIMEOUT, "GATEWAY_TIMEOUT"),
            code if code >= 500 => (StatusCode::BAD_GATEWAY, "BAD_GATEWAY"),
            _ => (StatusCode::BAD_GATEWAY, "BAD_GATEWAY"),
        };

        let message = if body_text.trim().is_empty() {
            format!("知识提炼服务返回错误 ({status})")
        } else {
            format!("知识提炼服务返回错误 ({status})：{body_text}")
        };

        Err(ApiError::Upstream {
            status: mapped_status,
            code,
            message,
        })
    }
}

/// GET /api/knowledge - 获取知识条目列表
pub async fn list_knowledge(
    State(state): State<Arc<AppState>>,
    Query(params): Query<KnowledgeQuery>,
) -> Result<impl IntoResponse, ApiError> {
    let result = state.storage.with_conn_async(move |conn| {
        let (entries, total) = if let Some(ref category) = params.category {
            match category.as_str() {
                "bake_knowledge" => {
                    let mut stmt = conn.prepare(
                        "SELECT b.id, b.timeline_id, b.summary, b.title, b.content, b.entities,
                         b.importance, b.created_at, b.updated_at, b.created_at_ms, b.updated_at_ms
                         FROM bake_knowledge b
                         ORDER BY b.created_at DESC LIMIT ?1 OFFSET ?2"
                    ).map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    let entries = stmt.query_map(rusqlite::params![params.limit, params.offset], |row: &rusqlite::Row| {
                        let entities_json: String = row.get(5).unwrap_or_default();
                        let entities: Vec<String> = serde_json::from_str(&entities_json).unwrap_or_default();
                        Ok(KnowledgeEntry {
                            id: row.get(0)?,
                            capture_id: row.get(1)?,
                            summary: row.get(2)?,
                            overview: row.get::<_, Option<String>>(3).ok().flatten(),
                            details: row.get::<_, Option<String>>(4).ok().flatten(),
                            entities,
                            category: "bake_knowledge".to_string(),
                            importance: row.get::<_, Option<i64>>(6)?.unwrap_or(3),
                            occurrence_count: None,
                            observed_at: None,
                            event_time_start: None,
                            event_time_end: None,
                            history_view: false,
                            content_origin: None,
                            activity_type: None,
                            is_self_generated: false,
                            evidence_strength: None,
                            user_verified: false,
                            user_edited: false,
                            created_at: row.get(7)?,
                            updated_at: row.get(8)?,
                            created_at_ms: row.get::<_, Option<i64>>(9)?.unwrap_or(0),
                            updated_at_ms: row.get::<_, Option<i64>>(10)?.unwrap_or(0),
                            capture_ids: None,
                            key_timestamps: None,
                        })
                    })
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    let total: i64 = conn.query_row("SELECT COUNT(*) FROM bake_knowledge", [], |row| row.get(0))
                        .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    (entries, total)
                },
                "bake_sop" => {
                    let mut stmt = conn.prepare(
                        "SELECT b.id, b.timeline_id, b.summary, b.title, b.content, b.entities,
                         b.importance, b.created_at, b.updated_at, b.created_at_ms, b.updated_at_ms
                         FROM bake_sops b
                         ORDER BY b.created_at DESC LIMIT ?1 OFFSET ?2"
                    ).map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    let entries = stmt.query_map(rusqlite::params![params.limit, params.offset], |row: &rusqlite::Row| {
                        let entities_json: String = row.get(5).unwrap_or_default();
                        let entities: Vec<String> = serde_json::from_str(&entities_json).unwrap_or_default();
                        Ok(KnowledgeEntry {
                            id: row.get(0)?,
                            capture_id: row.get(1)?,
                            summary: row.get(2)?,
                            overview: row.get::<_, Option<String>>(3).ok().flatten(),
                            details: row.get::<_, Option<String>>(4).ok().flatten(),
                            entities,
                            category: "bake_sop".to_string(),
                            importance: row.get::<_, Option<i64>>(6)?.unwrap_or(3),
                            occurrence_count: None,
                            observed_at: None,
                            event_time_start: None,
                            event_time_end: None,
                            history_view: false,
                            content_origin: None,
                            activity_type: None,
                            is_self_generated: false,
                            evidence_strength: None,
                            user_verified: false,
                            user_edited: false,
                            created_at: row.get(7)?,
                            updated_at: row.get(8)?,
                            created_at_ms: row.get::<_, Option<i64>>(9)?.unwrap_or(0),
                            updated_at_ms: row.get::<_, Option<i64>>(10)?.unwrap_or(0),
                            capture_ids: None,
                            key_timestamps: None,
                        })
                    })
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    let total: i64 = conn.query_row("SELECT COUNT(*) FROM bake_sops", [], |row| row.get(0))
                        .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    (entries, total)
                },
                _ => {
                    // 其他 category 查询 timelines
                    let mut stmt = conn.prepare(
                        "SELECT id, capture_id, summary, overview, details, entities, category, importance,
                         occurrence_count, observed_at, event_time_start, event_time_end,
                         history_view, content_origin, activity_type, is_self_generated,
                         evidence_strength, user_verified, user_edited, created_at, updated_at,
                         created_at_ms, updated_at_ms, capture_ids, key_timestamps
                         FROM timelines WHERE category = ?1
                           AND summary NOT LIKE ?2
                           AND COALESCE(is_self_generated, 0) = 0
                         ORDER BY created_at_ms DESC, id DESC LIMIT ?3 OFFSET ?4"
                    ).map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    let entries = stmt.query_map(rusqlite::params![category, format!("{}%", FALLBACK_NOISE_OVERVIEW_PREFIX), params.limit, params.offset], |row: &rusqlite::Row| {
                        let entities_json: String = row.get(5).unwrap_or_default();
                        let entities: Vec<String> = serde_json::from_str(&entities_json).unwrap_or_default();
                        let capture_ids: Option<Vec<i64>> = row.get::<_, Option<String>>(23).ok().flatten()
                            .and_then(|s| serde_json::from_str(&s).ok());
                        let key_timestamps: Option<serde_json::Value> = row.get::<_, Option<String>>(24).ok().flatten()
                            .and_then(|s| serde_json::from_str(&s).ok());
                        Ok(KnowledgeEntry {
                            id: row.get(0)?, capture_id: row.get(1)?,
                            summary: row.get(2)?, overview: row.get(3).ok(),
                            details: row.get(4).ok(), entities,
                            category: row.get::<_, Option<String>>(6)?.unwrap_or_default(),
                            importance: row.get::<_, Option<i64>>(7)?.unwrap_or(3),
                            occurrence_count: row.get(8).ok(),
                            observed_at: row.get(9).ok().flatten(),
                            event_time_start: row.get(10).ok().flatten(),
                            event_time_end: row.get(11).ok().flatten(),
                            history_view: row.get::<_, Option<bool>>(12)?.unwrap_or(false),
                            content_origin: row.get(13).ok().flatten(),
                            activity_type: row.get(14).ok().flatten(),
                            is_self_generated: row.get::<_, Option<bool>>(15)?.unwrap_or(false),
                            evidence_strength: row.get(16).ok().flatten(),
                            user_verified: row.get::<_, Option<bool>>(17)?.unwrap_or(false),
                            user_edited: row.get::<_, Option<bool>>(18)?.unwrap_or(false),
                            created_at: row.get(19)?, updated_at: row.get(20)?,
                            created_at_ms: row.get::<_, Option<i64>>(21)?.unwrap_or(0),
                            updated_at_ms: row.get::<_, Option<i64>>(22)?.unwrap_or(0),
                            capture_ids,
                            key_timestamps,
                        })
                    })
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    let total: i64 = conn.query_row(
                        "SELECT COUNT(*) FROM timelines WHERE category = ?1 AND summary NOT LIKE ?2 AND COALESCE(is_self_generated, 0) = 0",
                        rusqlite::params![category, format!("{}%", FALLBACK_NOISE_OVERVIEW_PREFIX)],
                        |row| row.get(0),
                    ).map_err(|e| crate::storage::StorageError::Sqlite(e))?;

                    (entries, total)
                }
            }
        } else {
            // 没有 category 参数，查询 timelines
            let noise_prefix = format!("{}%", FALLBACK_NOISE_OVERVIEW_PREFIX);
            let mut sql = String::from(
                "SELECT id, capture_id, summary, overview, details, entities, category, importance,
                 occurrence_count, observed_at, event_time_start, event_time_end,
                 history_view, content_origin, activity_type, is_self_generated,
                 evidence_strength, user_verified, user_edited, created_at, updated_at,
                 created_at_ms, updated_at_ms, capture_ids, key_timestamps
                 FROM timelines WHERE summary NOT LIKE ?
                 AND COALESCE(is_self_generated, 0) = 0"
            );
            let mut bind: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(noise_prefix.clone())];
            let query_terms = params
                .q
                .as_deref()
                .map(keyword_terms)
                .unwrap_or_default();
            if !query_terms.is_empty() {
                let query_clause = query_terms
                    .iter()
                    .map(|_| "(COALESCE(summary, '') LIKE ? OR COALESCE(overview, '') LIKE ? OR COALESCE(details, '') LIKE ? OR COALESCE(category, '') LIKE ?)".to_string())
                    .collect::<Vec<_>>()
                    .join(" OR ");
                sql.push_str(" AND (");
                sql.push_str(&query_clause);
                sql.push(')');
                for term in &query_terms {
                    let pattern = format!("%{}%", term);
                    for _ in 0..4 {
                        bind.push(Box::new(pattern.clone()));
                    }
                }
                // FTS5 预筛：timelines_fts 候选可用时收窄扫描，否则回退 LIKE 全扫
                if let Some(fts_query) = crate::storage::fts::build_fts_or_query(&query_terms) {
                    if let Some(ids) = crate::storage::fts::fts_candidate_ids(
                        &conn,
                        "timelines_fts",
                        &fts_query,
                        crate::storage::fts::DEFAULT_FTS_CANDIDATE_CAP,
                    ) {
                        let (clause, mut id_binds) = crate::storage::fts::render_in_clause(&ids);
                        sql.push_str(" AND id IN ");
                        sql.push_str(&clause);
                        bind.append(&mut id_binds);
                    }
                }
            }
            if let Some(f) = params.from { sql.push_str(" AND created_at_ms >= ?"); bind.push(Box::new(f)); }
            if let Some(t) = params.to   { sql.push_str(" AND created_at_ms <= ?"); bind.push(Box::new(t)); }
            // 时间线表格统一按创建时间逆序展示
            sql.push_str(" ORDER BY created_at_ms DESC, id DESC LIMIT ? OFFSET ?");
            bind.push(Box::new(params.limit));
            bind.push(Box::new(params.offset));

            let mut stmt = conn.prepare(&sql).map_err(|e| crate::storage::StorageError::Sqlite(e))?;
            let p: Vec<&dyn rusqlite::ToSql> = bind.iter().map(|b| b.as_ref()).collect();
            let entries = stmt.query_map(p.as_slice(), |row: &rusqlite::Row| {
                let entities_json: String = row.get(5).unwrap_or_default();
                let entities: Vec<String> = serde_json::from_str(&entities_json).unwrap_or_default();
                let capture_ids: Option<Vec<i64>> = row.get::<_, Option<String>>(23).ok().flatten()
                    .and_then(|s| serde_json::from_str(&s).ok());
                let key_timestamps: Option<serde_json::Value> = row.get::<_, Option<String>>(24).ok().flatten()
                    .and_then(|s| serde_json::from_str(&s).ok());
                Ok(KnowledgeEntry {
                    id: row.get(0)?, capture_id: row.get(1)?,
                    summary: row.get(2)?, overview: row.get(3).ok(),
                    details: row.get(4).ok(), entities,
                    category: row.get::<_, Option<String>>(6)?.unwrap_or_default(),
                    importance: row.get::<_, Option<i64>>(7)?.unwrap_or(3),
                    occurrence_count: row.get(8).ok(),
                    observed_at: row.get(9).ok().flatten(),
                    event_time_start: row.get(10).ok().flatten(),
                    event_time_end: row.get(11).ok().flatten(),
                    history_view: row.get::<_, Option<bool>>(12)?.unwrap_or(false),
                    content_origin: row.get(13).ok().flatten(),
                    activity_type: row.get(14).ok().flatten(),
                    is_self_generated: row.get::<_, Option<bool>>(15)?.unwrap_or(false),
                    evidence_strength: row.get(16).ok().flatten(),
                    user_verified: row.get::<_, Option<bool>>(17)?.unwrap_or(false),
                    user_edited: row.get::<_, Option<bool>>(18)?.unwrap_or(false),
                    created_at: row.get(19)?, updated_at: row.get(20)?,
                    created_at_ms: row.get::<_, Option<i64>>(21)?.unwrap_or(0),
                    updated_at_ms: row.get::<_, Option<i64>>(22)?.unwrap_or(0),
                    capture_ids,
                    key_timestamps,
                })
            })
            .map_err(|e| crate::storage::StorageError::Sqlite(e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

            let mut count_sql = String::from("SELECT COUNT(*) FROM timelines WHERE summary NOT LIKE ? AND COALESCE(is_self_generated, 0) = 0");
            let mut count_bind: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(noise_prefix)];
            if !query_terms.is_empty() {
                let query_clause = query_terms
                    .iter()
                    .map(|_| "(COALESCE(summary, '') LIKE ? OR COALESCE(overview, '') LIKE ? OR COALESCE(details, '') LIKE ? OR COALESCE(category, '') LIKE ?)".to_string())
                    .collect::<Vec<_>>()
                    .join(" OR ");
                count_sql.push_str(" AND (");
                count_sql.push_str(&query_clause);
                count_sql.push(')');
                for term in &query_terms {
                    let pattern = format!("%{}%", term);
                    for _ in 0..4 {
                        count_bind.push(Box::new(pattern.clone()));
                    }
                }
                // FTS5 预筛（与列表查询保持一致的候选收窄）
                if let Some(fts_query) = crate::storage::fts::build_fts_or_query(&query_terms) {
                    if let Some(ids) = crate::storage::fts::fts_candidate_ids(
                        &conn,
                        "timelines_fts",
                        &fts_query,
                        crate::storage::fts::DEFAULT_FTS_CANDIDATE_CAP,
                    ) {
                        let (clause, mut id_binds) = crate::storage::fts::render_in_clause(&ids);
                        count_sql.push_str(" AND id IN ");
                        count_sql.push_str(&clause);
                        count_bind.append(&mut id_binds);
                    }
                }
            }
            if let Some(f) = params.from { count_sql.push_str(" AND created_at_ms >= ?"); count_bind.push(Box::new(f)); }
            if let Some(t) = params.to   { count_sql.push_str(" AND created_at_ms <= ?"); count_bind.push(Box::new(t)); }
            let cp: Vec<&dyn rusqlite::ToSql> = count_bind.iter().map(|b| b.as_ref()).collect();
            let total: i64 = conn.query_row(&count_sql, cp.as_slice(), |row| row.get(0))
                .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

            (entries, total)
        };

        Ok(KnowledgeListResponse { entries, total })
    }).await?;

    Ok((
        [(
            axum::http::header::CACHE_CONTROL,
            "no-cache, no-store, must-revalidate",
        )],
        Json(result),
    ))
}

fn keyword_terms(query: &str) -> Vec<String> {
    query
        .split_whitespace()
        .map(str::trim)
        .filter(|term| !term.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

/// GET /api/knowledge/:id - 获取单条时间线详情
pub async fn get_knowledge(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    let mut record = state
        .storage
        .get_timeline_entry(id)?
        .ok_or_else(|| ApiError::NotFound(format!("timeline {id} not found")))?;

    if !matches!(record.category.as_str(), "bake_knowledge" | "bake_sop") {
        let capture_ids = state.storage.get_timeline_capture_ids(id)?;
        if !capture_ids.is_empty() {
            record.capture_ids =
                Some(serde_json::to_string(&capture_ids).unwrap_or_else(|_| "[]".to_string()));
        }
    }

    Ok(Json(timeline_record_to_knowledge_entry(record)))
}

fn timeline_record_to_knowledge_entry(record: TimelineRecord) -> KnowledgeEntry {
    KnowledgeEntry {
        id: record.id,
        capture_id: record.capture_id,
        summary: record.summary,
        overview: record.overview,
        details: record.details,
        entities: serde_json::from_str(&record.entities).unwrap_or_default(),
        category: record.category,
        importance: record.importance,
        occurrence_count: record.occurrence_count,
        observed_at: record.observed_at,
        event_time_start: record.event_time_start,
        event_time_end: record.event_time_end,
        history_view: record.history_view,
        content_origin: record.content_origin,
        activity_type: record.activity_type,
        is_self_generated: record.is_self_generated,
        evidence_strength: record.evidence_strength,
        user_verified: record.user_verified,
        user_edited: record.user_edited,
        created_at: record.created_at,
        updated_at: record.updated_at,
        created_at_ms: record.created_at_ms,
        updated_at_ms: record.updated_at_ms,
        capture_ids: record
            .capture_ids
            .as_deref()
            .and_then(|raw| serde_json::from_str(raw).ok()),
        key_timestamps: record
            .key_timestamps
            .as_deref()
            .and_then(|raw| serde_json::from_str(raw).ok()),
    }
}

/// POST /api/knowledge/:id/verify - 验证知识条目
pub async fn verify_knowledge(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    state.storage.with_conn_async(move |conn| {
        // 尝试在各个表中查找并更新
        let updated = conn.execute(
            "UPDATE timelines SET user_verified = 1, updated_at = CURRENT_TIMESTAMP, updated_at_ms = CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER) WHERE id = ?",
            [id],
        ).map_err(|e| crate::storage::StorageError::Sqlite(e))?;

        if updated == 0 {
            // 如果在 timelines 中没找到，尝试 bake 表
            // 注意：bake 表没有 user_verified 字段，这里可能需要调整逻辑
            // 暂时返回成功，因为 bake 表的记录不需要验证
        }

        Ok(())
    }).await?;
    Ok(StatusCode::OK)
}

/// DELETE /api/knowledge/:id - 删除知识条目
pub async fn delete_knowledge(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    state
        .storage
        .with_conn_async(move |conn| {
            // 尝试从各个表中删除
            let deleted = conn
                .execute("DELETE FROM timelines WHERE id = ?", [id])
                .map_err(|e| crate::storage::StorageError::Sqlite(e))?;

            if deleted == 0 {
                let deleted = conn
                    .execute("DELETE FROM bake_knowledge WHERE id = ?", [id])
                    .map_err(|e| crate::storage::StorageError::Sqlite(e))?;
                if deleted == 0 {
                    let deleted = conn
                        .execute("DELETE FROM bake_sops WHERE id = ?", [id])
                        .map_err(|e| crate::storage::StorageError::Sqlite(e))?;
                    if deleted == 0 {
                        conn.execute("DELETE FROM bake_designs WHERE id = ?", [id])
                            .map_err(|e| crate::storage::StorageError::Sqlite(e))?;
                    }
                }
            }

            Ok(())
        })
        .await?;
    Ok(StatusCode::OK)
}
