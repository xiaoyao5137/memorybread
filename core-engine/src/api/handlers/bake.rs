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
    api::{
        error::ApiError,
        handlers::data::{
            looks_like_terminal_page, normalize_preview_id, scrape_browser_async,
            scrape_browser_extension_async, DataToolError, BROWSER_DOCUMENT_READY_POLL_ATTEMPTS,
        },
        state::AppState,
    },
    services::{
        bake_service::{
            BakeBucket, BakeCaptureFilter, BakeCapturePayload, BakeDocumentPayload,
            BakeExtractResponse, BakeKnowledgePayload, BakeListFilter, BakeListSort,
            BakeMemoryFilter, BakeMemoryPayload, BakeOverviewPayload, BakePagedResponse,
            BakeService, BakeSopPayload, BakeStyleConfig, CreateOrUpdateDocumentRequest,
            CreateOrUpdateKnowledgeRequest, CreateOrUpdateSopRequest,
            DocumentSourceSnapshotPayload, InitializeBakeMemoriesResponse,
            TimelineRelationsPayload, MAX_BAKE_RETRY_FAILURES,
        },
        document_refresh::{
            source_text_fingerprint, DocumentRefreshDecision, DocumentRefreshSkipReason,
            DOCUMENT_REFRESH_ERROR_PAGE_GONE,
        },
    },
    storage::{
        db::current_ts_ms,
        document_identity::canonical_document_identity,
        models::{EventType, NewCapture},
        models_bake::{
            BakeArtifactAuditRecord, BakeQueueStatusRecord, NewBakeDocumentSourceSnapshot,
        },
        repo::favorite::is_supported_favorite_kind,
    },
};

#[derive(serde::Deserialize)]
pub struct BakePaginationQuery {
    pub q: Option<String>,
    pub id: Option<i64>,
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

#[derive(serde::Deserialize)]
pub struct RefreshBakeDocumentRequest {
    /// 浏览器采集目标描述，透传给采集器引导读取。
    pub objective: Option<String>,
    /// 用户当前任务明确要求“最新/当前”时，可绕过普通内容 TTL，
    /// 但不能绕过 never、URL 安全、终态错误和 6 小时节流门禁。
    #[serde(default)]
    pub require_latest: bool,
    /// 开启网页爬虫时，文档刷新与报表刷新统一走 Chrome 扩展后台标签页。
    /// 关闭时保留兼容的 Apple Events 一次性浏览器会话。
    #[serde(default = "default_browser_extension_enabled")]
    pub browser_extension_enabled: bool,
}

fn default_browser_extension_enabled() -> bool {
    true
}

#[derive(serde::Deserialize)]
pub struct UpdateDocumentRefreshPolicyRequest {
    pub refresh_policy: String,
}

/// 刷新结果统一用 200 + status 表达，失败也带可落库原因，
/// 供创作召回端静默降级而不是报错中断。
#[derive(serde::Serialize)]
pub struct RefreshBakeDocumentResponse {
    pub status: String,
    pub reason: Option<String>,
    pub completeness_status: Option<String>,
    pub document: Option<BakeDocumentPayload>,
    pub source_snapshot: Option<DocumentSourceSnapshotPayload>,
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

pub async fn set_bake_document_refresh_policy(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<UpdateDocumentRefreshPolicyRequest>,
) -> Result<Json<BakeDocumentPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let document = tokio::task::spawn_blocking(move || {
        service.set_document_refresh_policy(id, body.refresh_policy.trim())
    })
    .await
    .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(document))
}

