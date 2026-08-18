use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};

use crate::{
    api::{error::ApiError, state::AppState},
    services::bake_service::{
        BakeBucket, BakeCaptureFilter, BakeCapturePayload, BakeDocumentPayload,
        BakeExtractResponse, BakeKnowledgePayload, BakeListFilter, BakeListSort, BakeMemoryFilter,
        BakeMemoryPayload, BakeOverviewPayload, BakePagedResponse, BakeService, BakeSopPayload,
        BakeStyleConfig, CreateOrUpdateDocumentRequest, CreateOrUpdateKnowledgeRequest,
        CreateOrUpdateSopRequest, InitializeBakeMemoriesResponse, MAX_BAKE_RETRY_FAILURES,
    },
    storage::{
        db::current_ts_ms,
        models::{EventType, NewCapture},
        models_bake::{BakeArtifactAuditRecord, BakeQueueStatusRecord},
        repo::favorite::is_supported_favorite_kind,
    },
};

#[derive(serde::Deserialize)]
pub struct BakePaginationQuery {
    pub q: Option<String>,
    pub app: Option<String>,
    pub doc_type: Option<String>,
    pub favorite: Option<bool>,
    pub bucket: Option<String>,
    pub from: Option<i64>,
    pub to: Option<i64>,
    pub source_capture_id: Option<i64>,
    pub limit: Option<usize>,
    pub offset: Option<usize>,
    pub sort: Option<String>,
}

#[derive(serde::Serialize)]
pub struct BakeMemoriesResponse {
    pub articles: Vec<BakeMemoryPayload>,
    pub memories: Vec<BakeMemoryPayload>,
    pub total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(serde::Serialize)]
pub struct BakeKnowledgeResponse {
    pub items: Vec<BakeKnowledgePayload>,
    pub total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(serde::Serialize)]
pub struct BakeCapturesResponse {
    pub items: Vec<BakeCapturePayload>,
    pub total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(serde::Serialize)]
pub struct BakeSopsResponse {
    pub items: Vec<BakeSopPayload>,
    pub total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(serde::Serialize)]
pub struct BakeDocumentsResponse {
    pub items: Vec<BakeDocumentPayload>,
    pub total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(serde::Deserialize)]
pub struct InitializeBakeMemoriesRequest {
    pub limit: Option<usize>,
}

#[derive(serde::Deserialize)]
pub struct RunBakeRequest {
    pub trigger_reason: Option<String>,
    pub limit: Option<usize>,
    pub max_concurrency: Option<usize>,
}

#[derive(serde::Serialize)]
pub struct BakeQueueStatusResponse {
    #[serde(flatten)]
    pub queue: BakeQueueStatusRecord,
    pub capture_enabled: bool,
    pub running_count: i64,
}

#[derive(serde::Deserialize)]
pub struct UpdateMemoryFavoriteRequest {
    pub is_favorite: bool,
}

#[derive(serde::Serialize)]
pub struct MemoryFavoriteResponse {
    pub resource_kind: String,
    pub resource_id: i64,
    pub is_favorite: bool,
}

#[derive(serde::Serialize)]
pub struct BakeArtifactAuditsResponse {
    pub timeline_id: i64,
    pub items: Vec<BakeArtifactAuditRecord>,
}

pub async fn get_bake_artifact_audits(
    State(state): State<Arc<AppState>>,
    Path(timeline_id): Path<i64>,
    Query(params): Query<BakePaginationQuery>,
) -> Result<Json<BakeArtifactAuditsResponse>, ApiError> {
    let limit = params.limit.unwrap_or(30).clamp(1, 100);
    let items = state
        .storage
        .list_bake_artifact_audits_for_timeline(timeline_id, limit)?;
    Ok(Json(BakeArtifactAuditsResponse { timeline_id, items }))
}

pub async fn get_bake_queue_status(
    State(state): State<Arc<AppState>>,
) -> Result<Json<BakeQueueStatusResponse>, ApiError> {
    let queue = state
        .storage
        .get_bake_queue_status(MAX_BAKE_RETRY_FAILURES)?;
    Ok(Json(BakeQueueStatusResponse {
        queue,
        capture_enabled: state.is_capture_enabled(),
        running_count: state.storage.count_running_bake_runs().unwrap_or(0),
    }))
}

