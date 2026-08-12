//! 面包屑规则同步、本地结算、库存与佩戴 API。

use std::sync::Arc;

use axum::{extract::State, Json};
use serde::Deserialize;

use crate::{
    api::{error::ApiError, state::AppState},
    storage::repo::breadcrumbs::{BreadcrumbAwardResult, BreadcrumbProfile, BreadcrumbRule},
};

#[derive(Debug, Deserialize)]
pub struct SyncBreadcrumbRulesRequest {
    pub rules: Vec<BreadcrumbRule>,
}

#[derive(Debug, Deserialize)]
pub struct AwardBreadcrumbRequest {
    pub rule_id: String,
    pub period_key: String,
    pub observed_value: f64,
}

#[derive(Debug, Deserialize)]
pub struct EquipBreadcrumbRequest {
    pub surface: String,
    pub breadcrumb_id: Option<String>,
}

pub async fn sync_breadcrumb_rules(
    State(state): State<Arc<AppState>>,
    Json(body): Json<SyncBreadcrumbRulesRequest>,
) -> Result<Json<BreadcrumbProfile>, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.sync_breadcrumb_rules(&body.rules)?;
        storage.list_breadcrumb_profile()
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))?
    .map(Json)
    .map_err(ApiError::from)
}

pub async fn list_breadcrumbs(
    State(state): State<Arc<AppState>>,
) -> Result<Json<BreadcrumbProfile>, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || storage.list_breadcrumb_profile())
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?
        .map(Json)
        .map_err(ApiError::from)
}

pub async fn list_breadcrumb_rules(
    State(state): State<Arc<AppState>>,
) -> Result<Json<Vec<BreadcrumbRule>>, ApiError> {
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || storage.list_breadcrumb_rules())
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))?
        .map(Json)
        .map_err(ApiError::from)
}

pub async fn award_breadcrumb(
    State(state): State<Arc<AppState>>,
    Json(body): Json<AwardBreadcrumbRequest>,
) -> Result<Json<BreadcrumbAwardResult>, ApiError> {
    if body.rule_id.trim().is_empty() || body.period_key.trim().is_empty() {
        return Err(ApiError::BadRequest("规则和周期不能为空".to_string()));
    }
    if !body.observed_value.is_finite() || body.observed_value < 0.0 {
        return Err(ApiError::BadRequest("面包屑计算值不合法".to_string()));
    }
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.award_breadcrumb(&body.rule_id, &body.period_key, body.observed_value)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))?
    .map(Json)
    .map_err(ApiError::from)
}

pub async fn equip_breadcrumb(
    State(state): State<Arc<AppState>>,
    Json(body): Json<EquipBreadcrumbRequest>,
) -> Result<Json<BreadcrumbProfile>, ApiError> {
    if !matches!(body.surface.as_str(), "profile_avatar" | "floating_avatar") {
        return Err(ApiError::BadRequest("面包屑佩戴位置不受支持".to_string()));
    }
    let storage = state.storage.clone();
    tokio::task::spawn_blocking(move || {
        storage.equip_breadcrumb(&body.surface, body.breadcrumb_id.as_deref())
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))?
    .map(Json)
    .map_err(ApiError::from)
}
