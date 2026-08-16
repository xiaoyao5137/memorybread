//! 定时任务 API 处理器

use std::sync::Arc;

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use chrono::Utc;
use serde::Deserialize;

use crate::api::{error::ApiError, state::AppState};
use crate::scheduler::{
    cron_expression::next_run_at_ms,
    models::{NewScheduledTask, UpdateScheduledTask},
    notification_repo::NotificationChannelRepo,
    repo::TaskRepo,
};

/// POST /api/tasks - 创建任务
pub async fn create_task(
    State(state): State<Arc<AppState>>,
    Json(mut body): Json<NewScheduledTask>,
) -> Result<impl IntoResponse, ApiError> {
    body.notification_channel_ids = normalize_channel_ids(&body.notification_channel_ids);
    if !NotificationChannelRepo::all_exist(&state.storage, &body.notification_channel_ids)? {
        return Err(ApiError::BadRequest("消息渠道不存在".into()));
    }
    let (normalized_cron, next_run) = next_run_at_ms(&body.cron_expression)
        .map_err(|error| ApiError::BadRequest(format!("cron 表达式无效: {error}")))?;
    body.cron_expression = normalized_cron;

    let now_ms = Utc::now().timestamp_millis();
    let id = TaskRepo::create(&state.storage, &body, now_ms)?;
    TaskRepo::set_next_run(&state.storage, id, next_run)?;
    let task = TaskRepo::get(&state.storage, id)?.ok_or(ApiError::NotFound("task".into()))?;
    Ok((StatusCode::CREATED, Json(task)))
}

/// GET /api/tasks - 列出所有任务
pub async fn list_tasks(State(state): State<Arc<AppState>>) -> Result<impl IntoResponse, ApiError> {
    let tasks = TaskRepo::list_all(&state.storage)?;
    Ok(Json(
        serde_json::json!({ "tasks": tasks, "total": tasks.len() }),
    ))
}

/// GET /api/tasks/:id - 获取单个任务
pub async fn get_task(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    let task = TaskRepo::get(&state.storage, id)?.ok_or(ApiError::NotFound("task".into()))?;
    Ok(Json(task))
}

/// PUT /api/tasks/:id - 更新任务
pub async fn update_task(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(mut body): Json<UpdateScheduledTask>,
) -> Result<impl IntoResponse, ApiError> {
    if let Some(channel_ids) = body.notification_channel_ids.as_mut() {
        *channel_ids = normalize_channel_ids(channel_ids);
        if !NotificationChannelRepo::all_exist(&state.storage, channel_ids)? {
            return Err(ApiError::BadRequest("消息渠道不存在".into()));
        }
    }
    let next_run = if let Some(expression) = body.cron_expression.as_deref() {
        let (normalized, next_run) = next_run_at_ms(expression)
            .map_err(|error| ApiError::BadRequest(format!("cron 表达式无效: {error}")))?;
        body.cron_expression = Some(normalized);
        Some(next_run)
    } else {
        None
    };

    let now_ms = Utc::now().timestamp_millis();
    let updated = TaskRepo::update(&state.storage, id, &body, now_ms)?;
    if !updated {
        return Err(ApiError::NotFound("task".into()));
    }

    if let Some(next_run) = next_run {
        TaskRepo::set_next_run(&state.storage, id, next_run)?;
    }

    let task = TaskRepo::get(&state.storage, id)?.ok_or(ApiError::NotFound("task".into()))?;
    Ok(Json(task))
}

/// DELETE /api/tasks/:id - 删除任务
pub async fn delete_task(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    let task =
        TaskRepo::get(&state.storage, id)?.ok_or_else(|| ApiError::NotFound("task".into()))?;
    if !task.can_delete {
        return Err(ApiError::BadRequest(
            "内置日记任务不能删除，可关闭、编辑、执行或查看历史".into(),
        ));
    }
    let deleted = TaskRepo::delete(&state.storage, id)?;
    if !deleted {
        return Err(ApiError::NotFound("task".into()));
    }
    Ok(StatusCode::NO_CONTENT)
}

/// GET /api/tasks/:id/executions - 查询执行历史
pub async fn list_executions(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Query(params): Query<ExecutionQuery>,
) -> Result<impl IntoResponse, ApiError> {
    let limit = params.limit.unwrap_or(20).min(100);
    let executions = TaskRepo::list_executions(&state.storage, id, limit)?;
    Ok(Json(serde_json::json!({ "executions": executions })))
}

/// POST /api/tasks/:id/trigger - 手动立即触发任务
pub async fn trigger_task(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, ApiError> {
    // 确认任务存在
    TaskRepo::get(&state.storage, id)?.ok_or(ApiError::NotFound("task".into()))?;

    // 异步触发（不等待结果）
    let sidecar_url = state.sidecar_url.clone();
    tokio::spawn(async move {
        let client = reqwest::Client::new();
        let _ = client
            .post(format!("{}/tasks/execute", sidecar_url))
            .json(&serde_json::json!({ "task_id": id }))
            .timeout(std::time::Duration::from_secs(1800))
            .send()
            .await;
    });

    Ok(Json(
        serde_json::json!({ "message": "任务已触发", "task_id": id }),
    ))
}

#[derive(Debug, Deserialize)]
pub struct ExecutionQuery {
    pub limit: Option<i64>,
}

fn normalize_channel_ids(ids: &[i64]) -> Vec<i64> {
    ids.iter()
        .copied()
        .filter(|id| *id > 0)
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect()
}