pub async fn update_memory_favorite(
    State(state): State<Arc<AppState>>,
    Path((resource_kind, resource_id)): Path<(String, i64)>,
    Json(body): Json<UpdateMemoryFavoriteRequest>,
) -> Result<Json<MemoryFavoriteResponse>, ApiError> {
    if !is_supported_favorite_kind(&resource_kind) {
        return Err(ApiError::BadRequest(format!(
            "unsupported favorite resource kind: {resource_kind}"
        )));
    }
    let storage = state.storage.clone();
    let kind_for_update = resource_kind.clone();
    let is_favorite = body.is_favorite;
    let updated = tokio::task::spawn_blocking(move || {
        storage.set_memory_favorite(&kind_for_update, resource_id, is_favorite)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;
    if !updated {
        return Err(ApiError::NotFound(format!(
            "{resource_kind} {resource_id} not found"
        )));
    }
    Ok(Json(MemoryFavoriteResponse {
        resource_kind,
        resource_id,
        is_favorite,
    }))
}

pub async fn get_bake_style_config(
    State(state): State<Arc<AppState>>,
) -> Result<Json<BakeStyleConfig>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let config = tokio::task::spawn_blocking(move || service.get_style_config())
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(config))
}

pub async fn update_bake_style_config(
    State(state): State<Arc<AppState>>,
    Json(body): Json<BakeStyleConfig>,
) -> Result<Json<BakeStyleConfig>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let config = tokio::task::spawn_blocking(move || service.save_style_config(&body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(config))
}

pub async fn list_bake_sops(
    State(state): State<Arc<AppState>>,
    Query(params): Query<BakePaginationQuery>,
) -> Result<Json<BakeSopsResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let limit = params.limit.unwrap_or(20).clamp(1, 100);
    let offset = params.offset.unwrap_or(0);
    let bucket = BakeBucket::from_query(params.bucket.as_deref())?;
    let filter = BakeListFilter {
        q: params.q.filter(|value| !value.trim().is_empty()),
        bucket,
        from_ts: params.from,
        to_ts: params.to,
        favorite: params.favorite,
        limit,
        offset,
        sort: BakeListSort::Recent,
    };
    let response: BakePagedResponse<BakeSopPayload> =
        tokio::task::spawn_blocking(move || service.list_sops_paginated(filter))
            .await
            .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(BakeSopsResponse {
        items: response.items,
        total: response.total,
        limit: response.limit,
        offset: response.offset,
    }))
}

