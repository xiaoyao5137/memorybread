use std::sync::Arc;

use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde::Deserialize;
use uuid::Uuid;

use crate::{
    api::{error::ApiError, state::AppState},
    integration::{
        execute_integration_skill, integration_skill_bundle, integration_skill_catalog,
        integration_skill_detail, integration_skill_file, list_runs, run_input_summary,
        validate_run_request, RunIntegrationSkillRequest,
    },
};

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct IntegrationRunQuery {
    pub skill_id: Option<String>,
    pub limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
pub struct IntegrationFileQuery {
    pub path: String,
}

#[derive(Debug, Deserialize)]
pub struct MemoryOptionsQuery {
    pub q: Option<String>,
    pub limit: Option<usize>,
    pub offset: Option<usize>,
}

pub async fn list_integration_skills() -> impl IntoResponse {
    Json(integration_skill_catalog())
}

pub async fn list_integration_memory_options(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<MemoryOptionsQuery>,
) -> Result<impl IntoResponse, ApiError> {
    validate_local_execution_origin(&headers)?;
    let limit = query.limit.unwrap_or(60).clamp(1, 200);
    let offset = query.offset.unwrap_or(0);
    let keyword = query.q.unwrap_or_default();
    state
        .storage
        .integration_list_memory_options(&keyword, limit, offset)
        .map(Json)
        .map_err(|error| ApiError::Internal(format!("读取记忆候选失败: {error}")))
}

pub async fn get_integration_skill(
    Path(skill_id): Path<String>,
) -> Result<impl IntoResponse, ApiError> {
    integration_skill_detail(&skill_id)
        .map(Json)
        .ok_or_else(|| ApiError::NotFound("集成 Skill 不存在".to_string()))
}

pub async fn download_integration_skill_file(
    Path(skill_id): Path<String>,
    Query(query): Query<IntegrationFileQuery>,
) -> Result<Response, ApiError> {
    let (media_type, content) = integration_skill_file(&skill_id, &query.path)
        .ok_or_else(|| ApiError::NotFound("Skill 文件不存在".to_string()))?;
    let file_name = query.path.rsplit('/').next().unwrap_or("skill-file.txt");
    download_response(media_type, file_name, content.as_bytes().to_vec())
}

pub async fn download_integration_skill_bundle(
    Path(skill_id): Path<String>,
) -> Result<Response, ApiError> {
    let bundle = integration_skill_bundle(&skill_id)
        .ok_or_else(|| ApiError::NotFound("集成 Skill 不存在".to_string()))?;
    let bytes = serde_json::to_vec_pretty(&bundle)
        .map_err(|error| ApiError::Internal(format!("生成 Skill 包失败: {error}")))?;
    download_response(
        "application/json; charset=utf-8",
        &format!("memorybread-{skill_id}.skill.json"),
        bytes,
    )
}

pub async fn start_integration_skill_run(
    State(state): State<Arc<AppState>>,
    Path(skill_id): Path<String>,
    headers: HeaderMap,
    Json(request): Json<RunIntegrationSkillRequest>,
) -> Result<impl IntoResponse, ApiError> {
    validate_local_execution_origin(&headers)?;
    validate_run_request(&skill_id, &request)
        .map_err(|error| ApiError::BadRequest(error.message))?;
    let run_id = format!("integration-{}", Uuid::new_v4());
    let input_summary = run_input_summary(&request);
    let run = state.storage.create_integration_skill_run(
        &run_id,
        &skill_id,
        &request.mode,
        &input_summary,
    )?;

    let state_for_task = state.clone();
    let run_id_for_task = run_id.clone();
    tokio::spawn(async move {
        if let Err(error) = state_for_task
            .storage
            .start_integration_skill_run(&run_id_for_task)
        {
            tracing::error!(run_id = %run_id_for_task, %error, "启动集成 Skill 运行记录失败");
            let _ = state_for_task.storage.fail_integration_skill_run(
                &run_id_for_task,
                "RUN_START_FAILED",
                &format!("本地执行器启动失败: {error}"),
            );
            return;
        }
        let storage = state_for_task.storage.clone();
        let storage_for_execution = storage.clone();
        let run_id_for_execution = run_id_for_task.clone();
        let skill_id_for_execution = skill_id.clone();
        let outcome = tokio::task::spawn_blocking(move || {
            execute_integration_skill(
                &storage_for_execution,
                &run_id_for_execution,
                &skill_id_for_execution,
                &request,
            )
        })
        .await;
        match outcome {
            Ok(Ok(result)) => {
                if let Err(error) = storage.finish_integration_skill_run(&run_id_for_task, &result)
                {
                    tracing::error!(run_id = %run_id_for_task, %error, "保存集成 Skill 成功结果失败");
                }
            }
            Ok(Err(error)) => {
                if let Err(storage_error) =
                    storage.fail_integration_skill_run(&run_id_for_task, error.code, &error.message)
                {
                    tracing::error!(run_id = %run_id_for_task, %storage_error, "保存集成 Skill 失败结果失败");
                }
            }
            Err(error) => {
                let message = format!("本地执行任务异常结束: {error}");
                if let Err(storage_error) = storage.fail_integration_skill_run(
                    &run_id_for_task,
                    "EXECUTOR_JOIN_FAILED",
                    &message,
                ) {
                    tracing::error!(run_id = %run_id_for_task, %storage_error, "保存集成 Skill 异常结果失败");
                }
            }
        }
    });

    Ok((StatusCode::ACCEPTED, Json(run)))
}

fn validate_local_execution_origin(headers: &HeaderMap) -> Result<(), ApiError> {
    let Some(origin) = headers.get(header::ORIGIN) else {
        // 本机 CLI 与内部测试不会发送 Origin；浏览器跨域请求一定会发送。
        return Ok(());
    };
    let origin = origin.to_str().unwrap_or_default();
    let trusted = matches!(
        origin,
        "tauri://localhost"
            | "http://tauri.localhost"
            | "https://tauri.localhost"
            | "http://localhost:1420"
            | "http://127.0.0.1:1420"
    );
    if trusted {
        Ok(())
    } else {
        Err(ApiError::Upstream {
            status: StatusCode::FORBIDDEN,
            code: "UNTRUSTED_LOCAL_EXECUTION_ORIGIN",
            message: "只允许从记忆面包桌面客户端启动本地 Skill".to_string(),
        })
    }
}

pub async fn list_integration_skill_runs(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<IntegrationRunQuery>,
) -> Result<impl IntoResponse, ApiError> {
    validate_local_execution_origin(&headers)?;
    let runs = list_runs(
        &state.storage,
        query.skill_id.as_deref(),
        query.limit.unwrap_or(30).clamp(1, 100),
    )
    .map_err(|error| ApiError::Internal(error.message))?;
    Ok(Json(runs))
}

pub async fn get_integration_skill_run(
    State(state): State<Arc<AppState>>,
    Path(run_id): Path<String>,
    headers: HeaderMap,
) -> Result<impl IntoResponse, ApiError> {
    validate_local_execution_origin(&headers)?;
    state
        .storage
        .get_integration_skill_run(&run_id)?
        .map(Json)
        .ok_or_else(|| ApiError::NotFound("Skill 执行记录不存在".to_string()))
}

fn download_response(
    media_type: &str,
    file_name: &str,
    bytes: Vec<u8>,
) -> Result<Response, ApiError> {
    let safe_name = file_name
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '.' | '-' | '_') {
                character
            } else {
                '_'
            }
        })
        .collect::<String>();
    let mut response = Response::new(Body::from(bytes));
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(media_type)
            .map_err(|error| ApiError::Internal(format!("无效的文件类型: {error}")))?,
    );
    response.headers_mut().insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&format!("attachment; filename=\"{safe_name}\""))
            .map_err(|error| ApiError::Internal(format!("无效的下载文件名: {error}")))?,
    );
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_execution_rejects_external_browser_origins() {
        let mut trusted = HeaderMap::new();
        trusted.insert(
            header::ORIGIN,
            HeaderValue::from_static("tauri://localhost"),
        );
        assert!(validate_local_execution_origin(&trusted).is_ok());

        let mut external = HeaderMap::new();
        external.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://example.com"),
        );
        assert!(validate_local_execution_origin(&external).is_err());
    }
}