/// 创作链路召回文档后的浏览器即时刷新：直接创建一次性隐藏浏览器
/// 会话加载来源页面，不依赖用户预先打开匹配标签页；抓到的正文保存为
/// 独立来源快照，本轮直接消费，不覆盖烘焙文档。
pub async fn refresh_bake_document(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<RefreshBakeDocumentRequest>,
) -> Result<Json<RefreshBakeDocumentResponse>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let now = current_ts_ms();
    let (record, decision) =
        tokio::task::spawn_blocking(move || service.evaluate_document_refresh(id, now))
            .await
            .map_err(|err| ApiError::Internal(err.to_string()))??;

    if let DocumentRefreshDecision::Skip(reason) = decision {
        let latest_override = should_override_document_ttl(body.require_latest, reason);
        if latest_override {
            tracing::info!(
                document_id = id,
                "latest creation request overrides document content TTL"
            );
        } else if should_reuse_recent_document_snapshot(
            reason,
            record.last_refresh_success_at_ms,
            record.last_refresh_error.as_deref(),
        ) {
            let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
            if let Some(source_snapshot) = service.get_latest_document_source_snapshot(id)? {
                let completeness_status = Some(source_snapshot.completeness_status.clone());
                return Ok(Json(RefreshBakeDocumentResponse {
                    status: "reused".to_string(),
                    reason: Some(reason.as_str().to_string()),
                    completeness_status,
                    document: None,
                    source_snapshot: Some(source_snapshot),
                }));
            }
        } else {
            return Ok(Json(RefreshBakeDocumentResponse {
                status: "skipped".to_string(),
                reason: Some(reason.as_str().to_string()),
                completeness_status: None,
                document: None,
                source_snapshot: None,
            }));
        }
        if !latest_override {
            return Ok(Json(RefreshBakeDocumentResponse {
                status: "skipped".to_string(),
                reason: Some(reason.as_str().to_string()),
                completeness_status: None,
                document: None,
                source_snapshot: None,
            }));
        }
    }

    let url = record.source_url.clone().unwrap_or_default();
    let source_app_name = record.source_app_name.clone();
    let objective = body.objective.clone();
    let scrape_once = |preview_token: Option<String>| {
        scrape_browser_async(
            url.clone(),
            "auto".to_string(),
            source_app_name.clone(),
            preview_token,
            None,
            objective.clone(),
            None,
            None,
            None,
            BROWSER_DOCUMENT_READY_POLL_ATTEMPTS,
            false,
        )
    };

    let scrape_result = if body.browser_extension_enabled {
        tracing::info!(
            document_id = id,
            "文档即时刷新开始使用 Chrome 扩展后台标签页"
        );
        scrape_browser_extension_async(
            &state.browser_extension,
            url.clone(),
            objective.clone(),
            Vec::new(),
            None,
            None,
            None,
        )
        .await
    } else {
        // 兼容未开启网页爬虫的用户：沿用带唯一标识的一次性浏览器会话。
        let preview_token = normalize_preview_id(None).map_err(scrape_error_to_api)?;
        tracing::info!(document_id = id, "文档即时刷新开始使用一次性隐藏浏览器会话");
        scrape_once(Some(preview_token)).await
    };

    let result = match scrape_result {
        Ok(result) => result,
        Err(error) => {
            record_refresh_failure(&state, id, error.code(), now).await;
            return Ok(Json(RefreshBakeDocumentResponse {
                status: "failed".to_string(),
                reason: Some(error.code().to_string()),
                completeness_status: Some("failed".to_string()),
                document: None,
                source_snapshot: None,
            }));
        }
    };

    if looks_like_terminal_page(&result.title, &result.url, &result.content_text) {
        // 页面已不存在：终态错误永久阻止后续刷新，避免反复白开浏览器。
        record_refresh_failure(&state, id, DOCUMENT_REFRESH_ERROR_PAGE_GONE, now).await;
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "failed".to_string(),
            reason: Some(DOCUMENT_REFRESH_ERROR_PAGE_GONE.to_string()),
            completeness_status: Some("failed".to_string()),
            document: None,
            source_snapshot: None,
        }));
    }
    if result.content_text.trim().is_empty() {
        record_refresh_failure(&state, id, "SCRAPE_EMPTY", now).await;
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "failed".to_string(),
            reason: Some("SCRAPE_EMPTY".to_string()),
            completeness_status: Some("failed".to_string()),
            document: None,
            source_snapshot: None,
        }));
    }

    let identity_match = document_refresh_identity_matches(&url, &result.url);
    if !identity_match {
        record_refresh_failure(&state, id, "IDENTITY_MISMATCH", now).await;
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "failed".to_string(),
            reason: Some("IDENTITY_MISMATCH".to_string()),
            completeness_status: Some("failed".to_string()),
            document: None,
            source_snapshot: None,
        }));
    }

    let assessment =
        assess_document_refresh_completeness(&result.structured_data, &result.content_text);
    let content_hash = source_text_fingerprint(&result.content_text)
        .ok_or_else(|| ApiError::BadRequest("刷新抓取内容为空".to_string()))?;
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let (changed, source_snapshot) =
        service.record_document_refresh_snapshot(NewBakeDocumentSourceSnapshot {
            document_id: record.id,
            source_url: result.url,
            page_title: result.title,
            content_text: result.content_text,
            content_hash,
            completeness_status: assessment.completeness_status.clone(),
            identity_match,
            reached_end: assessment.reached_end,
            stable_passes: assessment.stable_passes,
            segment_count: assessment.segment_count,
            character_count: assessment.character_count,
            truncated: assessment.truncated,
            collector: "browser_attach".to_string(),
            collected_at: now,
        })?;
    let document = service.get_document(record.id)?;
    Ok(Json(RefreshBakeDocumentResponse {
        status: if changed { "updated" } else { "no_change" }.to_string(),
        reason: if changed {
            None
        } else {
            Some("source_fingerprint_already_seen".to_string())
        },
        completeness_status: Some(assessment.completeness_status),
        document: Some(document),
        source_snapshot: Some(source_snapshot),
    }))
}

