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
            looks_like_terminal_page, normalize_preview_id,
            DataToolError, BROWSER_DOCUMENT_READY_POLL_ATTEMPTS,
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
            DocumentRefreshConfig, DOCUMENT_REFRESH_CONFIG_KEY,
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
    pub source_url: Option<String>,
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
    /// Explicit one-shot refresh; does not change the saved automatic policy.
    #[serde(default)]
    pub manual: bool,
    /// 开启网页爬虫时，文档刷新与报表刷新统一走 Chrome 扩展后台标签页。
    /// 关闭时保留兼容的 Apple Events 一次性浏览器会话。
    #[serde(default = "default_browser_extension_enabled")]
    pub browser_extension_enabled: bool,
}

fn default_browser_extension_enabled() -> bool {
    true
}

/// One durable worker, sharing the manual refresh lease and existing privacy /
/// identity / coverage gates. No automatic request can bypass `never`.
pub async fn run_document_refresh_worker(state: Arc<AppState>) {
    loop {
        let poll_seconds = load_document_refresh_config(&state).map(|c| c.poll_seconds).unwrap_or(15);
        tokio::time::sleep(std::time::Duration::from_secs(poll_seconds)).await;
        let config = match load_document_refresh_config(&state) {
            Ok(config) => config,
            Err(_) => { tracing::warn!("invalid document refresh configuration; worker paused"); continue; }
        };
        if !config.enabled || !config.automatic_enabled || !config.automatic_browser_reads_enabled
            || !config.source_writes_enabled { continue; }
        if !state.browser_extension.status().connected { continue; }
        let now = chrono::Utc::now().timestamp_millis();
        let job = match state.storage.claim_document_refresh_job_in_scope(now, config.max_attempts, config.rollout_document_ids.as_deref()) {
            Ok(Some(job)) => job,
            Ok(None) => continue,
            Err(_) => { tracing::warn!(code="REFRESH_QUEUE_CLAIM_FAILED", "document refresh queue claim failed"); continue; }
        };
        let response = refresh_document_source(state.clone(), job.document_id,
            RefreshBakeDocumentRequest { objective: None, require_latest: false,
                manual: false, browser_extension_enabled: true }, true, Some(&job.lease_id)).await;
        let (complete, permanent, reason, snapshot) = match response {
            Ok(Json(response)) => {
                // Reused snapshots did not check the newly observed revision.
                let complete = matches!(response.status.as_str(), "updated" | "no_change")
                    && response.completeness_status.as_deref() == Some("complete");
                let reason = if !complete && matches!(response.status.as_str(), "updated" | "no_change") {
                    "COVERAGE_UNVERIFIED".to_string()
                } else {
                    response.reason.unwrap_or_else(|| "COVERAGE_UNVERIFIED".to_string())
                };
                let permanent = matches!(reason.as_str(), "policy_never" | "url_missing" | "url_invalid"
                    | "page_gone" | "PAGE_GONE" | "AUTH_REQUIRED" | "PERMISSION_DENIED" | "IDENTITY_MISMATCH" | "SOURCE_REFRESH_CANCELLED");
                (complete, permanent, reason, response.source_snapshot.map(|s| s.id))
            }
            Err(_) => (false, false, "SOURCE_REFRESH_FAILED".to_string(), None),
        };
        let next_state = if matches!(reason.as_str(), "SOURCE_REFRESH_PAUSED" | "SOURCE_WRITES_PAUSED") { "pending" }
            else if complete { "completed" } else if permanent || job.attempts >= config.max_attempts { "blocked" } else { "pending" };
        if state.storage.finish_document_refresh_job(&job, next_state,
            if complete { None } else { Some(&reason) }, snapshot,
            chrono::Utc::now().timestamp_millis(), config.retry_delay_ms(job.attempts)).is_err() {
            tracing::warn!(code="REFRESH_QUEUE_FINISH_FAILED", document_id=job.document_id, "document refresh queue finish failed");
        }
    }
}

pub async fn regenerate_bake_document_summary(State(state):State<Arc<AppState>>,Path(id):Path<i64>,
    Json(request):Json<serde_json::Value>)->Result<Json<serde_json::Value>,ApiError> {
    let revision=request.get("expected_updated_at").and_then(serde_json::Value::as_i64)
        .filter(|v|*v>0).ok_or_else(||ApiError::BadRequest("expected_updated_at is required".into()))?;
    if state.storage.get_bake_document(id)?.is_none() {return Err(ApiError::NotFound("document not found".into()));}
    let queued=state.storage.regenerate_document_summary(id,revision,chrono::Utc::now().timestamp_millis())?;
    Ok(Json(serde_json::json!({"document_id":id,"queued":queued})))
}

/// Summary inference has its own durable lease and never requires browser focus.
pub async fn retry_bake_document_summary(State(state):State<Arc<AppState>>,Path(id):Path<i64>)
    ->Result<Json<serde_json::Value>,ApiError> {
    if state.storage.get_bake_document(id)?.is_none() {return Err(ApiError::NotFound("document not found".into()));}
    let queued=state.storage.retry_document_summary(id,chrono::Utc::now().timestamp_millis())?;
    Ok(Json(serde_json::json!({"document_id":id,"queued":queued})))
}

/// Summary inference has its own durable lease and never requires browser focus.
pub async fn run_document_summary_worker(state:Arc<AppState>) {
    let client=reqwest::Client::builder().no_proxy().build().expect("local summary client");
    loop {
        let config=load_document_refresh_config(&state).unwrap_or_default();
        tokio::time::sleep(std::time::Duration::from_secs(config.poll_seconds)).await;
        if process_document_summary_job(state.clone(),&client).await.is_err() {
            tracing::warn!(code="SUMMARY_WORKER_FAILED","document summary worker failed");
        }
    }
}