pub async fn delete_bake_sop(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    tokio::task::spawn_blocking(move || service.delete_sop(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn create_bake_sop(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CreateOrUpdateSopRequest>,
) -> Result<Json<BakeSopPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let sop = tokio::task::spawn_blocking(move || service.create_sop(body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(sop))
}

pub async fn update_bake_sop(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<CreateOrUpdateSopRequest>,
) -> Result<Json<BakeSopPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let sop = tokio::task::spawn_blocking(move || service.update_sop(id, body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(sop))
}

pub async fn get_bake_sop(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeSopPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let sop = tokio::task::spawn_blocking(move || service.get_sop(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(sop))
}

pub async fn list_bake_documents(
    State(state): State<Arc<AppState>>,
    Query(params): Query<BakePaginationQuery>,
) -> Result<Json<BakeDocumentsResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let limit = params.limit.unwrap_or(20).clamp(1, 100);
    let offset = params.offset.unwrap_or(0);
    let bucket = BakeBucket::from_query(params.bucket.as_deref())?;
    let filter = BakeListFilter {
        q: params.q.filter(|value| !value.trim().is_empty()),
        bucket,
        from_ts: params.from,
        to_ts: params.to,
        favorite: params.favorite,
        limit,
        offset,
        sort: BakeListSort::Recent,
    };
    let doc_type = params.doc_type.filter(|value| !value.trim().is_empty());
    let response: BakePagedResponse<BakeDocumentPayload> = tokio::task::spawn_blocking(move || {
        service.list_documents_paginated_with_type(filter, doc_type.as_deref())
    })
    .await
    .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(BakeDocumentsResponse {
        items: response.items,
        total: response.total,
        limit: response.limit,
        offset: response.offset,
    }))
}

pub async fn create_bake_document(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CreateOrUpdateDocumentRequest>,
) -> Result<Json<BakeDocumentPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let document = tokio::task::spawn_blocking(move || service.create_document(body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(document))
}

pub async fn get_bake_document(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeDocumentPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let document = tokio::task::spawn_blocking(move || service.get_document(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(document))
}

pub async fn update_bake_document(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<CreateOrUpdateDocumentRequest>,
) -> Result<Json<BakeDocumentPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let document = tokio::task::spawn_blocking(move || service.update_document(id, body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(document))
}

pub async fn toggle_bake_document_status(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeDocumentPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let document = tokio::task::spawn_blocking(move || service.toggle_document_status(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(document))
}

pub async fn delete_bake_document(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    tokio::task::spawn_blocking(move || service.delete_document(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn list_bake_memories(
    State(state): State<Arc<AppState>>,
    Query(params): Query<BakePaginationQuery>,
) -> Result<Json<BakeMemoriesResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let limit = params.limit.unwrap_or(20).clamp(1, 100);
    let offset = params.offset.unwrap_or(0);
    let filter = BakeMemoryFilter {
        q: params.q.filter(|value| !value.trim().is_empty()),
        from_ts: params.from,
        to_ts: params.to,
        limit,
        offset,
    };
    let response: BakePagedResponse<BakeMemoryPayload> =
        tokio::task::spawn_blocking(move || service.list_memories_paginated(filter))
            .await
            .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(BakeMemoriesResponse {
        articles: response.items.clone(),
        memories: response.items,
        total: response.total,
        limit: response.limit,
        offset: response.offset,
    }))
}

pub async fn delete_bake_memory(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    tokio::task::spawn_blocking(move || service.delete_memory(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn list_bake_knowledge(
    State(state): State<Arc<AppState>>,
    Query(params): Query<BakePaginationQuery>,
) -> Result<impl IntoResponse, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let limit = params.limit.unwrap_or(20).clamp(1, 100);
    let offset = params.offset.unwrap_or(0);
    let bucket = BakeBucket::from_query(params.bucket.as_deref())?;
    let sort = match params.sort.as_deref() {
        None | Some("recent") => BakeListSort::Recent,
        Some("heat") => BakeListSort::Heat,
        Some(value) => {
            return Err(ApiError::BadRequest(format!(
                "unsupported bake knowledge sort: {value}"
            )))
        }
    };
    let filter = BakeListFilter {
        q: params.q.filter(|value| !value.trim().is_empty()),
        bucket,
        from_ts: params.from,
        to_ts: params.to,
        favorite: params.favorite,
        limit,
        offset,
        sort,
    };
    let response: BakePagedResponse<BakeKnowledgePayload> =
        tokio::task::spawn_blocking(move || service.list_knowledge_paginated(filter))
            .await
            .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok((
        [(header::CACHE_CONTROL, "no-cache, no-store, must-revalidate")],
        Json(BakeKnowledgeResponse {
            items: response.items,
            total: response.total,
            limit: response.limit,
            offset: response.offset,
        }),
    ))
}

pub async fn delete_bake_knowledge(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    tokio::task::spawn_blocking(move || service.delete_knowledge(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn create_bake_knowledge(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CreateOrUpdateKnowledgeRequest>,
) -> Result<Json<BakeKnowledgePayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let knowledge = tokio::task::spawn_blocking(move || service.create_knowledge(body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(knowledge))
}

pub async fn update_bake_knowledge(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<CreateOrUpdateKnowledgeRequest>,
) -> Result<Json<BakeKnowledgePayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let knowledge = tokio::task::spawn_blocking(move || service.update_knowledge(id, body))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(knowledge))
}

pub async fn get_bake_knowledge(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeKnowledgePayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let knowledge = tokio::task::spawn_blocking(move || service.get_knowledge(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(knowledge))
}

pub async fn list_bake_captures(
    State(state): State<Arc<AppState>>,
    Query(params): Query<BakePaginationQuery>,
) -> Result<Json<BakeCapturesResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let limit = params.limit.unwrap_or(20).clamp(1, 100);
    let offset = params.offset.unwrap_or(0);
    let filter = BakeCaptureFilter {
        q: params.q.filter(|value| !value.trim().is_empty()),
        app_name: params.app.filter(|value| !value.trim().is_empty()),
        from_ts: params.from,
        to_ts: params.to,
        source_capture_id: params.source_capture_id,
        limit,
        offset,
    };
    let response: BakePagedResponse<BakeCapturePayload> =
        tokio::task::spawn_blocking(move || service.list_capture_records_paginated(filter))
            .await
            .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(BakeCapturesResponse {
        items: response.items,
        total: response.total,
        limit: response.limit,
        offset: response.offset,
    }))
}

pub async fn get_bake_capture(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeCapturePayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let capture = tokio::task::spawn_blocking(move || service.get_capture_record(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(capture))
}

fn capture_assets_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".memory-bread").join("captures")
}

pub async fn delete_bake_capture(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let storage = state.storage.clone();
    let deleted = tokio::task::spawn_blocking(move || {
        storage.delete_capture_with_assets(id, &capture_assets_dir())
    })
    .await
    .map_err(|err| ApiError::Internal(err.to_string()))??;
    if !deleted {
        return Err(ApiError::NotFound(format!("capture {id} not found")));
    }
    Ok(StatusCode::NO_CONTENT)
}

/// 手动新建采集记录的请求体。
#[derive(serde::Deserialize)]
pub struct CreateManualCaptureRequest {
    /// 窗口/页面标题（必填）
    pub title: String,
    /// 应用名称（可选）
    pub app_name: Option<String>,
    /// 用户输入的文本信息（可选）
    pub text: Option<String>,
    /// 截图的 base64 编码（不含 data: 前缀，可选）
    pub screenshot_base64: Option<String>,
}

pub async fn create_manual_capture(
    State(state): State<Arc<AppState>>,
    Json(body): Json<CreateManualCaptureRequest>,
) -> Result<Json<BakeCapturePayload>, ApiError> {
    let title = body.title.trim().to_string();
    // 手工录入的记录标题与应用名均非必填，未填写时默认「手工录入」
    let title = if title.is_empty() {
        "手工录入".to_string()
    } else {
        title
    };
    let app_name = body
        .app_name
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "手工录入".to_string());

    let storage = state.storage.clone();
    let ts = current_ts_ms();

    // 处理截图：解码 base64 并保存到 captures/screenshots/
    let screenshot_path = if let Some(b64) = body.screenshot_base64.as_deref() {
        let b64_trimmed = b64.trim();
        if b64_trimmed.is_empty() {
            None
        } else {
            let screenshot_dir = capture_assets_dir().join("screenshots");
            std::fs::create_dir_all(&screenshot_dir)
                .map_err(|e| ApiError::Internal(format!("create screenshot dir: {e}")))?;

            let bytes = BASE64_STANDARD
                .decode(b64_trimmed)
                .map_err(|e| ApiError::BadRequest(format!("invalid base64 screenshot: {e}")))?;

            let filename = format!("manual-{ts}.jpg");
            let file_path = screenshot_dir.join(&filename);
            std::fs::write(&file_path, &bytes)
                .map_err(|e| ApiError::Internal(format!("write screenshot: {e}")))?;

            Some(format!("screenshots/{filename}"))
        }
    } else {
        None
    };

    let new_capture = NewCapture {
        ts,
        app_name: Some(app_name),
        app_bundle_id: None,
        win_title: Some(title),
        event_type: EventType::Manual,
        ax_text: None,
        ax_focused_role: None,
        ax_focused_id: None,
        ocr_text: None,
        screenshot_path: screenshot_path.clone(),
        screenshot_source: screenshot_path
            .as_ref()
            .map(|_| "manual_upload".to_string()),
        input_text: body.text.and_then(|v| {
            let trimmed = v.trim();
            if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            }
        }),
        is_sensitive: false,
        pii_scrubbed: false,
        url: None,
        webpage_title: None,
    };

    let capture_id = tokio::task::spawn_blocking(move || storage.insert_capture(&new_capture))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;

    // 返回创建的采集记录详情
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let capture = tokio::task::spawn_blocking(move || service.get_capture_record(capture_id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(capture))
}

pub async fn get_bake_capture_screenshot(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Response, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let capture = tokio::task::spawn_blocking(move || service.get_capture_record(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;

    let relative_path = capture
        .screenshot_path
        .ok_or_else(|| ApiError::NotFound(format!("capture {id} has no screenshot")))?;

    let full_path = capture_assets_dir().join(&relative_path);

    let bytes = tokio::fs::read(&full_path).await.map_err(|err| {
        ApiError::NotFound(format!("failed to read screenshot {relative_path}: {err}"))
    })?;

    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "image/jpeg")],
        bytes,
    )
        .into_response())
}

pub async fn initialize_bake_memories(
    State(state): State<Arc<AppState>>,
    Json(body): Json<InitializeBakeMemoriesRequest>,
) -> Result<Json<InitializeBakeMemoriesResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let limit = body.limit.unwrap_or(20).clamp(1, 100);
    let result = tokio::task::spawn_blocking(move || service.initialize_memories(limit))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(result))
}

pub async fn ignore_bake_memory(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeMemoryPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let memory = tokio::task::spawn_blocking(move || service.ignore_memory(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(memory))
}

pub async fn promote_bake_memory_to_document(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeDocumentPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let document = tokio::task::spawn_blocking(move || service.promote_memory_to_document(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(document))
}

pub async fn promote_bake_memory_to_sop(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeSopPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let sop = tokio::task::spawn_blocking(move || service.promote_memory_to_sop(id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(sop))
}

pub async fn run_bake_pipeline(
    State(state): State<Arc<AppState>>,
    Json(body): Json<RunBakeRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    // 启动时尚未超过阈值的遗留 run 会在运行期变 stale。每次触发前先收敛，
    // 避免历史 running 状态永久污染并发判断和监控告警。
    let recovered_stale_runs = state.storage.fail_stale_running_bake_runs()?;
    if recovered_stale_runs > 0 {
        tracing::warn!(
            "触发 bake 前已收敛 {} 个陈旧 running bake run",
            recovered_stale_runs
        );
    }

    if !state.is_capture_enabled() {
        return Ok(Json(serde_json::json!({
            "id": null,
            "status": "skipped",
            "reason": "capture and extraction are paused",
        })));
    }

    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let trigger_reason = body
        .trigger_reason
        .unwrap_or_else(|| "manual_debug".to_string());
    let limit = body.limit.unwrap_or(20).clamp(1, 100);
    let max_concurrency = body.max_concurrency.unwrap_or(3).clamp(1, 3);

    // 统一 bake pipeline 使用全局 watermark，多个 run 并发会重复扫描同一段历史候选，
    // 让监控页出现多个长期“生成中”占位，并拖慢队列收敛。
    const MAX_CONCURRENT_BAKE_RUNS: i64 = 1;
    let running_count = state.storage.count_running_bake_runs().unwrap_or(0);
    if running_count >= MAX_CONCURRENT_BAKE_RUNS {
        return Ok(Json(serde_json::json!({
            "id": null,
            "status": "skipped",
            "reason": format!("max {} concurrent bake runs reached", MAX_CONCURRENT_BAKE_RUNS),
        })));
    }

    // 在写入 bake_runs 之前使用 Core 的统一队列口径预检，避免每 30 秒制造一条
    // 空 completed run。Sidecar 也读取同一端点，不再自行扫描 SQLite。
    let queue = state
        .storage
        .get_bake_queue_status(MAX_BAKE_RETRY_FAILURES)?;
    if queue.actionable_count == 0 {
        return Ok(Json(serde_json::json!({
            "id": null,
            "status": "skipped",
            "reason": "no actionable bake candidates",
            "retry_after_ms": queue.recommended_retry_after_ms,
            "queue": queue,
        })));
    }

    // no_op 退避守卫：actionable 看似 >0 但最近连续 run 零进展，说明队列口径与
    // run 时选候不一致（或候选已被预筛光）。继续创建 run 只会每 30 秒空转一次，
    // 还会通过 hold_capture 抢占 capture 提炼的模型槽，直接跳过并要求调用方指数退避。
    const NO_PROGRESS_BACKOFF_THRESHOLD: i64 = 3;
    if queue.recent_no_progress_count >= NO_PROGRESS_BACKOFF_THRESHOLD {
        let retry_after_ms = (15_000_i64 << queue.recent_no_progress_count.min(6)).min(900_000);
        tracing::warn!(
            "bake run skipped: recent_no_progress_count={} actionable={} retry_after_ms={}",
            queue.recent_no_progress_count,
            queue.actionable_count,
            retry_after_ms,
        );
        return Ok(Json(serde_json::json!({
            "id": null,
            "status": "skipped",
            "reason": "no_progress_backoff",
            "retry_after_ms": retry_after_ms,
            "queue": queue,
        })));
    }

    let run_id = service.spawn_bake_pipeline(trigger_reason, limit, max_concurrency)?;
    // 把触发时刻的队列口径快照落到 run 上，便于事后核对 no_op 空转的口径漂移。
    if let Err(err) = state
        .storage
        .set_bake_run_trigger_actionable_count(run_id, queue.actionable_count)
    {
        tracing::warn!("bake run {run_id} 记录 trigger_actionable_count 失败: {err}");
    }
    Ok(Json(serde_json::json!({
        "id": run_id,
        "status": "accepted",
        "queue": queue,
    })))
}

pub async fn get_bake_memory_preview(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<BakeExtractResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let result = service.preview_memory(id, "manual_preview").await?;
    Ok(Json(result))
}

pub async fn get_bake_overview(
    State(state): State<Arc<AppState>>,
) -> Result<Json<BakeOverviewPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let overview = tokio::task::spawn_blocking(move || service.get_overview())
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(overview))
}