fn should_override_document_ttl(require_latest: bool, reason: DocumentRefreshSkipReason) -> bool {
    require_latest
        && matches!(
            reason,
            DocumentRefreshSkipReason::NoUpdateEvidence | DocumentRefreshSkipReason::ContentFresh
        )
}

fn should_reuse_recent_document_snapshot(
    reason: DocumentRefreshSkipReason,
    last_refresh_success_at_ms: i64,
    last_refresh_error: Option<&str>,
) -> bool {
    reason == DocumentRefreshSkipReason::CheckThrottled
        && last_refresh_success_at_ms > 0
        && last_refresh_error.is_none()
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DocumentRefreshCompleteness {
    completeness_status: String,
    reached_end: bool,
    stable_passes: i64,
    segment_count: i64,
    character_count: i64,
    truncated: bool,
}

fn document_refresh_identity_matches(expected_url: &str, actual_url: &str) -> bool {
    match (
        canonical_document_identity(expected_url),
        canonical_document_identity(actual_url),
    ) {
        (Some(expected), Some(actual)) => expected == actual,
        _ => expected_url.trim() == actual_url.trim(),
    }
}

fn assess_document_refresh_completeness(
    structured_data: &serde_json::Value,
    content_text: &str,
) -> DocumentRefreshCompleteness {
    let scroll_capture = structured_data
        .get("scroll_capture")
        .and_then(serde_json::Value::as_object);
    let aggregated = scroll_capture
        .and_then(|value| value.get("aggregated"))
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    let segment_count = scroll_capture
        .and_then(|value| value.get("segment_count"))
        .and_then(serde_json::Value::as_i64)
        .unwrap_or(0);
    let geometry = scroll_capture
        .and_then(|value| value.get("geometry"))
        .and_then(serde_json::Value::as_object);
    let coverage_complete = geometry
        .and_then(|value| value.get("coverage_complete"))
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(!aggregated);
    let reached_end = geometry
        .and_then(|value| value.get("reached_end"))
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(!aggregated);
    let readiness_timed_out = structured_data
        .pointer("/page_state/readiness_timed_out")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    let character_count = content_text.chars().count() as i64;
    let truncated = (character_count >= 80_000 && content_text.ends_with('…'))
        || !coverage_complete
        || !reached_end;
    let stable_passes = if readiness_timed_out { 0 } else { 2 };
    let completeness_status = if !truncated && stable_passes >= 2 {
        "complete"
    } else {
        "partial"
    };
    DocumentRefreshCompleteness {
        completeness_status: completeness_status.to_string(),
        reached_end,
        stable_passes,
        segment_count,
        character_count,
        truncated,
    }
}

async fn record_refresh_failure(state: &Arc<AppState>, id: i64, error_code: &str, now: i64) {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let code = error_code.to_string();
    let _ = tokio::task::spawn_blocking(move || {
        service.record_document_refresh_failure(id, &code, now)
    })
    .await;
}

fn scrape_error_to_api(error: DataToolError) -> ApiError {
    ApiError::Upstream {
        status: error.status(),
        code: error.code(),
        message: error.message().to_string(),
    }
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

/// 定向查询时间线关联的知识/文档/操作/数据，供时间线详情回溯区使用，
/// 避免前端拉全量列表过滤时被分页上限截断。
pub async fn get_bake_memory_relations(
    State(state): State<Arc<AppState>>,
    Path(timeline_id): Path<i64>,
) -> Result<Json<TimelineRelationsPayload>, ApiError> {
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let payload = tokio::task::spawn_blocking(move || service.get_timeline_relations(timeline_id))
        .await
        .map_err(|err| ApiError::Internal(err.to_string()))??;
    Ok(Json(payload))
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
        source_capture_id: params.id.or(params.source_capture_id),
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
    if queue.recent_no_progress_count >= NO_PROGRESS_BACKOFF_THRESHOLD
        && queue.recommended_retry_after_ms > 0
    {
        let retry_after_ms = queue.recommended_retry_after_ms;
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

    if queue.recent_no_progress_count >= NO_PROGRESS_BACKOFF_THRESHOLD {
        tracing::info!(
            "bake no-progress backoff expired; allowing one half-open probe: count={} actionable={}",
            queue.recent_no_progress_count,
            queue.actionable_count,
        );
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

#[cfg(test)]
mod document_refresh_tests {
    use super::*;

    #[test]
    fn document_refresh_defaults_to_background_extension() {
        let request: RefreshBakeDocumentRequest =
            serde_json::from_value(serde_json::json!({})).unwrap();
        assert!(request.browser_extension_enabled);

        let disabled: RefreshBakeDocumentRequest =
            serde_json::from_value(serde_json::json!({"browser_extension_enabled": false}))
                .unwrap();
        assert!(!disabled.browser_extension_enabled);
    }

    #[test]
    fn document_identity_ignores_scheme_case_query_and_fragment() {
        assert!(document_refresh_identity_matches(
            "https://Docs.Example.com/d/home/ABC123?section=one#comment",
            "http://docs.example.com/d/home/abc123?section=two",
        ));
        assert!(!document_refresh_identity_matches(
            "https://docs.example.com/d/home/abc123",
            "https://docs.example.com/d/home/other",
        ));
    }

    #[test]
    fn static_dom_capture_is_complete_when_ready_and_not_truncated() {
        let assessment = assess_document_refresh_completeness(
            &serde_json::json!({"page_state": {"readiness_timed_out": false}}),
            "完整正文",
        );

        assert_eq!(assessment.completeness_status, "complete");
        assert!(assessment.reached_end);
        assert!(!assessment.truncated);
        assert_eq!(assessment.stable_passes, 2);
    }

    #[test]
    fn scroll_capture_is_partial_when_geometry_did_not_reach_end() {
        let assessment = assess_document_refresh_completeness(
            &serde_json::json!({
                "scroll_capture": {
                    "aggregated": true,
                    "segment_count": 20,
                    "geometry": {
                        "coverage_complete": false,
                        "reached_end": false
                    }
                },
                "page_state": {"readiness_timed_out": false}
            }),
            "只抓到前半部分",
        );

        assert_eq!(assessment.completeness_status, "partial");
        assert!(!assessment.reached_end);
        assert!(assessment.truncated);
        assert_eq!(assessment.segment_count, 20);
    }

    #[test]
    fn latest_override_only_bypasses_content_evidence_gates() {
        assert!(should_override_document_ttl(
            true,
            DocumentRefreshSkipReason::ContentFresh,
        ));
        assert!(should_override_document_ttl(
            true,
            DocumentRefreshSkipReason::NoUpdateEvidence,
        ));
        assert!(!should_override_document_ttl(
            true,
            DocumentRefreshSkipReason::PolicyNever,
        ));
        assert!(!should_override_document_ttl(
            true,
            DocumentRefreshSkipReason::CheckThrottled,
        ));
        assert!(!should_override_document_ttl(
            false,
            DocumentRefreshSkipReason::ContentFresh,
        ));
    }

    #[test]
    fn check_throttle_reuses_only_a_successful_snapshot() {
        assert!(should_reuse_recent_document_snapshot(
            DocumentRefreshSkipReason::CheckThrottled,
            100,
            None,
        ));
        assert!(!should_reuse_recent_document_snapshot(
            DocumentRefreshSkipReason::CheckThrottled,
            100,
            Some("SCRAPE_TIMEOUT"),
        ));
        assert!(!should_reuse_recent_document_snapshot(
            DocumentRefreshSkipReason::ContentFresh,
            100,
            None,
        ));
    }
}