async fn process_document_summary_job(state:Arc<AppState>,client:&reqwest::Client) -> Result<(),ApiError> {
    let config=load_document_refresh_config(&state)?;
    let now=chrono::Utc::now().timestamp_millis();
    let Some(job)=state.storage.claim_document_summary_job(now)? else {return Ok(());};
    let response=client.post(format!("{}/bake/document_summary",state.sidecar_url.trim_end_matches('/')))
        .json(&job).timeout(std::time::Duration::from_secs(config.summary_execution_seconds)).send().await;
    let mut reason="SUMMARY_GENERATION_FAILED";
    let mut permanent=false;
    let mut paused=false;
    if let Ok(response)=response {
        permanent=response.status()==reqwest::StatusCode::PAYLOAD_TOO_LARGE;
        if permanent {reason="SUMMARY_INPUT_BUDGET";}
        if response.status().is_success() {
            if let Ok(value)=response.json::<serde_json::Value>().await {
                let summary=value.get("summary").and_then(serde_json::Value::as_str);
                let quotes=value.get("evidence_quotes").and_then(serde_json::Value::as_array);
                let valid=value.get("document_id").and_then(serde_json::Value::as_i64)==Some(job.document_id)
                    && value.get("source_snapshot_id").and_then(serde_json::Value::as_i64)==Some(job.source_snapshot_id)
                    && value.get("expected_updated_at").and_then(serde_json::Value::as_i64)==Some(job.expected_updated_at)
                    && value.get("generation_version").and_then(serde_json::Value::as_str)==Some("document-summary.v1")
                    && summary.is_some_and(|s|!s.trim().is_empty() && s.chars().count()<=500)
                    && quotes.is_some_and(|qs|!qs.is_empty() && qs.iter().all(|q|
                        q.as_str().is_some_and(|s|!s.trim().is_empty() && s.chars().count()<=300 && job.content_text.contains(s)))
                        && qs.iter().filter_map(serde_json::Value::as_str).map(|s|s.chars().count()).sum::<usize>()
                            <=job.content_text.chars().count());
                if valid {
                    match state.storage.publish_document_summary_job(&job,summary.unwrap()) {
                        Ok(true)=>return Ok(()),
                        Ok(false)=>reason="SUMMARY_SOURCE_CHANGED",
                        Err(crate::storage::error::StorageError::DocumentSourceWritesPaused)
                        |Err(crate::storage::error::StorageError::DocumentAutomaticWritesPaused{..})=>{
                            reason="SUMMARY_PAUSED";paused=true;
                        }
                        Err(error)=>return Err(error.into()),
                    }
                } else {reason="SUMMARY_INVALID_RESULT";}
            }
        }
    }
    state.storage.finish_document_summary_job(&job,chrono::Utc::now().timestamp_millis(),
        config.retry_delay_ms(job.attempts),config.max_attempts,permanent,paused,reason)?;
    Ok(())
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
    if let Some(url) = params.source_url.as_deref() {
        let items = match state.storage.find_document_by_source_url(url)? {
            Some(document) => vec![service.get_document(document.id)?],
            None => Vec::new(),
        };
        return Ok(Json(BakeDocumentsResponse {
            total: items.len() as i64, items, limit: 1, offset: 0,
        }));
    }
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

static DOCUMENT_REFRESH_INFLIGHT: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<i64,Arc<DocumentRefreshControl>>>> = std::sync::OnceLock::new();

struct DocumentRefreshControl {
    phase: std::sync::atomic::AtomicU8,
    scope: String,
    script_cancelled: Arc<std::sync::atomic::AtomicBool>,
}
impl DocumentRefreshControl {
    fn cancel(&self) -> bool {
        let cancelled = self.phase.compare_exchange(0,1,std::sync::atomic::Ordering::SeqCst,std::sync::atomic::Ordering::SeqCst)
            .map(|_|true).unwrap_or_else(|phase|phase==1);
        if cancelled { self.script_cancelled.store(true,std::sync::atomic::Ordering::SeqCst); }
        cancelled
    }
    fn begin_commit(&self) -> bool {
        self.phase.compare_exchange(0,2,std::sync::atomic::Ordering::SeqCst,std::sync::atomic::Ordering::SeqCst).is_ok()
    }
}

struct DocumentRefreshLease(i64,Arc<DocumentRefreshControl>);
impl DocumentRefreshLease {
    fn acquire(id: i64) -> Option<Self> {
        let mut ids = DOCUMENT_REFRESH_INFLIGHT.get_or_init(Default::default)
            .lock().unwrap_or_else(|error| error.into_inner());
        if ids.contains_key(&id) { return None; }
        let control=Arc::new(DocumentRefreshControl {phase:std::sync::atomic::AtomicU8::new(0),scope:uuid::Uuid::new_v4().to_string(),
            script_cancelled:Arc::new(std::sync::atomic::AtomicBool::new(false))});
        ids.insert(id,control.clone());
        Some(Self(id,control))
    }
}

pub async fn cancel_bake_document_refresh(State(state):State<Arc<AppState>>,Path(id):Path<i64>)
    -> Result<Json<serde_json::Value>,ApiError> {
    let control=DOCUMENT_REFRESH_INFLIGHT.get_or_init(Default::default).lock()
        .unwrap_or_else(|error|error.into_inner()).get(&id).cloned();
    let was_running=control.is_some();
    if let Some(control)=control {
        if !control.cancel() { return Ok(Json(serde_json::json!({"status":"finishing"}))); }
        state.browser_extension.cancel_scope(&control.scope);
    }
    let count=state.storage.cancel_document_refresh_observations(id,chrono::Utc::now().timestamp_millis())?;
    Ok(Json(serde_json::json!({"status":if was_running || count>0 {"cancelled"} else {"idle"},"observations":count})))
}
impl Drop for DocumentRefreshLease {
    fn drop(&mut self) {
        if let Some(ids) = DOCUMENT_REFRESH_INFLIGHT.get() {
            ids.lock().unwrap_or_else(|error| error.into_inner()).remove(&self.0);
        }
    }
}

/// Collect a versioned source; apply verified complete bodies atomically.
pub async fn refresh_bake_document(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<RefreshBakeDocumentRequest>,
) -> Result<Json<RefreshBakeDocumentResponse>, ApiError> {
    refresh_document_source(state, id, body, false, None).await
}

async fn refresh_document_source(state: Arc<AppState>, id: i64,
    body: RefreshBakeDocumentRequest, observed_update: bool, observation_lease: Option<&str>,
) -> Result<Json<RefreshBakeDocumentResponse>, ApiError> {
    let started_at = chrono::Utc::now().timestamp_millis();
    let started = std::time::Instant::now();
    match refresh_document_source_inner(state.clone(), id, body, observed_update, observation_lease).await {
        Err(ApiError::Storage(crate::storage::StorageError::DocumentSourceWritesPaused)) => {
            // A pause that wins before a commit must not acknowledge the new
            // observation or leave a newly collected version marked current.
            record_refresh_failure(&state, id, "SOURCE_WRITES_PAUSED", started_at, started.elapsed()).await?;
            Ok(Json(RefreshBakeDocumentResponse {
                status: "skipped".into(), reason: Some("SOURCE_WRITES_PAUSED".into()),
                completeness_status: None, document: None, source_snapshot: None,
            }))
        }
        result => result,
    }
}

async fn refresh_document_source_inner(state: Arc<AppState>, id: i64,
    body: RefreshBakeDocumentRequest, observed_update: bool, observation_lease: Option<&str>,
) -> Result<Json<RefreshBakeDocumentResponse>, ApiError> {
    let config = load_document_refresh_config(&state)?;
    if !config.enabled || !config.permits_source_write(id) || (!body.manual && !config.automatic_enabled) {
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "skipped".into(), reason: Some("SOURCE_REFRESH_PAUSED".into()),
            completeness_status: None, document: None, source_snapshot: None,
        }));
    }
    let Some(_lease) = DocumentRefreshLease::acquire(id) else {
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "skipped".to_string(), reason: Some("refresh_in_progress".to_string()),
            completeness_status: None, document: None, source_snapshot: None,
        }));
    };
    let service = BakeService::new(state.storage.clone(), state.sidecar_url.clone());
    let now = current_ts_ms();
    let check_started = std::time::Instant::now();
    let (record, decision) =
        tokio::task::spawn_blocking(move || service.evaluate_document_refresh(id, now))
            .await
            .map_err(|err| ApiError::Internal(err.to_string()))??;

    if let DocumentRefreshDecision::Skip(reason) = decision {
        let manual_override = body.manual
            && now.saturating_sub(record.last_refresh_checked_at_ms) >= 30_000
            && matches!(reason, DocumentRefreshSkipReason::PolicyNever
                | DocumentRefreshSkipReason::CheckThrottled | DocumentRefreshSkipReason::ContentFresh
                | DocumentRefreshSkipReason::NoUpdateEvidence);
        let observation_override = should_override_observation_refresh(observed_update,
            now.saturating_sub(record.last_refresh_checked_at_ms), reason);
        let latest_override = manual_override || observation_override || should_override_document_ttl(body.require_latest, reason);
        if latest_override {
            tracing::info!(
                document_id = id,
                "latest creation request overrides document content TTL"
            );
        } else if should_reuse_recent_document_snapshot(
            reason,
            record.last_refresh_success_at_ms,
            record.last_refresh_error.as_deref(),
        ) && record.last_refresh_status == "fresh_complete" {
            if let Some(source_snapshot) = state.storage.get_current_verified_document_source_snapshot(id)? {
                let completeness_status = Some(source_snapshot.completeness_status.clone());
                return Ok(Json(RefreshBakeDocumentResponse {
                    status: "reused".to_string(),
                    reason: Some(reason.as_str().to_string()),
                    completeness_status,
                    document: None,
                    source_snapshot: Some(source_snapshot.into()),
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
        super::data::scrape_browser_controlled_async(
            url.clone(),
            "auto".to_string(),
            source_app_name.clone(),
            preview_token,
            None,
            objective.clone(),
            None,
            None,
            None,
            BROWSER_DOCUMENT_READY_POLL_ATTEMPTS.min(config.max_steps),
            false,
            Some(super::browser_script_control::ScriptControl::new(_lease.1.script_cancelled.clone(),
                std::time::Duration::from_secs(config.execution_seconds))),
        )
    };

    let scrape_result = if body.browser_extension_enabled {
        tracing::info!(
            document_id = id,
            "文档即时刷新开始使用 Chrome 扩展后台标签页"
        );
        super::data::scrape_browser_extension_scoped_async(
            &state.browser_extension,
            url.clone(),
            objective.clone(),
            Vec::new(),
            None,
            None,
            None,
            "document",
            Some(_lease.1.scope.clone()),
            Some(&config),
        )
        .await
    } else {
        // 兼容未开启网页爬虫的用户：沿用带唯一标识的一次性浏览器会话。
        let preview_token = normalize_preview_id(None).map_err(scrape_error_to_api)?;
        tracing::info!(document_id = id, "文档即时刷新开始使用一次性隐藏浏览器会话");
        scrape_once(Some(preview_token)).await
    };

    let mut result = match scrape_result {
        Ok(result) => result,
        Err(error) => {
            record_refresh_failure(&state, id, error.code(), now, check_started.elapsed()).await?;
            return Ok(Json(RefreshBakeDocumentResponse {
                status: "failed".to_string(),
                reason: Some(error.code().to_string()),
                completeness_status: Some("failed".to_string()),
                document: None,
                source_snapshot: None,
            }));
        }
    };

    // Apply the same privacy rules as passive captures before any persistence.
    let source_character_count = result.content_text.chars().count();
    let filtered = crate::capture::content_filter::ContentFilter::from_storage(&state.storage)
        .filter_text(&result.content_text);
    let redacted = filtered.redacted_count > 0;
    result.content_text = filtered.text;
    let failure_measurements = (source_character_count, result.content_text.chars().count(), filtered.redacted_characters);

    if looks_like_terminal_page(&result.title, &result.url, &result.content_text) {
        // 页面已不存在：终态错误永久阻止后续刷新，避免反复白开浏览器。
        record_refresh_failure_measured(&state, id, DOCUMENT_REFRESH_ERROR_PAGE_GONE, now, check_started.elapsed(), Some(failure_measurements)).await?;
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "failed".to_string(),
            reason: Some(DOCUMENT_REFRESH_ERROR_PAGE_GONE.to_string()),
            completeness_status: Some("failed".to_string()),
            document: None,
            source_snapshot: None,
        }));
    }
    if result.content_text.trim().is_empty()
        || crate::services::bake_service::is_document_shell(&result.content_text) {
        record_refresh_failure_measured(&state, id, "SCRAPE_EMPTY", now, check_started.elapsed(), Some(failure_measurements)).await?;
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
        record_refresh_failure_measured(&state, id, "IDENTITY_MISMATCH", now, check_started.elapsed(), Some(failure_measurements)).await?;
        return Ok(Json(RefreshBakeDocumentResponse {
            status: "failed".to_string(),
            reason: Some("IDENTITY_MISMATCH".to_string()),
            completeness_status: Some("failed".to_string()),
            document: None,
            source_snapshot: None,
        }));
    }

    let mut assessment =
        assess_document_refresh_completeness(&result.structured_data, &result.content_text);
    let body_verified = result.structured_data.pointer("/document_body/quality")
        .and_then(serde_json::Value::as_str) == Some("substantive");
    if !body_verified || redacted {
        assessment.completeness_status = "partial".to_string();
    }
    // Log only typed coverage statistics, never page text, URLs or arbitrary
    // extension payloads. Keep collector coverage distinct from privacy loss.
    tracing::info!(
        document_id = id,
        body_verified,
        redaction_count = filtered.redacted_count,
        covers_observed = ?result.structured_data.pointer("/document_body/final_snapshot_covers_observed").and_then(serde_json::Value::as_bool),
        virtualized_or_changed = ?result.structured_data.pointer("/document_body/virtualized_or_changed").and_then(serde_json::Value::as_bool),
        final_stable_passes = ?result.structured_data.pointer("/document_body/final_stable_passes").and_then(serde_json::Value::as_i64),
        positioned_matching_passes = ?result.structured_data.pointer("/document_body/matching_passes").and_then(serde_json::Value::as_i64),
        collector_complete = ?result.structured_data.pointer("/completeness/status").and_then(serde_json::Value::as_str).map(|status| status == "complete"),
        collector_truncated = ?result.structured_data.pointer("/completeness/truncated").and_then(serde_json::Value::as_bool),
        reached_end = assessment.reached_end,
        "document refresh coverage assessment"
    );
    let content_hash = source_text_fingerprint(&result.content_text)
        .ok_or_else(|| ApiError::BadRequest("刷新抓取内容为空".to_string()))?;
    let mut source_evidence = crate::storage::repo::document_source_checks::build_document_source_evidence(
        &result.structured_data, &result.content_text, &assessment.completeness_status,
        filtered.redacted_count as u64);
    source_evidence["source_character_count"]=serde_json::json!(source_character_count);
    source_evidence["redacted_characters"]=serde_json::json!(filtered.redacted_characters);
    source_evidence["redaction_fraction"]=serde_json::json!(if source_character_count==0 {0.0} else {filtered.redacted_characters as f64/source_character_count as f64});
    add_document_check_timing(&mut source_evidence, now, check_started.elapsed());
    let active_observation = match observation_lease {
        Some(lease) => state.storage.document_refresh_lease_active(lease)?,
        None => true,
    };
    if !active_observation || !_lease.1.begin_commit() {
        return record_cancelled_document_assessment(&state.storage, id, now, source_evidence);
    }
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
            collector: if body_verified {
                result.structured_data.pointer("/document_body/version")
                    .and_then(serde_json::Value::as_str)
                    .filter(|version| matches!(*version, "document-body.v2" | "document-body.v3"))
                    .unwrap_or("document-body.v2")
            } else { "browser_attach" }.to_string(),
            collected_at: now,
        })?;
    add_document_check_timing(&mut source_evidence, now, check_started.elapsed());
    record_snapshot_document_assessment(&state.storage, &source_snapshot, now,
        &result.structured_data, source_evidence)?;
    let applied = state.storage.apply_document_source_snapshot(source_snapshot.id)?;
    if assessment.completeness_status == "complete" {
        state.storage.acknowledge_document_observations(record.id, source_snapshot.id, now)?;
    }
    let document = service.get_document(record.id)?;
    Ok(Json(RefreshBakeDocumentResponse {
        status: if changed || applied { "updated" } else { "no_change" }.to_string(),
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

fn should_override_observation_refresh(observed: bool, elapsed_ms: i64, reason: DocumentRefreshSkipReason) -> bool {
    observed && elapsed_ms >= 30_000
        && matches!(reason, DocumentRefreshSkipReason::CheckThrottled
            | DocumentRefreshSkipReason::ContentFresh | DocumentRefreshSkipReason::NoUpdateEvidence)
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
    // The extension supplies explicit completeness; legacy collectors supply
    // scroll geometry. Missing evidence never implies a complete static page.
    let explicit = structured_data.get("completeness");
    let legacy = structured_data.get("scroll_capture");
    let reached_end = explicit.and_then(|v| v.get("reached_end"))
        .or_else(|| legacy.and_then(|v| v.pointer("/geometry/reached_end")))
        .and_then(serde_json::Value::as_bool).unwrap_or(false);
    let coverage_complete = explicit.and_then(|v| v.get("status"))
        .and_then(serde_json::Value::as_str).map(|v| v == "complete")
        .or_else(|| legacy.and_then(|v| v.pointer("/geometry/coverage_complete"))
            .and_then(serde_json::Value::as_bool)).unwrap_or(false);
    let readiness_timed_out = structured_data.pointer("/page_state/readiness_timed_out")
        .and_then(serde_json::Value::as_bool).unwrap_or(false);
    let stable_passes = if readiness_timed_out { 0 } else {
        explicit.and_then(|v| v.get("stable_passes"))
            .or_else(|| structured_data.pointer("/page_state/stable_passes"))
            .and_then(serde_json::Value::as_i64).unwrap_or(0).max(0)
    };
    let segment_count = explicit.and_then(|v| v.get("segment_count"))
        .or_else(|| legacy.and_then(|v| v.get("segment_count")))
        .and_then(serde_json::Value::as_i64).unwrap_or(0).max(0);
    let character_count = content_text.chars().count() as i64;
    let truncated = explicit.and_then(|v| v.get("truncated"))
        .and_then(serde_json::Value::as_bool).unwrap_or(false)
        || (character_count >= 80_000 && content_text.ends_with('…'))
        || !coverage_complete || !reached_end;
    let completeness_status = if !truncated && stable_passes >= 2 && character_count > 0 {
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

fn record_snapshot_document_assessment(storage: &crate::storage::StorageManager,
    snapshot: &crate::services::bake_service::DocumentSourceSnapshotPayload, now: i64,
    structured: &serde_json::Value, observed: serde_json::Value,
) -> Result<(), ApiError> {
    // Fingerprint deduplication may retain an older whitespace representation.
    // Offsets and hashes must describe the immutable snapshot actually referenced.
    let mut evidence=crate::storage::repo::document_source_checks::build_document_source_evidence(
        structured, &snapshot.content_text,
        observed["coverage"].as_str().unwrap_or("unverified"),
        observed["redaction_count"].as_u64().unwrap_or(0));
    for field in ["source_character_count","redacted_characters","redaction_fraction",
        "started_at_ms","finished_at_ms","execution_ms"] {
        if let Some(value)=observed.get(field) {evidence[field]=value.clone();}
    }
    storage.record_document_source_check(snapshot.document_id, Some(snapshot.id), now, &evidence)?;
    Ok(())
}

fn record_cancelled_document_assessment(storage: &crate::storage::StorageManager, id: i64,
    now: i64, mut evidence: serde_json::Value,
) -> Result<Json<RefreshBakeDocumentResponse>, ApiError> {
    // Cancellation is not a failed refresh and must not age or replace the
    // existing document. Keep the completed assessment without inventing a snapshot.
    evidence["assessed_coverage"] = evidence["coverage"].clone();
    evidence["coverage"] = serde_json::json!("cancelled");
    evidence["reason"] = serde_json::json!("SOURCE_REFRESH_CANCELLED");
    storage.record_document_source_check(id, None, now, &evidence)?;
    Ok(Json(RefreshBakeDocumentResponse {
        status: "cancelled".into(), reason: Some("SOURCE_REFRESH_CANCELLED".into()),
        completeness_status: None, document: None, source_snapshot: None,
    }))
}

fn add_document_check_timing(evidence: &mut serde_json::Value, started_at: i64, elapsed: std::time::Duration) {
    evidence["started_at_ms"] = serde_json::json!(started_at);
    evidence["finished_at_ms"] = serde_json::json!(current_ts_ms());
    // Monotonic elapsed time remains valid across wall-clock adjustments.
    evidence["execution_ms"] = serde_json::json!(elapsed.as_millis().min(u64::MAX as u128) as u64);
}

async fn record_refresh_failure(state: &Arc<AppState>, id: i64, error_code: &str, now: i64,
    elapsed: std::time::Duration) -> Result<(),ApiError> {
    record_refresh_failure_measured(state,id,error_code,now,elapsed,None).await
}

async fn record_refresh_failure_measured(state: &Arc<AppState>, id: i64, error_code: &str, now: i64,
    elapsed: std::time::Duration, measurements: Option<(usize,usize,usize)>) -> Result<(),ApiError> {
    let mut evidence = serde_json::json!({"coverage":"failed","reason":error_code,
        "body_character_count":null,"substantive_block_count":null,
        "excluded_block_count":null,"redaction_fraction":null,
        "completeness_evidence":null,
        "quality_version":crate::services::document_refresh::DOCUMENT_QUALITY_RULE_VERSION});
    if let Some((input,body,redacted))=measurements {
        evidence["source_character_count"]=serde_json::json!(input);
        evidence["body_character_count"]=serde_json::json!(body);
        evidence["redacted_characters"]=serde_json::json!(redacted);
        evidence["redaction_fraction"]=serde_json::json!(if input==0 {0.0} else {redacted as f64/input as f64});
    }
    add_document_check_timing(&mut evidence, now, elapsed);
    let storage = state.storage.clone();
    let code = error_code.to_string();
    tokio::task::spawn_blocking(move || storage.record_document_refresh_failure_check(id, now, &code, &evidence))
        .await.map_err(|error| ApiError::Internal(error.to_string()))??;
    Ok(())
}

fn load_document_refresh_config(state: &AppState) -> Result<DocumentRefreshConfig, ApiError> {
    match state.storage.get_preference_value(DOCUMENT_REFRESH_CONFIG_KEY)? {
        Some(value) => DocumentRefreshConfig::parse(&value).map_err(ApiError::BadRequest),
        None => Ok(DocumentRefreshConfig::default()),
    }
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
    if state
        .storage
        .set_bake_run_trigger_actionable_count(run_id, queue.actionable_count).is_err()
    {
        tracing::warn!(run_id, code="TRIGGER_COUNT_WRITE_FAILED", "bake trigger count write failed");
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

#[derive(serde::Deserialize)]
pub struct DocumentSourceHealthQuery {
    pub since_ms: Option<i64>,
}

pub async fn get_document_source_health(
    State(state): State<Arc<AppState>>,
    Query(query): Query<DocumentSourceHealthQuery>,
) -> Result<Json<serde_json::Value>,ApiError> {
    let now=current_ts_ms();
    let since=query.since_ms.unwrap_or(now-86_400_000);
    if since<0 || since>now || now-since>31*86_400_000 {
        return Err(ApiError::BadRequest("来源统计仅支持最近 31 天内的时间范围".into()));
    }
    let value=tokio::task::spawn_blocking(move || state.storage.document_source_health(since,now))
        .await.map_err(|e|ApiError::Internal(e.to_string()))??;
    Ok(Json(value))
}

#[cfg(test)]
mod document_refresh_tests {
    use super::*;
    #[test]
    fn document_reused_snapshot_audit_uses_retained_bytes() {
        use sha2::{Digest,Sha256};
        let db=crate::storage::StorageManager::open_in_memory().unwrap();
        let service=BakeService::new(db.clone(),String::new());
        db.with_conn(|c| {c.execute_batch("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at)
            VALUES(1,'test','document',1,1);")?;Ok(())}).unwrap();
        let retained="业务目标是保留已经验证的正文，同时让同一来源的后续访问可以更新文档。\n第二条事实说明摘要和索引必须绑定实际保存的来源版本，不能混用新旧内容。";
        let incoming=retained.replace('\n',"  \n  ");
        let mut input=NewBakeDocumentSourceSnapshot {
            document_id:1,source_url:"https://example.com/document".into(),page_title:"test".into(),
            content_text:retained.into(),content_hash:source_text_fingerprint(retained).unwrap(),
            completeness_status:"complete".into(),identity_match:true,reached_end:true,stable_passes:2,
            segment_count:1,character_count:retained.chars().count() as i64,truncated:false,
            collector:"document-body.v3".into(),collected_at:10,
        };
        let first=service.record_document_refresh_snapshot(input.clone()).unwrap().1;
        input.content_text=incoming.clone();input.content_hash=source_text_fingerprint(&incoming).unwrap();
        input.character_count=incoming.chars().count() as i64;input.collected_at=20;
        input.completeness_status="partial".into();input.truncated=true;
        let reused=service.record_document_refresh_snapshot(input).unwrap().1;
        assert_eq!(reused.completeness_status,"complete");
        db.with_conn(|c| {
            let state:(String,String,i64)=c.query_row("SELECT last_refresh_status,last_refresh_completeness,last_refresh_truncated FROM bake_documents WHERE id=1",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
            assert_eq!(state,("fresh_partial".into(),"partial".into(),1));
            Ok(())
        }).unwrap();
        assert_eq!(reused.id,first.id);
        assert_eq!(reused.content_text,retained);
        let structured=serde_json::json!({"document_body":{"version":"document-body.v3",
            "quality":"substantive","blocks":[{"type":"paragraph","text":incoming}]}});
        let mut observed=crate::storage::repo::document_source_checks::build_document_source_evidence(
            &structured,&incoming,"complete",0);
        observed["source_character_count"]=serde_json::json!(incoming.chars().count());
        add_document_check_timing(&mut observed,20,std::time::Duration::from_millis(30));
        record_snapshot_document_assessment(&db,&reused.into(),20,&structured,observed).unwrap();
        let check=db.latest_document_source_check(1).unwrap().unwrap();
        let evidence=&check["evidence"];
        assert_eq!(check["snapshot_id"],first.id);
        assert_eq!(evidence["snapshot_text_sha256"],format!("{:x}",Sha256::digest(retained.as_bytes())));
        assert_eq!(evidence["body_character_count"],retained.chars().count());
        assert_eq!(evidence["source_character_count"],incoming.chars().count());
        assert_eq!(evidence["blocks"][0]["end_byte"],retained.len());
        assert_eq!(evidence["unmatched_blocks"],0);
        assert_eq!(evidence["execution_ms"],30);
    }

    #[test]
    fn document_cancelled_assessment_preserves_current_record_and_measured_evidence() {
        let db=crate::storage::StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {c.execute_batch("INSERT INTO bake_documents(id,title,doc_type,full_content,summary,created_at,updated_at,
            last_refresh_checked_at_ms,last_refresh_success_at_ms,last_refresh_status)
            VALUES(1,'preserved','document','preserved body','preserved summary',1,2,3,3,'fresh_complete');")?;Ok(())}).unwrap();
        let structured=serde_json::json!({"document_body":{"version":"document-body.v3",
            "quality":"substantive","blocks":[{"text":"A newly collected paragraph with a specific fact.","type":"paragraph"}],
            "excluded_block_count":2},"completeness":{"reached_end":true,"stable_passes":2}});
        let mut evidence=crate::storage::repo::document_source_checks::build_document_source_evidence(
            &structured,"A newly collected paragraph with a specific fact.","complete",0);
        add_document_check_timing(&mut evidence,10,std::time::Duration::from_millis(25));
        let Json(response)=record_cancelled_document_assessment(&db,1,10,evidence).unwrap();
        assert_eq!(response.status,"cancelled");
        assert!(response.source_snapshot.is_none());
        let check=db.latest_document_source_check(1).unwrap().unwrap();
        assert!(check["snapshot_id"].is_null());
        assert_eq!(check["evidence"]["coverage"],"cancelled");
        assert_eq!(check["evidence"]["assessed_coverage"],"complete");
        assert_eq!(check["evidence"]["reason"],"SOURCE_REFRESH_CANCELLED");
        assert_eq!(check["evidence"]["excluded_block_count"],2);
        assert_eq!(check["evidence"]["execution_ms"],25);
        assert!(!check.to_string().contains("newly collected"));
        db.with_conn(|c| {
            let record:(String,String,i64,i64,i64,String)=c.query_row("SELECT full_content,summary,updated_at,
                last_refresh_checked_at_ms,last_refresh_success_at_ms,last_refresh_status FROM bake_documents WHERE id=1",
                [],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)))?;
            assert_eq!(record,("preserved body".into(),"preserved summary".into(),2,3,3,"fresh_complete".into()));
            for table in ["bake_document_source_snapshots","bake_document_source_heads","document_summary_jobs"] {
                assert_eq!(c.query_row(&format!("SELECT COUNT(*) FROM {table}"),[],|r|r.get::<_,i64>(0))?,0);
            }
            Ok(())
        }).unwrap();
        let health=db.document_source_health(0,20).unwrap();
        assert_eq!(health["checks"]["total"],1);
        assert_eq!(health["checks"]["complete"],0);
        assert_eq!(health["checks"]["failed"],0);
    }

    #[tokio::test]
    async fn document_summary_worker_publishes_only_matching_http_result() {
        for (valid,over_budget) in [(true,false),(false,false),(true,true)] {
            let db=crate::storage::StorageManager::open_in_memory().unwrap();
            db.with_conn(|c| {c.execute_batch("INSERT INTO bake_documents(id,title,doc_type,full_content,created_at,updated_at)
                VALUES(1,'source','document','fact1\nfact2\nfact3\nfact4\nfact5\nfact6\nfact7',1,1);
                INSERT INTO bake_document_source_snapshots(id,document_id,source_url,page_title,content_text,content_hash,completeness_status,identity_match,collected_at)
                VALUES(61,1,'https://example.com/doc','source','fact1\nfact2\nfact3\nfact4\nfact5\nfact6\nfact7','hash','complete',1,1);
                INSERT INTO bake_document_source_heads VALUES(1,61,1);")?;Ok(())}).unwrap();
            let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address=listener.local_addr().unwrap();
            let app=axum::Router::new().route("/bake/document_summary",axum::routing::post(
                move |Json(input):Json<serde_json::Value>| async move {
                    assert_eq!(input["content_text"],"fact1\nfact2\nfact3\nfact4\nfact5\nfact6\nfact7");
                    assert!(input.get("summary").is_none());
                    assert!(input.get("lease_id").is_none());
                    let quotes=if over_budget {vec!["fact1";9]} else {vec!["fact1","fact2","fact3","fact4","fact5","fact6","fact7"]};
                    Json(serde_json::json!({"document_id":input["document_id"],
                        "source_snapshot_id":if valid {61}else{62},"expected_updated_at":input["expected_updated_at"],
                        "summary":"verified summary","evidence_quotes":quotes,
                        "generation_version":"document-summary.v1"}))
                }));
            let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap();});
            let state=AppState::with_service_urls(db,format!("http://{address}"),String::new(),vec![]);
            let client=reqwest::Client::builder().no_proxy().build().unwrap();
            process_document_summary_job(state.clone(),&client).await.unwrap();
            state.storage.with_conn(|c| {
                let summary:Option<String>=c.query_row("SELECT summary FROM bake_documents WHERE id=1",[],|r|r.get(0))?;
                assert_eq!(summary.as_deref(),if valid && !over_budget {Some("verified summary")} else {None});
                let status:String=c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get(0))?;
                assert_eq!(status,if valid && !over_budget {"completed"}else{"pending"});
                Ok(())
            }).unwrap();
            server.abort();let _=server.await;
        }
    }

    #[tokio::test]
    async fn source_health_bounds_query_window_and_defaults_to_one_day() {
        let db=crate::storage::StorageManager::open_in_memory().unwrap();
        let state=AppState::with_service_urls(db,String::new(),String::new(),vec![]);
        for since in [-1,current_ts_ms()-32*86_400_000,current_ts_ms()+86_400_000] {
            assert!(get_document_source_health(State(state.clone()),Query(DocumentSourceHealthQuery{since_ms:Some(since)})).await.is_err());
        }
        let Json(report)=get_document_source_health(State(state),Query(DocumentSourceHealthQuery{since_ms:None})).await.unwrap();
        assert_eq!(report["checks"]["total"],0);
        assert_eq!(report["as_of_ms"].as_i64().unwrap()-report["since_ms"].as_i64().unwrap(),86_400_000);
    }

    #[tokio::test]
    async fn failed_document_check_records_execution_without_changing_body() {
        let db = crate::storage::StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| { c.execute("INSERT INTO bake_documents(id,title,doc_type,full_content,created_at,updated_at)
            VALUES(90002,'test','document','preserved body',1,1)",[])?; Ok(()) }).unwrap();
        let state = AppState::with_service_urls(db, String::new(), String::new(), vec![]);
        record_refresh_failure(&state,90002,"SCRAPE_TIMEOUT",1234,std::time::Duration::from_millis(1500)).await.unwrap();
        let check = state.storage.latest_document_source_check(90002).unwrap().unwrap();
        assert_eq!(check["evidence"]["execution_ms"],1500);
        for field in ["body_character_count","substantive_block_count","excluded_block_count",
            "redaction_fraction","completeness_evidence"] {
            assert!(check["evidence"].get(field).unwrap().is_null());
        }
        assert_eq!(check["evidence"]["started_at_ms"],1234);
        assert!(check["evidence"]["finished_at_ms"].as_i64().unwrap()>1234);
        assert_eq!(check["evidence"]["reason"],"SCRAPE_TIMEOUT");
        assert!(check["snapshot_id"].is_null());
        record_refresh_failure(&state,90002,"SOURCE_WRITES_PAUSED",3000,std::time::Duration::from_millis(1800)).await.unwrap();
        let paused = state.storage.latest_document_source_check(90002).unwrap().unwrap();
        assert_eq!(paused["evidence"]["reason"],"SOURCE_WRITES_PAUSED");
        assert_eq!(paused["evidence"]["execution_ms"],1800);
        assert_eq!(paused["evidence"]["quality_version"],crate::services::document_refresh::DOCUMENT_QUALITY_RULE_VERSION);
        record_refresh_failure_measured(&state,90002,"SCRAPE_EMPTY",4000,
            std::time::Duration::from_millis(20),Some((100,15,90))).await.unwrap();
        let rejected=state.storage.latest_document_source_check(90002).unwrap().unwrap();
        assert_eq!(rejected["evidence"]["source_character_count"],100);
        assert_eq!(rejected["evidence"]["body_character_count"],15);
        assert_eq!(rejected["evidence"]["redaction_fraction"],0.9);
        assert_eq!(rejected["evidence"]["redacted_characters"],90);
        assert!(rejected["snapshot_id"].is_null());
        assert!(rejected["evidence"]["completeness_evidence"].is_null());
        state.storage.with_conn(|c| {
            assert_eq!(c.query_row("SELECT full_content FROM bake_documents WHERE id=90002",[],|r|r.get::<_,String>(0))?,"preserved body");
            Ok(())
        }).unwrap();
    }

    #[tokio::test]
    async fn document_refresh_stop_switch_preserves_queued_work_and_blocks_manual_dispatch() {
        let db = crate::storage::StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| { c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(90001,'test','document',1,1)",[])?; Ok(()) }).unwrap();
        db.enqueue_document_refresh_observation(90001, "new-body", None, 1).unwrap();
        db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY, r#"{"enabled":false}"#, "user", 1.0).unwrap();
        let state = AppState::with_service_urls(db, String::new(), String::new(), vec![]);
        let Json(result) = refresh_document_source(state.clone(), 90001,
            serde_json::from_value(serde_json::json!({"manual":true})).unwrap(), false, None).await.unwrap();
        assert_eq!(result.reason.as_deref(), Some("SOURCE_REFRESH_PAUSED"));
        assert_eq!(state.storage.document_refresh_observation_state(90001,"new-body").unwrap().as_deref(),Some("pending"));
        assert_eq!(state.browser_extension.status().active_job_count, 0);
        state.storage.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY, r#"{"source_writes_enabled":false}"#, "user", 1.0).unwrap();
        let Json(result) = refresh_document_source(state.clone(), 90001,
            serde_json::from_value(serde_json::json!({"manual":true})).unwrap(), false, None).await.unwrap();
        assert_eq!(result.reason.as_deref(), Some("SOURCE_REFRESH_PAUSED"));
        assert_eq!(state.storage.document_refresh_observation_state(90001,"new-body").unwrap().as_deref(),Some("pending"));
        assert_eq!(state.browser_extension.status().active_job_count, 0);
        state.storage.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY, r#"{"enabled":true}"#, "user", 1.0).unwrap();
        let config=load_document_refresh_config(&state).unwrap();
        assert!(config.enabled);
        assert!(!config.automatic_browser_reads_enabled);
        assert_eq!(state.browser_extension.status().active_job_count,0);
    }

    #[test]
    fn observed_updates_respect_policy_identity_and_cooldown() {
        for reason in [DocumentRefreshSkipReason::PolicyNever,DocumentRefreshSkipReason::UrlMissing,
            DocumentRefreshSkipReason::UrlInvalid,DocumentRefreshSkipReason::PageGone] {
            assert!(!should_override_observation_refresh(true,60_000,reason));
        }
        for reason in [DocumentRefreshSkipReason::CheckThrottled,DocumentRefreshSkipReason::ContentFresh,
            DocumentRefreshSkipReason::NoUpdateEvidence] {
            assert!(!should_override_observation_refresh(false,60_000,reason));
            assert!(!should_override_observation_refresh(true,29_999,reason));
            assert!(should_override_observation_refresh(true,30_000,reason));
        }
    }

    #[test]
    fn document_refresh_lease_deduplicates_and_releases_on_cancel() {
        let first = DocumentRefreshLease::acquire(-9001).unwrap();
        assert!(DocumentRefreshLease::acquire(-9001).is_none());
        drop(first);
        assert!(DocumentRefreshLease::acquire(-9001).is_some());
    }

    #[test]
    fn document_refresh_cancel_and_commit_are_mutually_exclusive() {
        let cancelled=DocumentRefreshLease::acquire(-9010).unwrap();
        assert!(cancelled.1.cancel());
        assert!(cancelled.1.cancel());
        assert!(!cancelled.1.begin_commit());
        drop(cancelled);
        let committing=DocumentRefreshLease::acquire(-9010).unwrap();
        assert!(committing.1.begin_commit());
        assert!(!committing.1.cancel());
        assert!(!committing.1.begin_commit());
    }

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
    fn document_identity_ignores_declared_editor_view_parameters() {
        assert!(document_refresh_identity_matches(
            "https://Docs.Example.com/d/home/ABC123?section=one#comment",
            "https://docs.example.com/d/home/ABC123?section=two",
        ));
        assert!(!document_refresh_identity_matches(
            "https://docs.example.com/d/home/abc123",
            "https://docs.example.com/d/home/other",
        ));
    }

    #[test]
    fn static_dom_capture_without_coverage_evidence_is_partial() {
        let assessment = assess_document_refresh_completeness(
            &serde_json::json!({"page_state": {"readiness_timed_out": false}}),
            "完整正文",
        );

        assert_eq!(assessment.completeness_status, "partial");
        assert!(!assessment.reached_end);
        assert!(assessment.truncated);
        assert_eq!(assessment.stable_passes, 0);
    }

    #[test]
    fn extension_completeness_is_consumed_without_inventing_evidence() {
        let evidence = serde_json::json!({"completeness": {
            "status": "complete", "reached_end": true, "stable_passes": 2,
            "segment_count": 3, "truncated": false
        }});
        let complete = assess_document_refresh_completeness(&evidence, "完整正文");
        assert_eq!(complete.completeness_status, "complete");
        assert_eq!(complete.segment_count, 3);
        let mut partial = evidence.clone();
        partial["completeness"]["truncated"] = serde_json::json!(true);
        assert_eq!(assess_document_refresh_completeness(&partial, "部分正文").completeness_status, "partial");
        partial = evidence.clone();
        partial["completeness"]["stable_passes"] = serde_json::json!(0);
        assert_eq!(assess_document_refresh_completeness(&partial, "未稳定正文").completeness_status, "partial");
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
