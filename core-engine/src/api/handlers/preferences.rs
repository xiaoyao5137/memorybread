//! GET /preferences — 获取所有用户偏好
//! PUT /preferences/:key — 更新单条偏好
//! POST /preferences/screenshot-cleanup/run — 立即执行一次截图清理
//! POST /preferences/capture-cleanup/run — 立即执行一次采集记录清理

use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    extract::{Path, State},
    Json,
};
use serde::{Deserialize, Serialize};

use crate::{
    api::{error::ApiError, state::AppState},
    storage::{db::current_ts_ms, models::PreferenceRecord},
};

#[derive(Serialize)]
pub struct PreferencesResponse {
    pub preferences: Vec<PreferenceRecord>,
}

pub async fn list_preferences(
    State(state): State<Arc<AppState>>,
) -> Result<Json<PreferencesResponse>, ApiError> {
    let storage = state.storage.clone();
    let prefs = tokio::task::spawn_blocking(move || storage.list_preferences())
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(PreferencesResponse { preferences: prefs }))
}

/// PUT /preferences/:key 请求体
#[derive(Deserialize)]
pub struct UpdatePreferenceRequest {
    pub value: String,
}

#[derive(Serialize)]
pub struct UpdatePreferenceResponse {
    pub key: String,
    pub value: String,
    pub updated_at: i64,
}

#[derive(Serialize)]
pub struct RunScreenshotCleanupResponse {
    pub keep_days: i64,
    pub deleted_count: usize,
    pub freed_bytes: u64,
}

#[derive(Serialize)]
pub struct RunCaptureCleanupResponse {
    pub retention_days: i64,
    pub deleted_count: usize,
    pub deleted_screenshot_count: usize,
    pub freed_bytes: u64,
}

fn parse_screenshot_keep_days(storage: &crate::storage::StorageManager) -> i64 {
    storage
        .get_preference("privacy.screenshot_keep_days")
        .ok()
        .flatten()
        .and_then(|p| p.value.parse::<i64>().ok())
        .map(|value| value.max(1))
        .unwrap_or(90)
}

fn parse_capture_retention_days(storage: &crate::storage::StorageManager) -> i64 {
    storage
        .get_preference("privacy.capture_retention_days")
        .ok()
        .flatten()
        .and_then(|p| p.value.parse::<i64>().ok())
        .map(|value| value.max(1))
        .unwrap_or(14)
}

fn screenshot_captures_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".memory-bread").join("captures")
}

pub async fn run_screenshot_cleanup_now(
    State(state): State<Arc<AppState>>,
) -> Result<Json<RunScreenshotCleanupResponse>, ApiError> {
    let storage = state.storage.clone();

    let result = tokio::task::spawn_blocking(move || {
        let keep_days = parse_screenshot_keep_days(&storage);
        let cutoff = current_ts_ms() - keep_days * 24 * 60 * 60 * 1000;
        let captures_dir = screenshot_captures_dir();
        let (deleted_count, freed_bytes) = storage.run_screenshot_purge(cutoff, &captures_dir)?;
        Ok::<_, crate::storage::StorageError>((keep_days, deleted_count, freed_bytes))
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(RunScreenshotCleanupResponse {
        keep_days: result.0,
        deleted_count: result.1,
        freed_bytes: result.2,
    }))
}

pub async fn run_capture_cleanup_now(
    State(state): State<Arc<AppState>>,
) -> Result<Json<RunCaptureCleanupResponse>, ApiError> {
    let storage = state.storage.clone();

    let result = tokio::task::spawn_blocking(move || {
        let retention_days = parse_capture_retention_days(&storage);
        let cutoff = current_ts_ms() - retention_days * 24 * 60 * 60 * 1000;
        let captures_dir = screenshot_captures_dir();
        let (deleted_count, deleted_screenshot_count, freed_bytes) =
            storage.run_old_captures_cleanup(cutoff, &captures_dir)?;
        storage.delete_capture_attempts_before(cutoff)?;
        Ok::<_, crate::storage::StorageError>((
            retention_days,
            deleted_count,
            deleted_screenshot_count,
            freed_bytes,
        ))
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(RunCaptureCleanupResponse {
        retention_days: result.0,
        deleted_count: result.1,
        deleted_screenshot_count: result.2,
        freed_bytes: result.3,
    }))
}

pub async fn update_preference(
    State(state): State<Arc<AppState>>,
    Path(key): Path<String>,
    Json(body): Json<UpdatePreferenceRequest>,
) -> Result<Json<UpdatePreferenceResponse>, ApiError> {
    if key.is_empty() {
        return Err(ApiError::BadRequest("key 不能为空".into()));
    }
    let capture_interval = if key == "privacy.capture_interval_sec" {
        let value = body
            .value
            .trim()
            .parse::<u64>()
            .map_err(|_| ApiError::BadRequest("采集间隔必须是 5–120 秒之间的整数".into()))?;
        if !(5..=120).contains(&value) {
            return Err(ApiError::BadRequest(
                "采集间隔必须是 5–120 秒之间的整数".into(),
            ));
        }
        Some(value)
    } else {
        None
    };
    let keyboard_signal_enabled = if key == "privacy.keyboard_capture" {
        match body.value.trim().to_ascii_lowercase().as_str() {
            "true" | "1" | "yes" | "on" => Some(true),
            "false" | "0" | "no" | "off" => Some(false),
            _ => {
                return Err(ApiError::BadRequest("键盘活动信号开关必须是布尔值".into()));
            }
        }
    } else {
        None
    };

    let key_clone = key.clone();
    let value_clone = body.value.clone();
    let storage = state.storage.clone();

    let record = tokio::task::spawn_blocking(move || {
        // 用户手动设置：source="user"，confidence=1.0
        storage.upsert_preference(&key_clone, &value_clone, "user", 1.0)?;
        storage.get_preference(&key_clone)?.ok_or_else(|| {
            crate::storage::StorageError::NotFound(format!(
                "preference '{key_clone}' not found after upsert"
            ))
        })
    })
    .await
    .map_err(|e| ApiError::Internal(e.to_string()))??;

    if let Some(interval_secs) = capture_interval {
        state.update_capture_interval(interval_secs);
    }
    if let Some(enabled) = keyboard_signal_enabled {
        state.update_keyboard_signal_enabled(enabled);
    }

    Ok(Json(UpdatePreferenceResponse {
        key: record.key,
        value: record.value,
        updated_at: record.updated_at,
    }))
}
