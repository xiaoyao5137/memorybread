/// 隐私设置 API 处理器
///
/// 提供应用黑名单和敏感内容过滤配置的 HTTP 接口
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::{
    api::{error::ApiError, state::AppState},
    storage::{
        models::{AppBlacklistRecord, NewAppBlacklist, PrivacyFilterRecord},
        repo::privacy,
    },
};

#[derive(Debug, Serialize)]
pub struct BlacklistWithStats {
    #[serde(flatten)]
    pub record: AppBlacklistRecord,
    pub week_blocked: i64,
}

#[derive(Debug, Serialize)]
pub struct FilterWithStats {
    #[serde(flatten)]
    pub record: PrivacyFilterRecord,
    pub week_blocked: i64,
}

// ─────────────────────────────────────────────────────────────────────────────
// 请求/响应结构体
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct AddBlacklistRequest {
    #[serde(default)]
    pub bundle_id: Option<String>,
    pub app_name: String,
    pub reason: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct UpdateBlacklistEnabledRequest {
    pub enabled: bool,
}

#[derive(Debug, Deserialize)]
pub struct UpdateFilterEnabledRequest {
    pub enabled: bool,
}

#[derive(Debug, Deserialize)]
pub struct UpdateFilterConfigRequest {
    pub config_json: String,
}

#[derive(Debug, Serialize)]
pub struct ApiResponse<T> {
    pub success: bool,
    pub data: Option<T>,
    pub error: Option<String>,
}

impl<T> ApiResponse<T> {
    fn ok(data: T) -> Self {
        Self {
            success: true,
            data: Some(data),
            error: None,
        }
    }

    fn err(error: String) -> Self {
        Self {
            success: false,
            data: None,
            error: Some(error),
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 应用黑名单 API
// ─────────────────────────────────────────────────────────────────────────────

/// GET /api/privacy/blacklist - 获取所有黑名单记录（含本周拦截统计）
pub async fn list_blacklist(
    State(state): State<Arc<AppState>>,
) -> Result<impl IntoResponse, ApiError> {
    let storage = state.storage.clone();
    let result = tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| {
            let records = privacy::list_app_blacklist(conn)?;
            let stats = privacy::get_week_block_stats(conn)?;
            let stats_map: std::collections::HashMap<_, _> = stats
                .into_iter()
                .filter(|s| s.stat_type == "blacklist")
                .map(|s| (s.target_id, s.block_count))
                .collect();
            let with_stats: Vec<BlacklistWithStats> = records
                .into_iter()
                .map(|r| BlacklistWithStats {
                    week_blocked: r
                        .bundle_id
                        .as_ref()
                        .and_then(|id| stats_map.get(id))
                        .copied()
                        .unwrap_or(0)
                        + if r.bundle_id.as_deref() == Some(r.app_name.as_str()) {
                            0
                        } else {
                            *stats_map.get(&r.app_name).unwrap_or(&0)
                        },
                    record: r,
                })
                .collect();
            Ok(with_stats)
        })
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(ApiResponse::ok(result)))
}

/// POST /api/privacy/blacklist - 添加应用到黑名单
pub async fn add_blacklist(
    State(state): State<Arc<AppState>>,
    Json(req): Json<AddBlacklistRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let app_name = req.app_name.trim().to_string();
    if app_name.is_empty() {
        return Err(ApiError::BadRequest("请填写应用名称".to_string()));
    }
    let bundle_id = req
        .bundle_id
        .map(|id| id.trim().to_string())
        .filter(|id| !id.is_empty());
    let storage = state.storage.clone();
    let id = tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| {
            let new_blacklist = NewAppBlacklist {
                bundle_id,
                app_name,
                enabled: true,
                reason: req.reason,
            };
            privacy::add_app_blacklist(conn, &new_blacklist)
        })
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))?
    .map_err(|error| match error {
        crate::storage::StorageError::Sqlite(rusqlite::Error::SqliteFailure(info, _))
            if info.code == rusqlite::ErrorCode::ConstraintViolation =>
        {
            ApiError::Upstream {
                status: StatusCode::CONFLICT,
                code: "BLACKLIST_ENTRY_EXISTS",
                message: "该 Bundle ID 或应用名称已在排除列表中".to_string(),
            }
        }
        other => ApiError::Storage(other),
    })?;

    state.storage.notify_blacklist_changed();

    Ok(Json(ApiResponse::ok(id)))
}

/// PATCH /api/privacy/blacklist/:id/enabled - 更新黑名单启用状态
pub async fn update_blacklist_enabled(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(req): Json<UpdateBlacklistEnabledRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| privacy::update_app_blacklist_enabled(conn, id, req.enabled))
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    state.storage.notify_blacklist_changed();

    Ok(Json(ApiResponse::ok(())))
}

/// DELETE /api/privacy/blacklist/:id - 删除黑名单记录
pub async fn delete_blacklist(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| privacy::delete_app_blacklist(conn, id))
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    state.storage.notify_blacklist_changed();

    Ok(Json(ApiResponse::ok(())))
}

// ─────────────────────────────────────────────────────────────────────────────
// 敏感内容过滤 API
// ─────────────────────────────────────────────────────────────────────────────

/// GET /api/privacy/filters - 获取所有过滤规则（含本周拦截统计）
pub async fn list_filters(
    State(state): State<Arc<AppState>>,
) -> Result<impl IntoResponse, ApiError> {
    let storage = state.storage.clone();
    let result = tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| {
            let records = privacy::list_privacy_filters(conn)?;
            let stats = privacy::get_week_block_stats(conn)?;
            let stats_map: std::collections::HashMap<_, _> = stats
                .into_iter()
                .filter(|s| s.stat_type == "filter")
                .map(|s| (s.target_id, s.block_count))
                .collect();
            let with_stats: Vec<FilterWithStats> = records
                .into_iter()
                .map(|r| FilterWithStats {
                    week_blocked: *stats_map.get(&r.filter_type).unwrap_or(&0),
                    record: r,
                })
                .collect();
            Ok(with_stats)
        })
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(ApiResponse::ok(result)))
}

/// PATCH /api/privacy/filters/:filter_type/enabled - 更新过滤规则启用状态
pub async fn update_filter_enabled(
    State(state): State<Arc<AppState>>,
    Path(filter_type): Path<String>,
    Json(req): Json<UpdateFilterEnabledRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| {
            privacy::update_privacy_filter_enabled(conn, &filter_type, req.enabled)
        })
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(ApiResponse::ok(())))
}

/// PATCH /api/privacy/filters/:filter_type/config - 更新过滤规则配置
pub async fn update_filter_config(
    State(state): State<Arc<AppState>>,
    Path(filter_type): Path<String>,
    Json(req): Json<UpdateFilterConfigRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.with_conn(|conn| {
            privacy::update_privacy_filter_config(conn, &filter_type, &req.config_json)
        })
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(ApiResponse::ok(())))
}
