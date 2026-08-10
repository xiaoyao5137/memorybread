use std::{
    fs,
    io::BufWriter,
    path::PathBuf,
    process::{Command, Output, Stdio},
    sync::Arc,
    thread,
    time::Duration,
};

use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::{
    api::{error::ApiError, state::AppState},
    storage::{
        repo::creation_evidence::NewCreationEvidenceAsset, CreationEvidenceAssetView,
        DataExtractionSummary, DataSearchResult, DataSourceRecord, DiscoveredSourceOutcome,
        StorageError,
    },
};
use uuid::Uuid;

const MAX_SCRAPED_CHARS: usize = 80_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BrowserScriptKind {
    Chromium,
    Safari,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct BrowserAdapter {
    id: &'static str,
    app_name: &'static str,
    process_name: &'static str,
    script_kind: BrowserScriptKind,
}

const BROWSER_ADAPTERS: &[BrowserAdapter] = &[
    BrowserAdapter {
        id: "chrome",
        app_name: "Google Chrome",
        process_name: "Google Chrome",
        script_kind: BrowserScriptKind::Chromium,
    },
    BrowserAdapter {
        id: "chrome_canary",
        app_name: "Google Chrome Canary",
        process_name: "Google Chrome Canary",
        script_kind: BrowserScriptKind::Chromium,
    },
    BrowserAdapter {
        id: "edge",
        app_name: "Microsoft Edge",
        process_name: "Microsoft Edge",
        script_kind: BrowserScriptKind::Chromium,
    },
    BrowserAdapter {
        id: "brave",
        app_name: "Brave Browser",
        process_name: "Brave Browser",
        script_kind: BrowserScriptKind::Chromium,
    },
    BrowserAdapter {
        id: "chromium",
        app_name: "Chromium",
        process_name: "Chromium",
        script_kind: BrowserScriptKind::Chromium,
    },
    BrowserAdapter {
        id: "vivaldi",
        app_name: "Vivaldi",
        process_name: "Vivaldi",
        script_kind: BrowserScriptKind::Chromium,
    },
    BrowserAdapter {
        id: "safari",
        app_name: "Safari",
        process_name: "Safari",
        script_kind: BrowserScriptKind::Safari,
    },
];

#[derive(Debug, Deserialize)]
pub struct DataListQuery {
    pub q: Option<String>,
    pub limit: Option<usize>,
    pub offset: Option<usize>,
}

#[derive(Debug, Serialize)]
pub struct DataListResponse {
    pub items: Vec<DataSourceRecord>,
    pub total: i64,
    pub pending_items: Vec<DataSourceRecord>,
    pub pending_total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(Debug, Deserialize)]
pub struct ExtractDataRequest {
    pub limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
pub struct RegisterDiscoveredSourceRequest {
    pub url: String,
    pub title: Option<String>,
    pub capture_id: i64,
    pub timeline_id: Option<i64>,
    pub observed_at: Option<i64>,
    pub page_kind: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct RegisterDiscoveredSourceResponse {
    pub status: &'static str,
    pub reason_code: Option<&'static str>,
    pub source_id: Option<i64>,
    pub created: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub struct DataSearchRequest {
    pub query: String,
    #[serde(default)]
    pub need_fresh: bool,
    pub as_of_ms: Option<i64>,
    pub limit: Option<usize>,
}

#[derive(Debug, Serialize)]
pub struct DataSearchResponse {
    pub schema_version: &'static str,
    pub query: String,
    pub results: Vec<DataSearchResult>,
}

#[derive(Debug, Deserialize)]
pub struct RefreshDataSourceRequest {
    #[serde(default = "default_scrape_mode")]
    pub mode: String,
    #[serde(default = "default_browser_preference")]
    pub browser_preference: String,
    #[serde(default)]
    pub capture_evidence: bool,
    /// 是否额外保留网页截图；缺省开启。关闭时仍使用专用浏览器窗口和 AX/DOM。
    #[serde(default = "default_true")]
    pub retain_screenshot: bool,
    #[serde(default)]
    pub run_id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub preview_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WebpageScrapeResponse {
    pub schema_version: &'static str,
    pub source_id: i64,
    pub collector: String,
    pub browser: Option<String>,
    pub interaction_mode: String,
    pub collected_at: i64,
    pub title: String,
    pub url: String,
    pub content_text: String,
    pub structured_data: Value,
    pub content_hash: String,
    pub evidence: Option<CreationEvidenceAssetView>,
}

#[derive(Debug, Deserialize)]
pub struct ValidateEvidenceRequest {
    pub status: String,
    #[serde(default)]
    pub validation: Value,
}

#[derive(Debug)]
struct PendingEvidenceCapture {
    id: String,
    run_id: String,
    session_id: String,
    full_path: PathBuf,
}

#[derive(Debug)]
struct BrowserWindowSession {
    apple_script_id: String,
    preview_token: String,
    launched_browser: bool,
}

#[derive(Debug)]
struct BrowserScreenshot {
    relative_path: String,
    width: i64,
    height: i64,
    content_hash: String,
}

#[derive(Debug)]
struct ScrapeResult {
    collector: &'static str,
    browser: Option<&'static str>,
    interaction_mode: &'static str,
    title: String,
    url: String,
    content_text: String,
    structured_data: Value,
    screenshot: Option<BrowserScreenshot>,
}

#[derive(Debug)]
pub struct DataToolError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
}

impl DataToolError {
    fn new(status: StatusCode, code: &'static str, message: &'static str) -> Self {
        Self {
            status,
            code,
            message,
        }
    }
}

impl IntoResponse for DataToolError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({"error": self.code, "message": self.message})),
        )
            .into_response()
    }
}

pub async fn list_data_sources(
    State(state): State<Arc<AppState>>,
    Query(params): Query<DataListQuery>,
) -> Result<Json<DataListResponse>, ApiError> {
    let limit = params.limit.unwrap_or(20).clamp(1, 100);
    let offset = params.offset.unwrap_or(0);
    let query = params.q.filter(|value| !value.trim().is_empty());
    let storage = state.storage.clone();
    let (items, total, pending_items, pending_total) = tokio::task::spawn_blocking(move || {
        let (items, total) = storage.list_data_sources(query.as_deref(), limit, offset)?;
        let (pending_items, pending_total) =
            storage.list_pending_data_sources(query.as_deref(), 100)?;
        Ok::<_, StorageError>((items, total, pending_items, pending_total))
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;
    Ok(Json(DataListResponse {
        items,
        total,
        pending_items,
        pending_total,
        limit,
        offset,
    }))
}

pub async fn get_data_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<DataSourceRecord>, ApiError> {
    let storage = state.storage.clone();
    let source = tokio::task::spawn_blocking(move || storage.get_data_source(id))
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))??
        .ok_or_else(|| ApiError::NotFound("数据源不存在".to_string()))?;
    Ok(Json(source))
}

pub async fn delete_data_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let storage = state.storage.clone();
    let deleted = tokio::task::spawn_blocking(move || storage.delete_data_source(id))
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))??;
    if !deleted {
        return Err(ApiError::NotFound("数据不存在或已删除".to_string()));
    }
    Ok(StatusCode::NO_CONTENT)
}

pub async fn extract_data_sources(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ExtractDataRequest>,
) -> Result<Json<DataExtractionSummary>, ApiError> {
    let storage = state.storage.clone();
    let limit = body.limit.unwrap_or(1000).clamp(1, 5000);
    let summary = tokio::task::spawn_blocking(move || storage.extract_data_candidates(limit))
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))??;
    Ok(Json(summary))
}

/// 注册时间线推理同车输出的数据页面分类（data_report/data_platform）。
///
/// sidecar 已做过一轮代码校验（URL 必须真实存在于本组 capture），这里再做
/// 服务端兜底：类型白名单、capture 存在性与敏感过滤在存储层复核。
pub async fn register_discovered_source(
    State(state): State<Arc<AppState>>,
    Json(body): Json<RegisterDiscoveredSourceRequest>,
) -> Result<Json<RegisterDiscoveredSourceResponse>, ApiError> {
    let url = body.url.trim().to_string();
    if url.is_empty() {
        return Err(ApiError::BadRequest("数据源 URL 不能为空".to_string()));
    }
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return Err(ApiError::BadRequest("仅支持 http/https 数据源".to_string()));
    }
    if let Some(kind) = body.page_kind.as_deref() {
        if !matches!(kind, "data_report" | "data_platform") {
            return Err(ApiError::BadRequest(
                "仅接受 data_report/data_platform 类型的数据页面".to_string(),
            ));
        }
    }
    if body.capture_id <= 0 {
        return Err(ApiError::BadRequest("capture_id 无效".to_string()));
    }
    let storage = state.storage.clone();
    let title = body.title.unwrap_or_default();
    let capture_id = body.capture_id;
    let timeline_id = body.timeline_id;
    let observed_at = body.observed_at.unwrap_or(0);
    let outcome = tokio::task::spawn_blocking(move || {
        storage.register_discovered_report_source(
            &url,
            &title,
            capture_id,
            timeline_id,
            observed_at,
        )
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;
    let response = match outcome {
        DiscoveredSourceOutcome::Registered { source_id, created } => {
            RegisterDiscoveredSourceResponse {
                status: "registered",
                reason_code: None,
                source_id: Some(source_id),
                created: Some(created),
            }
        }
        DiscoveredSourceOutcome::RejectedInvalidUrl => RegisterDiscoveredSourceResponse {
            status: "rejected",
            reason_code: Some("invalid_url"),
            source_id: None,
            created: None,
        },
        DiscoveredSourceOutcome::RejectedCaptureMissing => RegisterDiscoveredSourceResponse {
            status: "rejected",
            reason_code: Some("capture_missing"),
            source_id: None,
            created: None,
        },
        DiscoveredSourceOutcome::RejectedCaptureSensitive => RegisterDiscoveredSourceResponse {
            status: "rejected",
            reason_code: Some("capture_sensitive"),
            source_id: None,
            created: None,
        },
    };
    Ok(Json(response))
}

pub async fn search_data(
    State(state): State<Arc<AppState>>,
    Json(body): Json<DataSearchRequest>,
) -> Result<Json<DataSearchResponse>, ApiError> {
    let query = body.query.trim().to_string();
    if query.is_empty() {
        return Err(ApiError::BadRequest("数据检索词不能为空".to_string()));
    }
    let storage = state.storage.clone();
    let need_fresh = body.need_fresh;
    let as_of_ms = body.as_of_ms.unwrap_or_else(now_ms);
    let limit = body.limit.unwrap_or(30).clamp(1, 50);
    let search_query = query.clone();
    let results = tokio::task::spawn_blocking(move || {
        storage.search_data_sources(&search_query, need_fresh, as_of_ms, limit)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;
    Ok(Json(DataSearchResponse {
        schema_version: "memorybread.data-search.v1",
        query,
        results,
    }))
}

pub async fn refresh_data_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(body): Json<RefreshDataSourceRequest>,
) -> Result<Json<WebpageScrapeResponse>, DataToolError> {
    let mode = body.mode.trim().to_lowercase();
    if !matches!(mode.as_str(), "auto" | "browser" | "http") {
        return Err(DataToolError::new(
            StatusCode::BAD_REQUEST,
            "BAD_REQUEST",
            "网页采集模式无效",
        ));
    }
    let browser_preference = body.browser_preference.trim().to_lowercase();
    if browser_preference != "auto"
        && !BROWSER_ADAPTERS
            .iter()
            .any(|adapter| adapter.id == browser_preference)
    {
        return Err(DataToolError::new(
            StatusCode::BAD_REQUEST,
            "BAD_REQUEST",
            "浏览器偏好无效或当前不受支持",
        ));
    }
    let evidence_capture = if body.capture_evidence {
        Some(prepare_evidence_capture(
            body.run_id.as_deref(),
            body.session_id.as_deref(),
            body.preview_id.as_deref(),
        )?)
    } else {
        None
    };
    let storage = state.storage.clone();
    let source = tokio::task::spawn_blocking(move || storage.get_data_source(id))
        .await
        .map_err(|_| internal_scrape_error())?
        .map_err(|_| internal_scrape_error())?
        .ok_or_else(|| {
            DataToolError::new(
                StatusCode::NOT_FOUND,
                "DATA_SOURCE_NOT_FOUND",
                "数据源不存在或已停用",
            )
        })?;
    let url = source.source_url.clone().ok_or_else(|| {
        DataToolError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "DATA_SOURCE_URL_MISSING",
            "该数据源没有可刷新的网页地址",
        )
    })?;
    validate_scrape_url(&url)?;

    let preview_token = evidence_capture.as_ref().map(|capture| capture.id.clone());
    let evidence_path = evidence_capture
        .as_ref()
        .filter(|_| body.retain_screenshot)
        .map(|capture| capture.full_path.clone());
    let scrape_in_browser = || {
        scrape_browser_async(
            url.clone(),
            browser_preference.clone(),
            source.source_app_name.clone(),
            preview_token.clone(),
            evidence_path.clone(),
        )
    };

    let scrape_result = match mode.as_str() {
        _ if evidence_capture.is_some() => scrape_in_browser().await,
        "browser" => scrape_in_browser().await,
        "http" => scrape_http(&url).await,
        _ if source.access_mode == "browser_session" => match scrape_in_browser().await {
            Ok(result) => Ok(result),
            Err(browser_error) => match scrape_http(&url).await {
                Ok(result) => Ok(result),
                Err(_) => Err(browser_error),
            },
        },
        _ => match scrape_http(&url).await {
            Ok(result) => Ok(result),
            Err(_) => scrape_in_browser().await,
        },
    };

    let result = match scrape_result {
        Ok(result) => result,
        Err(error) => {
            cleanup_pending_evidence(evidence_capture.as_ref());
            let storage = state.storage.clone();
            let error_code = error.code.to_string();
            let _ = tokio::task::spawn_blocking(move || {
                storage.mark_data_source_error(id, &error_code)
            })
            .await;
            return Err(error);
        }
    };
    if result.content_text.trim().is_empty() {
        cleanup_pending_evidence(evidence_capture.as_ref());
        return Err(DataToolError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "SCRAPE_EMPTY",
            "页面没有可采纳的数据正文或表格",
        ));
    }

    let collected_at = now_ms();
    let content_hash = format!(
        "{:x}",
        Sha256::digest(format!("{}\n{}", result.content_text, result.structured_data).as_bytes())
    );
    let storage = state.storage.clone();
    let collector = result.collector.to_string();
    let title = result.title.clone();
    let content = result.content_text.clone();
    let structured = result.structured_data.clone();
    let snapshot_result = tokio::task::spawn_blocking(move || {
        storage.save_data_snapshot(
            id,
            &collector,
            Some(&title),
            &content,
            &structured,
            collected_at,
        )
    })
    .await;
    let snapshot = match snapshot_result {
        Ok(Ok(snapshot)) => snapshot,
        _ => {
            cleanup_pending_evidence(evidence_capture.as_ref());
            return Err(internal_scrape_error());
        }
    };

    let evidence = if let (Some(capture), Some(screenshot)) =
        (evidence_capture.as_ref(), result.screenshot.as_ref())
    {
        let storage = state.storage.clone();
        let asset = NewCreationEvidenceAsset {
            id: capture.id.clone(),
            run_id: capture.run_id.clone(),
            session_id: capture.session_id.clone(),
            source_id: id,
            // 跨阶段快照会保留，截图证据直接绑定当次采集所属快照；
            // 同一自然周内刷新时仍更新同一条阶段快照。
            data_snapshot_id: Some(snapshot.id),
            source_url: result.url.clone(),
            page_title: result.title.clone(),
            captured_at: collected_at,
            image_path: screenshot.relative_path.clone(),
            mime_type: "image/jpeg".to_string(),
            width: screenshot.width,
            height: screenshot.height,
            content_hash: screenshot.content_hash.clone(),
            screenshot_source: "browser_window_segments".to_string(),
        };
        match tokio::task::spawn_blocking(move || storage.save_creation_evidence_asset(&asset))
            .await
        {
            Ok(Ok(asset)) => Some(asset.into()),
            _ => {
                cleanup_pending_evidence(evidence_capture.as_ref());
                return Err(internal_scrape_error());
            }
        }
    } else {
        None
    };

    Ok(Json(WebpageScrapeResponse {
        schema_version: "memorybread.webpage-scrape.v1",
        source_id: id,
        collector: result.collector.to_string(),
        browser: result.browser.map(ToString::to_string),
        interaction_mode: result.interaction_mode.to_string(),
        collected_at,
        title: result.title,
        url: result.url,
        content_text: result.content_text,
        structured_data: result.structured_data,
        content_hash,
        evidence,
    }))
}

pub async fn get_creation_evidence_image(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let storage = state.storage.clone();
    let asset = tokio::task::spawn_blocking(move || storage.get_creation_evidence_asset(&id))
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))??
        .ok_or_else(|| ApiError::NotFound("创作证据不存在".to_string()))?;
    let path = evidence_dir().join(&asset.image_path);
    let bytes = tokio::fs::read(path)
        .await
        .map_err(|_| ApiError::NotFound("创作证据图片不存在".to_string()))?;
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, asset.mime_type)
        .header(
            header::CACHE_CONTROL,
            "private, max-age=31536000, immutable",
        )
        .body(Body::from(bytes))
        .map_err(|error| ApiError::Internal(error.to_string()))
}

pub async fn get_browser_preview_image(Path(id): Path<String>) -> Result<Response, ApiError> {
    let id = Uuid::parse_str(id.trim())
        .map_err(|_| ApiError::BadRequest("浏览器预览标识无效".to_string()))?
        .to_string();
    let bytes = tokio::fs::read(evidence_dir().join(format!("{id}.jpg")))
        .await
        .map_err(|_| ApiError::NotFound("浏览器预览尚未生成".to_string()))?;
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "image/jpeg")
        .header(header::CACHE_CONTROL, "private, no-store, max-age=0")
        .body(Body::from(bytes))
        .map_err(|error| ApiError::Internal(error.to_string()))
}

pub async fn validate_creation_evidence(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
    Json(body): Json<ValidateEvidenceRequest>,
) -> Result<Json<CreationEvidenceAssetView>, ApiError> {
    let status = body.status.trim().to_lowercase();
    if !matches!(status.as_str(), "verified" | "rejected") {
        return Err(ApiError::BadRequest(
            "证据校验状态只能是 verified 或 rejected".to_string(),
        ));
    }
    let storage = state.storage.clone();
    let validation = body.validation;
    let asset = tokio::task::spawn_blocking(move || {
        storage.validate_creation_evidence_asset(&id, &status, &validation)
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??
    .ok_or_else(|| ApiError::NotFound("创作证据不存在".to_string()))?;
    Ok(Json(asset.into()))
}

async fn scrape_browser_async(
    url: String,
    browser_preference: String,
    source_app_name: Option<String>,
    preview_token: Option<String>,
    evidence_path: Option<PathBuf>,
) -> Result<ScrapeResult, DataToolError> {
    tokio::task::spawn_blocking(move || {
        scrape_with_browser(
            &url,
            Some(browser_preference.as_str()),
            source_app_name.as_deref(),
            preview_token.as_deref(),
            evidence_path.as_deref(),
        )
    })
    .await
    .map_err(|_| internal_scrape_error())?
}

fn scrape_with_browser(
    url: &str,
    browser_preference: Option<&str>,
    source_app_name: Option<&str>,
    preview_token: Option<&str>,
    evidence_path: Option<&std::path::Path>,
) -> Result<ScrapeResult, DataToolError> {
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (
            url,
            browser_preference,
            source_app_name,
            preview_token,
            evidence_path,
        );
        return Err(DataToolError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "BROWSER_ATTACH_UNAVAILABLE",
            "当前系统暂不支持附加本机浏览器会话",
        ));
    }

    #[cfg(target_os = "macos")]
    {
        let (adapter, launched_browser) =
            resolve_browser_adapter(browser_candidates(browser_preference, source_app_name))?;
        let javascript = browser_extraction_javascript();
        let readiness_javascript = browser_readiness_javascript();
        let evidence_path_string = evidence_path.map(|path| path.to_string_lossy().into_owned());
        let (output, accessibility_text) = if let Some(preview_token) = preview_token {
            let session =
                start_background_browser_window(adapter, url, preview_token, launched_browser)?;
            let capture_result = (|| {
                // 页面刚进入专用窗口后先生成一次运行中预览；最终 DOM 稳定后会原子覆盖。
                if let Some(evidence_path) = evidence_path {
                    thread::sleep(Duration::from_millis(350));
                    let _ =
                        capture_background_browser_window(adapter, &session, evidence_path, false);
                }
                let script = build_background_browser_extract_script(
                    adapter,
                    &session.apple_script_id,
                    &readiness_javascript,
                    &javascript,
                );
                let output = run_browser_script(&script)?;
                let accessibility_text = if output.status.success() {
                    let payload: Value = serde_json::from_slice(&output.stdout).unwrap_or_default();
                    let page_title = payload
                        .get("title")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    crate::capture::ax::extract_window_text_by_title(
                        adapter.process_name,
                        &session.preview_token,
                    )
                    .or_else(|| {
                        crate::capture::ax::extract_window_text_by_title(
                            adapter.process_name,
                            page_title,
                        )
                    })
                } else {
                    None
                };
                if output.status.success() {
                    if let Some(evidence_path) = evidence_path {
                        prepare_background_browser_window_for_capture(adapter, &session)?;
                        thread::sleep(Duration::from_millis(500));
                        capture_background_browser_long_screenshot(
                            adapter,
                            &session,
                            evidence_path,
                        )?;
                    }
                }
                Ok::<_, DataToolError>((output, accessibility_text))
            })();
            cleanup_background_browser_window(adapter, &session);
            capture_result?
        } else {
            let script = match adapter.script_kind {
                BrowserScriptKind::Chromium => {
                    build_chromium_scrape_script(adapter, url, &readiness_javascript, &javascript)
                }
                BrowserScriptKind::Safari => {
                    build_safari_scrape_script(adapter, url, &readiness_javascript, &javascript)
                }
            };
            (run_browser_script(&script)?, None)
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).to_lowercase();
            if browser_scripting_is_disabled(&stderr) {
                return Err(DataToolError::new(
                    StatusCode::PRECONDITION_FAILED,
                    "BROWSER_SCRIPTING_DISABLED",
                    "请允许浏览器执行来自 Apple Events 的 JavaScript",
                ));
            }
            return Err(DataToolError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "BROWSER_ATTACH_UNAVAILABLE",
                "浏览器登录会话暂时不可用",
            ));
        }
        let payload: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
            DataToolError::new(
                StatusCode::BAD_GATEWAY,
                "SCRAPE_FAILED",
                "浏览器返回的页面数据无法解析",
            )
        })?;
        let title = payload
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("数据报表");
        let final_url = payload.get("url").and_then(Value::as_str).unwrap_or(url);
        let dom_content_text = payload.get("text").and_then(Value::as_str).unwrap_or("");
        if looks_like_auth_page(title, final_url, dom_content_text) {
            return Err(DataToolError::new(
                StatusCode::UNAUTHORIZED,
                "SCRAPE_AUTH_REQUIRED",
                "请先在对应浏览器中完成页面登录",
            ));
        }
        let screenshot =
            evidence_path
                .map(read_browser_screenshot)
                .transpose()?
                .map(|mut screenshot| {
                    screenshot.relative_path = evidence_path_string
                        .as_deref()
                        .and_then(|value| std::path::Path::new(value).file_name())
                        .and_then(|value| value.to_str())
                        .unwrap_or_default()
                        .to_string();
                    screenshot
                });
        let ax_content_text = accessibility_text
            .as_deref()
            .map(|value| clip_text(value, MAX_SCRAPED_CHARS))
            .filter(|value| accessibility_text_covers_dom(value, dom_content_text));
        let content_text = ax_content_text.as_deref().unwrap_or(dom_content_text);
        let mut structured_data = payload
            .get("structured_data")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !structured_data.is_object() {
            structured_data = json!({"payload": structured_data});
        }
        if let Some(object) = structured_data.as_object_mut() {
            object.insert(
                "dom_content_text".to_string(),
                Value::String(clip_text(dom_content_text, MAX_SCRAPED_CHARS)),
            );
            object.insert(
                "extraction".to_string(),
                json!({
                    "primary": if ax_content_text.is_some() { "accessibility" } else { "dom" },
                    "fallback": "dom",
                    "accessibility_char_count": ax_content_text
                        .as_ref()
                        .map(|value| value.chars().count())
                        .unwrap_or(0),
                    "dom_char_count": dom_content_text.chars().count(),
                }),
            );
        }
        Ok(ScrapeResult {
            collector: "browser_attach",
            browser: Some(adapter.id),
            interaction_mode: if preview_token.is_some() {
                "background_browser_window"
            } else {
                "background_tab"
            },
            title: clip_text(title, 240),
            url: redact_url_credentials(final_url).unwrap_or_else(|| url.to_string()),
            content_text: clip_text(content_text, MAX_SCRAPED_CHARS),
            structured_data,
            screenshot,
        })
    }
}

fn evidence_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".memory-bread")
        .join("creation-evidence")
}

fn cleanup_pending_evidence(capture: Option<&PendingEvidenceCapture>) {
    if let Some(capture) = capture {
        let _ = fs::remove_file(&capture.full_path);
    }
}

fn prepare_evidence_capture(
    run_id: Option<&str>,
    session_id: Option<&str>,
    preview_id: Option<&str>,
) -> Result<PendingEvidenceCapture, DataToolError> {
    let run_id = run_id.map(str::trim).filter(|value| !value.is_empty());
    let session_id = session_id.map(str::trim).filter(|value| !value.is_empty());
    let (Some(run_id), Some(session_id)) = (run_id, session_id) else {
        return Err(DataToolError::new(
            StatusCode::BAD_REQUEST,
            "EVIDENCE_CONTEXT_REQUIRED",
            "创作截图需要 run_id 与 session_id",
        ));
    };
    let id = normalize_preview_id(preview_id)?;
    let relative_path = format!("{id}.jpg");
    let directory = evidence_dir();
    fs::create_dir_all(&directory).map_err(|_| internal_scrape_error())?;
    Ok(PendingEvidenceCapture {
        id,
        run_id: clip_text(run_id, 128),
        session_id: clip_text(session_id, 128),
        full_path: directory.join(&relative_path),
    })
}

fn normalize_preview_id(preview_id: Option<&str>) -> Result<String, DataToolError> {
    match preview_id.map(str::trim).filter(|value| !value.is_empty()) {
        Some(value) => Uuid::parse_str(value)
            .map(|id| id.to_string())
            .map_err(|_| {
                DataToolError::new(StatusCode::BAD_REQUEST, "BAD_REQUEST", "浏览器预览标识无效")
            }),
        None => Ok(Uuid::new_v4().to_string()),
    }
}

fn read_browser_screenshot(path: &std::path::Path) -> Result<BrowserScreenshot, DataToolError> {
    let bytes = fs::read(path).map_err(|_| {
        DataToolError::new(
            StatusCode::BAD_GATEWAY,
            "SCREENSHOT_FAILED",
            "报表页面已读取，但通用浏览器截图失败",
        )
    })?;
    let (width, height) = image::image_dimensions(path).map_err(|_| {
        DataToolError::new(
            StatusCode::BAD_GATEWAY,
            "SCREENSHOT_FAILED",
            "浏览器截图文件无法解析",
        )
    })?;
    Ok(BrowserScreenshot {
        relative_path: String::new(),
        width: width.into(),
        height: height.into(),
        content_hash: format!("{:x}", Sha256::digest(&bytes)),
    })
}

fn browser_candidates(
    browser_preference: Option<&str>,
    source_app_name: Option<&str>,
) -> Vec<BrowserAdapter> {
    let preference = browser_preference.unwrap_or("auto").trim().to_lowercase();
    if preference != "auto" {
        return BROWSER_ADAPTERS
            .iter()
            .copied()
            .filter(|adapter| adapter.id == preference)
            .collect();
    }

    let hinted = browser_id_for_app_name(source_app_name.unwrap_or_default());
    let mut candidates = Vec::with_capacity(BROWSER_ADAPTERS.len());
    if let Some(hinted_id) = hinted {
        if let Some(adapter) = BROWSER_ADAPTERS
            .iter()
            .copied()
            .find(|adapter| adapter.id == hinted_id)
        {
            candidates.push(adapter);
        }
    }
    candidates.extend(
        BROWSER_ADAPTERS
            .iter()
            .copied()
            .filter(|adapter| Some(adapter.id) != hinted),
    );
    candidates
}

fn browser_id_for_app_name(app_name: &str) -> Option<&'static str> {
    let name = app_name.trim().to_lowercase();
    if name.contains("canary") {
        Some("chrome_canary")
    } else if name.contains("chrome") {
        Some("chrome")
    } else if name.contains("edge") {
        Some("edge")
    } else if name.contains("brave") {
        Some("brave")
    } else if name.contains("chromium") {
        Some("chromium")
    } else if name.contains("vivaldi") {
        Some("vivaldi")
    } else if name == "safari" {
        Some("safari")
    } else {
        None
    }
}

fn browser_is_running(adapter: BrowserAdapter) -> bool {
    Command::new("pgrep")
        .args(["-x", adapter.process_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(target_os = "macos")]
fn resolve_browser_adapter(
    candidates: Vec<BrowserAdapter>,
) -> Result<(BrowserAdapter, bool), DataToolError> {
    if let Some(adapter) = candidates
        .iter()
        .copied()
        .find(|adapter| browser_is_running(*adapter))
    {
        return Ok((adapter, false));
    }

    // Chromium 的 --no-startup-window 会复用默认本机配置，但不创建前台窗口。
    // Safari 没有等价的后台启动方式，因此只在已经运行时附加。
    for adapter in candidates
        .into_iter()
        .filter(|adapter| adapter.script_kind == BrowserScriptKind::Chromium)
    {
        let installed = Command::new("open")
            .args(["-Ra", adapter.app_name])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        if !installed {
            continue;
        }
        let launched = Command::new("open")
            .args([
                "-gj",
                "-a",
                adapter.app_name,
                "--args",
                "--no-startup-window",
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        if !launched {
            continue;
        }
        for _ in 0..40 {
            if browser_is_running(adapter) {
                return Ok((adapter, true));
            }
            thread::sleep(Duration::from_millis(100));
        }
    }

    Err(DataToolError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "BROWSER_ATTACH_UNAVAILABLE",
        "无法在后台启动或附加受支持的浏览器",
    ))
}

#[cfg(target_os = "macos")]
fn run_browser_script(script: &str) -> Result<Output, DataToolError> {
    Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
        .map_err(|_| {
            DataToolError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "BROWSER_ATTACH_UNAVAILABLE",
                "无法附加本机浏览器会话",
            )
        })
}

#[cfg(target_os = "macos")]
fn start_background_browser_window(
    adapter: BrowserAdapter,
    url: &str,
    preview_token: &str,
    launched_browser: bool,
) -> Result<BrowserWindowSession, DataToolError> {
    let preview_token = Uuid::parse_str(preview_token)
        .map_err(|_| internal_scrape_error())?
        .to_string();
    let output = match run_browser_script(&build_background_browser_start_script(
        adapter,
        url,
        &preview_token,
    )) {
        Ok(output) => output,
        Err(error) => {
            cleanup_orphaned_background_browser_window(adapter, &preview_token, launched_browser);
            return Err(error);
        }
    };
    if !output.status.success() {
        cleanup_orphaned_background_browser_window(adapter, &preview_token, launched_browser);
        return Err(DataToolError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "BROWSER_ATTACH_UNAVAILABLE",
            "无法创建后台浏览器预览窗口",
        ));
    }
    let apple_script_id = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if apple_script_id.is_empty()
        || !apple_script_id
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, '-' | '_'))
    {
        cleanup_orphaned_background_browser_window(adapter, &preview_token, launched_browser);
        return Err(internal_scrape_error());
    }
    Ok(BrowserWindowSession {
        apple_script_id,
        preview_token,
        launched_browser,
    })
}

#[cfg(target_os = "macos")]
fn cleanup_background_browser_window(adapter: BrowserAdapter, session: &BrowserWindowSession) {
    let _ = run_browser_script(&build_background_browser_cleanup_script(
        adapter,
        &session.apple_script_id,
        session.launched_browser,
    ));
}

#[cfg(target_os = "macos")]
fn cleanup_orphaned_background_browser_window(
    adapter: BrowserAdapter,
    preview_token: &str,
    launched_browser: bool,
) {
    let _ = run_browser_script(&build_background_browser_orphan_cleanup_script(
        adapter,
        preview_token,
        launched_browser,
    ));
}

#[cfg(target_os = "macos")]
fn capture_background_browser_window(
    adapter: BrowserAdapter,
    session: &BrowserWindowSession,
    path: &std::path::Path,
    require_rendered_content: bool,
) -> Result<(), DataToolError> {
    let image = capture_background_browser_image(adapter, session, require_rendered_content)?;
    write_browser_image_atomic(session, path, image)
}

#[cfg(target_os = "macos")]
fn capture_background_browser_image(
    adapter: BrowserAdapter,
    session: &BrowserWindowSession,
    require_rendered_content: bool,
) -> Result<image::RgbaImage, DataToolError> {
    use xcap::Window;

    let mut last_error = String::new();
    for _ in 0..12 {
        let windows = Window::all().map_err(|error| {
            DataToolError::new(
                StatusCode::BAD_GATEWAY,
                "SCREENSHOT_FAILED",
                if error.to_string().is_empty() {
                    "无法枚举后台浏览器窗口"
                } else {
                    "请检查系统录屏权限"
                },
            )
        })?;
        let mut title_matches = Vec::new();
        let mut bounds_matches = Vec::new();
        for window in windows {
            if window.app_name().ok().as_deref() != Some(adapter.app_name)
                || window.is_minimized().unwrap_or(false)
            {
                continue;
            }
            let title = window.title().unwrap_or_default();
            if title.contains(&session.preview_token) {
                title_matches.push(window);
                continue;
            }
            let geometry_matches = window
                .x()
                .ok()
                .zip(window.y().ok())
                .zip(window.width().ok())
                .zip(window.height().ok())
                .map(|(((x, y), width), height)| {
                    (x - 80).abs() <= 4
                        && (y - 80).abs() <= 40
                        && width.abs_diff(1200) <= 8
                        && height.abs_diff(740) <= 40
                })
                .unwrap_or(false);
            if geometry_matches {
                bounds_matches.push(window);
            }
        }
        let candidate = if title_matches.len() == 1 {
            title_matches.pop()
        } else if title_matches.is_empty() && bounds_matches.len() == 1 {
            bounds_matches.pop()
        } else {
            None
        };
        if let Some(window) = candidate {
            match window.capture_image() {
                Ok(image) => {
                    if require_rendered_content && !browser_screenshot_has_rendered_content(&image)
                    {
                        last_error = "后台浏览器只返回了空白页面".to_string();
                        thread::sleep(Duration::from_millis(150));
                        continue;
                    }
                    return Ok(image);
                }
                Err(error) => last_error = error.to_string(),
            }
        } else {
            last_error = "未找到唯一的 MemoryBread 后台浏览器窗口".to_string();
        }
        thread::sleep(Duration::from_millis(100));
    }
    tracing::warn!(browser = adapter.id, error = %last_error, "后台浏览器窗口截图失败");
    Err(DataToolError::new(
        StatusCode::BAD_GATEWAY,
        if last_error.contains("空白页面") {
            "SCREENSHOT_BLANK"
        } else {
            "SCREENSHOT_FAILED"
        },
        if last_error.contains("空白页面") {
            "后台页面已读取，但浏览器没有完成页面绘制"
        } else {
            "后台页面已读取，但缩略预览截图失败"
        },
    ))
}

#[cfg(target_os = "macos")]
fn write_browser_image_atomic(
    session: &BrowserWindowSession,
    path: &std::path::Path,
    image: image::RgbaImage,
) -> Result<(), DataToolError> {
    use image::{codecs::jpeg::JpegEncoder, DynamicImage};

    let parent = path.parent().ok_or_else(internal_scrape_error)?;
    fs::create_dir_all(parent).map_err(|_| internal_scrape_error())?;
    let temporary_path = parent.join(format!(
        ".{}.{}.tmp.jpg",
        session.preview_token,
        Uuid::new_v4()
    ));
    let file = fs::File::create(&temporary_path).map_err(|_| internal_scrape_error())?;
    let mut encoder = JpegEncoder::new_with_quality(BufWriter::new(file), 82);
    let encode_result = encoder.encode_image(&DynamicImage::ImageRgba8(image));
    drop(encoder);
    if encode_result.is_err() {
        let _ = fs::remove_file(&temporary_path);
        return Err(internal_scrape_error());
    }
    if fs::rename(&temporary_path, path).is_err() {
        let _ = fs::remove_file(&temporary_path);
        return Err(internal_scrape_error());
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct BrowserPageGeometry {
    outer_height: u32,
    inner_height: u32,
    scroll_height: u32,
}

fn browser_scroll_positions(geometry: BrowserPageGeometry) -> Vec<u32> {
    const MAX_SEGMENTS: usize = 20;
    let viewport = geometry.inner_height.max(1);
    let max_scroll = geometry.scroll_height.saturating_sub(viewport);
    if max_scroll == 0 {
        return vec![0];
    }
    let mut positions = Vec::new();
    let mut position = 0_u32;
    while positions.len() < MAX_SEGMENTS {
        positions.push(position);
        if position >= max_scroll {
            break;
        }
        position = position.saturating_add(viewport).min(max_scroll);
    }
    if positions.last().copied() != Some(max_scroll) {
        // 极长页面保留末屏作为边界证据；常规报表在 20 屏内会完整覆盖。
        if positions.len() == MAX_SEGMENTS {
            positions.pop();
        }
        positions.push(max_scroll);
    }
    positions
}

#[cfg(target_os = "macos")]
fn capture_background_browser_long_screenshot(
    adapter: BrowserAdapter,
    session: &BrowserWindowSession,
    path: &std::path::Path,
) -> Result<(), DataToolError> {
    use image::{imageops, Rgba, RgbaImage};

    let geometry_output = run_browser_script(&build_background_browser_evaluate_script(
        adapter,
        &session.apple_script_id,
        "JSON.stringify({outerHeight:Math.max(1,window.outerHeight||0),innerHeight:Math.max(1,window.innerHeight||0),scrollHeight:Math.max(document.documentElement?document.documentElement.scrollHeight:0,document.body?document.body.scrollHeight:0,window.innerHeight||0)})",
    ))?;
    if !geometry_output.status.success() {
        return Err(DataToolError::new(
            StatusCode::BAD_GATEWAY,
            "SCREENSHOT_FAILED",
            "无法读取网页长截图范围",
        ));
    }
    let geometry: BrowserPageGeometry =
        serde_json::from_slice(&geometry_output.stdout).map_err(|_| internal_scrape_error())?;
    let positions = browser_scroll_positions(geometry);
    let mut segments: Vec<(u32, RgbaImage)> = Vec::with_capacity(positions.len());
    for position in &positions {
        let scroll_script = format!(
            "window.scrollTo(0,{position});JSON.stringify({{scrollY:Math.round(window.scrollY||0)}})"
        );
        let output = run_browser_script(&build_background_browser_evaluate_script(
            adapter,
            &session.apple_script_id,
            &scroll_script,
        ))?;
        if !output.status.success() {
            return Err(DataToolError::new(
                StatusCode::BAD_GATEWAY,
                "SCREENSHOT_FAILED",
                "网页分段滚动失败",
            ));
        }
        thread::sleep(Duration::from_millis(220));
        segments.push((
            *position,
            capture_background_browser_image(adapter, session, true)?,
        ));
    }
    let _ = run_browser_script(&build_background_browser_evaluate_script(
        adapter,
        &session.apple_script_id,
        "window.scrollTo(0,0);JSON.stringify({scrollY:0})",
    ));

    let Some((_, first)) = segments.first() else {
        return Err(internal_scrape_error());
    };
    let target_width = first.width();
    let browser_chrome_css = geometry.outer_height.saturating_sub(geometry.inner_height);
    let mut prepared: Vec<RgbaImage> = Vec::with_capacity(segments.len());
    let mut previous_position = 0_u32;
    for (index, (position, image)) in segments.into_iter().enumerate() {
        if image.width() != target_width {
            return Err(internal_scrape_error());
        }
        if index == 0 {
            prepared.push(image);
            previous_position = position;
            continue;
        }
        let scale = image.height() as f64 / geometry.outer_height.max(1) as f64;
        let chrome_px = (browser_chrome_css as f64 * scale).round() as u32;
        let delta = position.saturating_sub(previous_position);
        let overlap_css = geometry.inner_height.saturating_sub(delta);
        let overlap_px = (overlap_css as f64 * scale).round() as u32;
        let crop_y = chrome_px.saturating_add(overlap_px).min(image.height());
        if crop_y < image.height() {
            prepared.push(
                imageops::crop_imm(&image, 0, crop_y, image.width(), image.height() - crop_y)
                    .to_image(),
            );
        }
        previous_position = position;
    }
    let total_height = prepared
        .iter()
        .fold(0_u32, |sum, image| sum.saturating_add(image.height()));
    if total_height == 0 {
        return Err(internal_scrape_error());
    }
    let mut stitched =
        RgbaImage::from_pixel(target_width, total_height, Rgba([255, 255, 255, 255]));
    let mut y = 0_i64;
    for segment in prepared {
        imageops::overlay(&mut stitched, &segment, 0, y);
        y += i64::from(segment.height());
    }
    write_browser_image_atomic(session, path, stitched)
}

#[cfg(target_os = "macos")]
fn prepare_background_browser_window_for_capture(
    adapter: BrowserAdapter,
    session: &BrowserWindowSession,
) -> Result<(), DataToolError> {
    let output = run_browser_script(&build_background_browser_prepare_capture_script(
        adapter,
        &session.apple_script_id,
    ))?;
    if output.status.success() {
        return Ok(());
    }
    Err(DataToolError::new(
        StatusCode::BAD_GATEWAY,
        "SCREENSHOT_FAILED",
        "后台页面已读取，但无法准备截图窗口",
    ))
}

fn browser_screenshot_has_rendered_content(image: &image::RgbaImage) -> bool {
    if image.width() < 64 || image.height() < 64 {
        return false;
    }
    let start_y = image.height() / 5;
    let mut sampled = 0_u64;
    let mut non_blank = 0_u64;
    for y in (start_y..image.height()).step_by(4) {
        for x in (0..image.width()).step_by(4) {
            let pixel = image.get_pixel(x, y).0;
            sampled += 1;
            if pixel[3] > 10 && (pixel[0] < 245 || pixel[1] < 245 || pixel[2] < 245) {
                non_blank += 1;
            }
        }
    }
    sampled > 0 && non_blank * 1_000 >= sampled * 3
}

fn accessibility_text_covers_dom(accessibility_text: &str, dom_text: &str) -> bool {
    let normalized_ax = accessibility_text
        .chars()
        .filter(|character| !character.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect::<String>();
    if normalized_ax.chars().count() < 12 {
        return false;
    }
    let mut matches = 0_usize;
    let mut numeric_match = false;
    for line in dom_text.lines().take(800) {
        let normalized = line
            .chars()
            .filter(|character| !character.is_whitespace())
            .flat_map(char::to_lowercase)
            .collect::<String>();
        let length = normalized.chars().count();
        if !(2..=160).contains(&length) || !normalized_ax.contains(&normalized) {
            continue;
        }
        matches += 1;
        numeric_match |= normalized
            .chars()
            .any(|character| character.is_ascii_digit());
        if matches >= 3 && numeric_match {
            return true;
        }
    }
    matches >= 5
}

fn applescript_window_id_literal(value: &str) -> String {
    if value.chars().all(|character| character.is_ascii_digit()) {
        value.to_string()
    } else {
        format!("\"{}\"", escape_applescript_string(value))
    }
}

fn browser_extraction_javascript() -> String {
    format!(
        r#"(function() {{
            var clean = function(v) {{ return String(v || '').replace(/\s+/g, ' ').trim(); }};
            var rawText = String(document.body ? (document.body.innerText || document.body.textContent) : '');
            var text = rawText.split(/\r?\n/).map(clean).filter(Boolean).join('\n');
            if (text.length > {max_chars}) text = text.substring(0, {max_chars});
            var tables = Array.prototype.slice.call(document.querySelectorAll('table'), 0, 20).map(function(table) {{
                return Array.prototype.slice.call(table.querySelectorAll('tr'), 0, 200).map(function(row) {{
                    return Array.prototype.slice.call(row.querySelectorAll('th,td'), 0, 40).map(function(cell) {{ return clean(cell.innerText || cell.textContent); }});
                }}).filter(function(row) {{ return row.some(Boolean); }});
            }}).filter(function(table) {{ return table.length > 0; }});
            var labels = Array.prototype.slice.call(document.querySelectorAll('[aria-label]'), 0, 500)
                .map(function(node) {{ return clean(node.getAttribute('aria-label')); }})
                .filter(function(label) {{ return /\d/.test(label); }}).slice(0, 200);
            var textBlocks = Array.prototype.slice.call(document.querySelectorAll('h1,h2,h3,h4,p,li,blockquote,dt,dd'), 0, 800)
                .map(function(node) {{ return clean(node.innerText || node.textContent); }})
                .filter(function(value) {{ return value.length >= 6 && value.length <= 500; }})
                .slice(0, 500);
            var loadingMarkerCount = (text.match(/加载中|数据加载中|loading(?:\.\.\.)?/ig) || []).length;
            var numericTokenCount = (text.match(/[+-]?\d[\d,]*(?:\.\d+)?%?/g) || []).length;
            return JSON.stringify({{title: clean(document.title), url: location.href, text: text, structured_data: {{tables: tables, metric_labels: labels, text_blocks: textBlocks, page_state: {{loading_marker_count: loadingMarkerCount, numeric_token_count: numericTokenCount, likely_loading: loadingMarkerCount > 0}}}}}});
        }})()"#,
        max_chars = MAX_SCRAPED_CHARS,
    )
}

fn browser_readiness_javascript() -> String {
    // SPA 报表往往先渲染数千字菜单壳层，再异步加载指标。仅按文字长度
    // 判断会在“加载中”阶段提前截图；仍返回数字以保持 AppleScript 简单。
    "(function(){var text=String(document.body ? (document.body.innerText || document.body.textContent || '') : '').replace(/\\s+/g,' ').trim();var loading=(text.match(/加载中|数据加载中|loading(?:\\.\\.\\.)?/ig)||[]).length;return loading>0?0:text.length;})()".to_string()
}

fn build_chromium_scrape_script(
    adapter: BrowserAdapter,
    url: &str,
    readiness_javascript: &str,
    javascript: &str,
) -> String {
    format!(
        r#"
        tell application "{app_name}"
            if (count of windows) is 0 then error "BROWSER_ATTACH_UNAVAILABLE"
            set target_window to last window
            set original_active_index to active tab index of target_window
            set report_tab to missing value
            try
                set report_tab to make new tab at end of tabs of target_window with properties {{URL:"about:blank"}}
                set active tab index of target_window to original_active_index
                set URL of report_tab to "{url}"
                repeat 80 times
                    if loading of report_tab is false then exit repeat
                    delay 0.25
                end repeat
                set last_text_length to 0
                set stable_read_count to 0
                repeat 36 times
                    set current_text_length to execute report_tab javascript "{readiness_javascript}"
                    if current_text_length is greater than or equal to 500 then
                        if current_text_length is last_text_length then
                            set stable_read_count to stable_read_count + 1
                        else
                            set stable_read_count to 0
                        end if
                        if stable_read_count is greater than or equal to 3 then exit repeat
                    end if
                    set last_text_length to current_text_length
                    delay 0.5
                end repeat
                set payload to execute report_tab javascript "{javascript}"
                try
                    if active tab of target_window is not report_tab then close report_tab
                end try
                return payload
            on error error_message
                try
                    if report_tab is not missing value then
                        if active tab of target_window is not report_tab then close report_tab
                    end if
                end try
                error error_message
            end try
        end tell
        "#,
        app_name = adapter.app_name,
        url = escape_applescript_string(url),
        readiness_javascript = escape_applescript_string(readiness_javascript),
        javascript = escape_applescript_string(javascript),
    )
}

fn build_safari_scrape_script(
    adapter: BrowserAdapter,
    url: &str,
    readiness_javascript: &str,
    javascript: &str,
) -> String {
    format!(
        r#"
        tell application "{app_name}"
            if (count of windows) is 0 then error "BROWSER_ATTACH_UNAVAILABLE"
            set target_window to last window
            set original_tab to current tab of target_window
            set report_tab to missing value
            try
                set report_tab to make new tab at end of tabs of target_window with properties {{URL:"about:blank"}}
                set current tab of target_window to original_tab
                set URL of report_tab to "{url}"
                repeat 80 times
                    try
                        if (do JavaScript "document.readyState" in report_tab) is "complete" then exit repeat
                    end try
                    delay 0.25
                end repeat
                set last_text_length to 0
                set stable_read_count to 0
                repeat 36 times
                    set current_text_length to do JavaScript "{readiness_javascript}" in report_tab
                    if current_text_length is greater than or equal to 500 then
                        if current_text_length is last_text_length then
                            set stable_read_count to stable_read_count + 1
                        else
                            set stable_read_count to 0
                        end if
                        if stable_read_count is greater than or equal to 3 then exit repeat
                    end if
                    set last_text_length to current_text_length
                    delay 0.5
                end repeat
                set payload to do JavaScript "{javascript}" in report_tab
                try
                    if current tab of target_window is not report_tab then close report_tab
                end try
                return payload
            on error error_message
                try
                    if report_tab is not missing value then
                        if current tab of target_window is not report_tab then close report_tab
                    end if
                end try
                error error_message
            end try
        end tell
        "#,
        app_name = adapter.app_name,
        url = escape_applescript_string(url),
        readiness_javascript = escape_applescript_string(readiness_javascript),
        javascript = escape_applescript_string(javascript),
    )
}

fn build_background_browser_extract_script(
    adapter: BrowserAdapter,
    apple_script_id: &str,
    readiness_javascript: &str,
    javascript: &str,
) -> String {
    let window_id = applescript_window_id_literal(apple_script_id);
    match adapter.script_kind {
        BrowserScriptKind::Chromium => format!(
            r#"
            tell application "{app_name}"
                set target_window to first window whose id is {window_id}
                set report_tab to active tab of target_window
                repeat 80 times
                    if loading of report_tab is false then exit repeat
                    delay 0.25
                end repeat
                set last_text_length to 0
                set stable_read_count to 0
                repeat 36 times
                    set current_text_length to execute report_tab javascript "{readiness_javascript}"
                    if current_text_length is greater than or equal to 500 then
                        if current_text_length is last_text_length then
                            set stable_read_count to stable_read_count + 1
                        else
                            set stable_read_count to 0
                        end if
                        if stable_read_count is greater than or equal to 3 then exit repeat
                    end if
                    set last_text_length to current_text_length
                    delay 0.5
                end repeat
                return execute report_tab javascript "{javascript}"
            end tell
            "#,
            app_name = adapter.app_name,
            window_id = window_id,
            readiness_javascript = escape_applescript_string(readiness_javascript),
            javascript = escape_applescript_string(javascript),
        ),
        BrowserScriptKind::Safari => format!(
            r#"
            tell application "{app_name}"
                set target_window to first window whose id is {window_id}
                set report_tab to current tab of target_window
                repeat 80 times
                    try
                        if (do JavaScript "document.readyState" in report_tab) is "complete" then exit repeat
                    end try
                    delay 0.25
                end repeat
                set last_text_length to 0
                set stable_read_count to 0
                repeat 36 times
                    set current_text_length to do JavaScript "{readiness_javascript}" in report_tab
                    if current_text_length is greater than or equal to 500 then
                        if current_text_length is last_text_length then
                            set stable_read_count to stable_read_count + 1
                        else
                            set stable_read_count to 0
                        end if
                        if stable_read_count is greater than or equal to 3 then exit repeat
                    end if
                    set last_text_length to current_text_length
                    delay 0.5
                end repeat
                return do JavaScript "{javascript}" in report_tab
            end tell
            "#,
            app_name = adapter.app_name,
            window_id = window_id,
            readiness_javascript = escape_applescript_string(readiness_javascript),
            javascript = escape_applescript_string(javascript),
        ),
    }
}

fn build_background_browser_evaluate_script(
    adapter: BrowserAdapter,
    apple_script_id: &str,
    javascript: &str,
) -> String {
    let window_id = applescript_window_id_literal(apple_script_id);
    match adapter.script_kind {
        BrowserScriptKind::Chromium => format!(
            r#"
            tell application "{app_name}"
                set target_window to first window whose id is {window_id}
                return execute active tab of target_window javascript "{javascript}"
            end tell
            "#,
            app_name = adapter.app_name,
            window_id = window_id,
            javascript = escape_applescript_string(javascript),
        ),
        BrowserScriptKind::Safari => format!(
            r#"
            tell application "{app_name}"
                set target_window to first window whose id is {window_id}
                return do JavaScript "{javascript}" in current tab of target_window
            end tell
            "#,
            app_name = adapter.app_name,
            window_id = window_id,
            javascript = escape_applescript_string(javascript),
        ),
    }
}

fn build_background_browser_start_script(
    adapter: BrowserAdapter,
    url: &str,
    preview_token: &str,
) -> String {
    match adapter.script_kind {
        BrowserScriptKind::Chromium => format!(
            r#"
            tell application "System Events"
                set previous_front_app to name of first application process whose frontmost is true
            end tell
            tell application "{app_name}"
                set original_front_window to missing value
                if (count of windows) is greater than 0 then set original_front_window to front window
                set preview_window to make new window with properties {{visible:false, bounds:{{80, 80, 1280, 820}}}}
                set given name of preview_window to "MemoryBread Preview {preview_token}"
                set URL of active tab of preview_window to "{url}"
                set visible of preview_window to true
                if original_front_window is not missing value then set index of original_front_window to 1
                set preview_window_id to id of preview_window as text
            end tell
            tell application "System Events"
                try
                    set frontmost of first application process whose name is previous_front_app to true
                end try
            end tell
            return preview_window_id
            "#,
            app_name = adapter.app_name,
            preview_token = escape_applescript_string(preview_token),
            url = escape_applescript_string(url),
        ),
        BrowserScriptKind::Safari => format!(
            r#"
            tell application "System Events"
                set previous_front_app to name of first application process whose frontmost is true
            end tell
            tell application "{app_name}"
                set original_front_window to missing value
                if (count of windows) is greater than 0 then set original_front_window to front window
                make new document with properties {{URL:"about:blank"}}
                set preview_window to front window
                set visible of preview_window to false
                set bounds of preview_window to {{80, 80, 1280, 820}}
                set URL of current tab of preview_window to "{url}"
                set visible of preview_window to true
                if original_front_window is not missing value then set index of original_front_window to 1
                set preview_window_id to id of preview_window as text
            end tell
            tell application "System Events"
                try
                    set frontmost of first application process whose name is previous_front_app to true
                end try
            end tell
            return preview_window_id
            "#,
            app_name = adapter.app_name,
            url = escape_applescript_string(url),
        ),
    }
}

fn build_background_browser_prepare_capture_script(
    adapter: BrowserAdapter,
    apple_script_id: &str,
) -> String {
    let window_id = applescript_window_id_literal(apple_script_id);
    format!(
        r#"
        tell application "System Events"
            set previous_front_app to name of first application process whose frontmost is true
        end tell
        tell application "{app_name}"
            set index of first window whose id is {window_id} to 1
        end tell
        tell application "System Events"
            try
                set frontmost of first application process whose name is previous_front_app to true
            end try
        end tell
        "#,
        app_name = adapter.app_name,
        window_id = window_id,
    )
}

fn build_background_browser_cleanup_script(
    adapter: BrowserAdapter,
    apple_script_id: &str,
    launched_browser: bool,
) -> String {
    let window_id = applescript_window_id_literal(apple_script_id);
    let quit_if_empty = if launched_browser {
        "if (count of windows) is 0 then quit"
    } else {
        ""
    };
    format!(
        r#"
        tell application "{app_name}"
            try
                close first window whose id is {window_id}
            end try
            {quit_if_empty}
        end tell
        "#,
        app_name = adapter.app_name,
        window_id = window_id,
        quit_if_empty = quit_if_empty,
    )
}

fn build_background_browser_orphan_cleanup_script(
    adapter: BrowserAdapter,
    preview_token: &str,
    launched_browser: bool,
) -> String {
    let quit_if_empty = if launched_browser {
        "if (count of windows) is 0 then quit"
    } else {
        ""
    };
    match adapter.script_kind {
        BrowserScriptKind::Chromium => format!(
            r#"
            tell application "{app_name}"
                repeat with candidate_window in windows
                    try
                        if (given name of candidate_window) contains "{preview_token}" then
                            close candidate_window
                            exit repeat
                        end if
                    end try
                end repeat
                {quit_if_empty}
            end tell
            "#,
            app_name = adapter.app_name,
            preview_token = escape_applescript_string(preview_token),
            quit_if_empty = quit_if_empty,
        ),
        // Safari 窗口没有可写的专用名称。启动失败后仅处理“本轮后台启动且没有
        // 窗口”的进程，不能只凭通用边界去关闭一个可能属于用户的窗口。
        BrowserScriptKind::Safari => format!(
            r#"
            tell application "{app_name}"
                {quit_if_empty}
            end tell
            "#,
            app_name = adapter.app_name,
            quit_if_empty = quit_if_empty,
        ),
    }
}

fn browser_scripting_is_disabled(stderr: &str) -> bool {
    stderr.contains("javascript")
        && (stderr.contains("apple")
            || stderr.contains("turned off")
            || stderr.contains("disabled")
            || stderr.contains("develop menu"))
}

async fn scrape_http(url: &str) -> Result<ScrapeResult, DataToolError> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::limited(5))
        .build()
        .map_err(|_| internal_scrape_error())?;
    let response = client
        .get(url)
        .header("User-Agent", "MemoryBreadDataTool/1.0")
        .send()
        .await
        .map_err(|_| internal_scrape_error())?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err(DataToolError::new(
            StatusCode::UNAUTHORIZED,
            "SCRAPE_AUTH_REQUIRED",
            "页面需要在受支持浏览器中完成登录",
        ));
    }
    if !response.status().is_success() {
        return Err(internal_scrape_error());
    }
    let final_url = response.url().to_string();
    let html = response.text().await.map_err(|_| internal_scrape_error())?;
    let title = extract_html_title(&html).unwrap_or_else(|| "数据报表".to_string());
    let content_text = html_to_text(&html, MAX_SCRAPED_CHARS);
    if looks_like_auth_page(&title, &final_url, &content_text) {
        return Err(DataToolError::new(
            StatusCode::UNAUTHORIZED,
            "SCRAPE_AUTH_REQUIRED",
            "页面需要在受支持浏览器中完成登录",
        ));
    }
    if content_text.is_empty() {
        return Err(DataToolError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "SCRAPE_EMPTY",
            "页面没有可采纳的数据正文",
        ));
    }
    Ok(ScrapeResult {
        collector: "direct_http",
        browser: None,
        interaction_mode: "none",
        title,
        url: redact_url_credentials(&final_url).unwrap_or_else(|| url.to_string()),
        content_text,
        structured_data: json!({"extraction": "html_text"}),
        screenshot: None,
    })
}

fn validate_scrape_url(url: &str) -> Result<(), DataToolError> {
    let parsed = reqwest::Url::parse(url).map_err(|_| {
        DataToolError::new(StatusCode::BAD_REQUEST, "BAD_REQUEST", "数据源 URL 无效")
    })?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err(DataToolError::new(
            StatusCode::BAD_REQUEST,
            "BAD_REQUEST",
            "只允许不含凭据的 HTTP 或 HTTPS URL",
        ));
    }
    Ok(())
}

fn redact_url_credentials(url: &str) -> Option<String> {
    let mut parsed = reqwest::Url::parse(url).ok()?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return None;
    }
    let filtered_pairs = parsed
        .query_pairs()
        .filter(|(key, _)| !is_sensitive_query_key(key))
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
    Some(parsed.to_string())
}

fn is_sensitive_query_key(key: &str) -> bool {
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

fn escape_applescript_string(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn extract_html_title(html: &str) -> Option<String> {
    let lower = html.to_lowercase();
    let start = lower.find("<title")?;
    let content_start = lower[start..].find('>')? + start + 1;
    let end = lower[content_start..].find("</title>")? + content_start;
    Some(decode_entities(html[content_start..end].trim()))
}

fn html_to_text(html: &str, max_chars: usize) -> String {
    let lower = html.to_lowercase();
    let mut output = String::new();
    let mut index = 0;
    let bytes = html.as_bytes();
    while index < bytes.len() && output.chars().count() < max_chars {
        if lower[index..].starts_with("<script") {
            if let Some(end) = lower[index..].find("</script>") {
                index += end + "</script>".len();
                continue;
            }
        }
        if lower[index..].starts_with("<style") {
            if let Some(end) = lower[index..].find("</style>") {
                index += end + "</style>".len();
                continue;
            }
        }
        if bytes[index] == b'<' {
            if let Some(end) = html[index..].find('>') {
                output.push('\n');
                index += end + 1;
                continue;
            }
        }
        let ch = html[index..].chars().next().unwrap_or_default();
        output.push(ch);
        index += ch.len_utf8().max(1);
    }
    let decoded = decode_entities(&output);
    decoded
        .lines()
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
        .chars()
        .take(max_chars)
        .collect()
}

fn decode_entities(value: &str) -> String {
    value
        .replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
}

fn looks_like_auth_page(title: &str, url: &str, content: &str) -> bool {
    let evidence = format!("{title}\n{url}\n{}", clip_text(content, 2000)).to_lowercase();
    [
        "sign in",
        "log in",
        "login",
        "登录",
        "扫码登录",
        "统一身份认证",
        "sso",
        "oauth",
    ]
    .iter()
    .any(|marker| evidence.contains(marker))
}

fn clip_text(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.trim().to_string();
    }
    value.chars().take(max_chars).collect::<String>() + "…"
}

fn internal_scrape_error() -> DataToolError {
    DataToolError::new(
        StatusCode::BAD_GATEWAY,
        "SCRAPE_FAILED",
        "网页采集暂时失败，请保留旧快照并稍后重试",
    )
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or(0)
}

impl From<StorageError> for DataToolError {
    fn from(_: StorageError) -> Self {
        internal_scrape_error()
    }
}

fn default_scrape_mode() -> String {
    "auto".to_string()
}

fn default_browser_preference() -> String {
    "auto".to_string()
}

fn default_true() -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_html_extraction_removes_scripts_and_decodes_text() {
        let html = "<html><head><title>周报 &amp; 看板</title><script>secret()</script></head><body><h1>GMV</h1><p>本周 120 万</p></body></html>";
        assert_eq!(extract_html_title(html).as_deref(), Some("周报 & 看板"));
        let text = html_to_text(html, 1000);
        assert!(text.contains("GMV"));
        assert!(text.contains("本周 120 万"));
        assert!(!text.contains("secret"));
    }

    #[test]
    fn scrape_url_rejects_file_and_embedded_credentials() {
        assert!(validate_scrape_url("https://example.com/report").is_ok());
        assert!(validate_scrape_url("file:///tmp/report").is_err());
        assert!(validate_scrape_url("https://user:pass@example.com/report").is_err());
    }

    #[test]
    fn scrape_response_url_redacts_tokens_but_keeps_business_filters() {
        assert_eq!(
            redact_url_credentials(
                "https://bi.example.com/report?team=a&access_token=secret#chart"
            )
            .as_deref(),
            Some("https://bi.example.com/report?team=a#chart")
        );
    }

    #[test]
    fn auth_page_detection_handles_common_sso_pages() {
        assert!(looks_like_auth_page(
            "统一身份认证",
            "https://sso.example.com/login",
            "请扫码登录"
        ));
        assert!(!looks_like_auth_page(
            "经营数据看板",
            "https://bi.example.com/dashboard",
            "本周订单 1200"
        ));
    }

    #[test]
    fn browser_candidates_prefer_the_source_browser_and_allow_explicit_choice() {
        let automatic = browser_candidates(Some("auto"), Some("Microsoft Edge"));
        assert_eq!(automatic.first().map(|item| item.id), Some("edge"));
        assert_eq!(automatic.len(), BROWSER_ADAPTERS.len());

        let explicit = browser_candidates(Some("safari"), Some("Google Chrome"));
        assert_eq!(
            explicit.iter().map(|item| item.id).collect::<Vec<_>>(),
            vec!["safari"]
        );
    }

    #[test]
    fn browser_scripts_keep_the_temporary_tab_in_the_background() {
        let javascript = browser_extraction_javascript();
        let readiness_javascript = browser_readiness_javascript();
        let chromium = build_chromium_scrape_script(
            BROWSER_ADAPTERS[0],
            "https://example.com",
            &readiness_javascript,
            &javascript,
        );
        let safari = build_safari_scrape_script(
            *BROWSER_ADAPTERS.last().unwrap(),
            "https://example.com",
            &readiness_javascript,
            &javascript,
        );

        assert!(chromium.contains("last window"));
        assert!(chromium.contains("set active tab index of target_window to original_active_index"));
        assert!(chromium
            .contains("if active tab of target_window is not report_tab then close report_tab"));
        assert!(safari.contains("set current tab of target_window to original_tab"));
        assert!(safari
            .contains("if current tab of target_window is not report_tab then close report_tab"));
        assert!(chromium.contains("stable_read_count"));
        assert!(safari.contains("stable_read_count"));
        assert!(javascript.contains("loading_marker_count"));
        assert!(javascript.contains("likely_loading"));
        assert!(readiness_javascript.contains("loading>0?0"));
    }

    #[test]
    fn evidence_scripts_use_a_dedicated_background_window_and_restore_focus() {
        let javascript = browser_extraction_javascript();
        let readiness_javascript = browser_readiness_javascript();
        let preview_id = "2d870d80-e2a2-4424-a732-069e174f2796";
        let chromium_start = build_background_browser_start_script(
            BROWSER_ADAPTERS[0],
            "https://example.com/dashboard",
            preview_id,
        );
        let safari_start = build_background_browser_start_script(
            *BROWSER_ADAPTERS.last().unwrap(),
            "https://example.com/dashboard",
            preview_id,
        );
        let chromium_extract = build_background_browser_extract_script(
            BROWSER_ADAPTERS[0],
            "12345",
            &readiness_javascript,
            &javascript,
        );
        let safari_extract = build_background_browser_extract_script(
            *BROWSER_ADAPTERS.last().unwrap(),
            "54321",
            &readiness_javascript,
            &javascript,
        );
        let chromium_prepare =
            build_background_browser_prepare_capture_script(BROWSER_ADAPTERS[0], "12345");
        let safari_prepare = build_background_browser_prepare_capture_script(
            *BROWSER_ADAPTERS.last().unwrap(),
            "54321",
        );
        let chromium_cleanup =
            build_background_browser_cleanup_script(BROWSER_ADAPTERS[0], "12345", false);
        let safari_cleanup = build_background_browser_cleanup_script(
            *BROWSER_ADAPTERS.last().unwrap(),
            "54321",
            false,
        );
        let chromium_orphan_cleanup =
            build_background_browser_orphan_cleanup_script(BROWSER_ADAPTERS[0], preview_id, true);
        let safari_orphan_cleanup = build_background_browser_orphan_cleanup_script(
            *BROWSER_ADAPTERS.last().unwrap(),
            preview_id,
            true,
        );

        for script in [&chromium_start, &safari_start] {
            assert!(script.contains("previous_front_app"));
            assert!(script.contains("frontmost of first application process"));
            assert!(script.contains("set index of original_front_window to 1"));
            assert!(!script.contains("activate"));
        }
        assert!(chromium_start.contains("visible:false"));
        assert!(chromium_start.contains("MemoryBread Preview"));
        assert!(safari_start.contains("set visible of preview_window to false"));
        for script in [&chromium_extract, &safari_extract] {
            assert!(script.contains("first window whose id is"));
            assert!(script.contains("stable_read_count"));
            assert!(!script.contains("front window"));
            assert!(!script.contains("screencapture"));
            assert!(!script.contains("activate"));
        }
        for script in [&chromium_prepare, &safari_prepare] {
            assert!(script.contains("previous_front_app"));
            assert!(script.contains("set index of first window whose id is"));
            assert!(script.contains("frontmost of first application process"));
            assert!(!script.contains("activate"));
        }
        assert!(chromium_cleanup.contains("close first window whose id is 12345"));
        assert!(safari_cleanup.contains("close first window whose id is 54321"));
        assert!(chromium_orphan_cleanup.contains("given name of candidate_window"));
        assert!(!safari_orphan_cleanup.contains("close candidate_window"));
        assert!(chromium_orphan_cleanup.contains("then quit"));
        assert!(safari_orphan_cleanup.contains("then quit"));

        #[cfg(target_os = "macos")]
        {
            let output_dir = tempfile::tempdir().unwrap();
            for (index, script) in [
                &chromium_start,
                &safari_start,
                &chromium_extract,
                &safari_extract,
                &chromium_prepare,
                &safari_prepare,
                &chromium_cleanup,
                &safari_cleanup,
                &chromium_orphan_cleanup,
                &safari_orphan_cleanup,
            ]
            .into_iter()
            .enumerate()
            {
                let output_path = output_dir.path().join(format!("evidence-{index}.scpt"));
                let output = Command::new("/usr/bin/osacompile")
                    .args(["-e", script, "-o"])
                    .arg(&output_path)
                    .output()
                    .unwrap();
                assert!(
                    output.status.success(),
                    "evidence AppleScript did not compile: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
            }
        }
    }

    #[test]
    fn rendered_content_guard_rejects_blank_browser_body() {
        use image::{Rgba, RgbaImage};

        let blank = RgbaImage::from_pixel(1200, 740, Rgba([255, 255, 255, 255]));
        assert!(!browser_screenshot_has_rendered_content(&blank));

        let mut report = blank.clone();
        for y in 220..520 {
            for x in 160..1040 {
                report.put_pixel(x, y, Rgba([20, 32, 58, 255]));
            }
        }
        assert!(browser_screenshot_has_rendered_content(&report));
    }

    #[test]
    fn accessibility_primary_requires_meaningful_dom_overlap() {
        let dom = "在用项目数\n102\n总卡数（X40折算）\n1803.59\n年化总成本（万元）\n12178.4万元";
        let ax = "项目 GPU 用量管理\n在用项目数\n102\n总卡数（X40折算）\n1803.59\n年化总成本（万元）\n12178.4万元";
        assert!(accessibility_text_covers_dom(ax, dom));
        assert!(!accessibility_text_covers_dom(
            "Google Chrome\n地址和搜索栏\n后退\n刷新\n完成更新",
            dom,
        ));
    }

    #[test]
    fn long_screenshot_positions_cover_regular_pages_and_keep_the_last_boundary() {
        assert_eq!(
            browser_scroll_positions(BrowserPageGeometry {
                outer_height: 740,
                inner_height: 650,
                scroll_height: 1_800,
            }),
            vec![0, 650, 1_150]
        );
        let very_tall = browser_scroll_positions(BrowserPageGeometry {
            outer_height: 740,
            inner_height: 650,
            scroll_height: 50_000,
        });
        assert_eq!(very_tall.first(), Some(&0));
        assert_eq!(very_tall.last(), Some(&(50_000 - 650)));
        assert!(very_tall.len() <= 20);
    }

    #[test]
    fn evidence_capture_reuses_a_valid_preview_id_and_rejects_invalid_values() {
        let preview_id = "2d870d80-e2a2-4424-a732-069e174f2796";
        assert_eq!(normalize_preview_id(Some(preview_id)).unwrap(), preview_id);
        assert!(normalize_preview_id(Some("../bad")).is_err());
    }

    #[test]
    fn refresh_request_defaults_to_retaining_screenshot() {
        let legacy: RefreshDataSourceRequest = serde_json::from_value(json!({})).unwrap();
        assert!(legacy.retain_screenshot);
        let disabled: RefreshDataSourceRequest = serde_json::from_value(json!({
            "capture_evidence": true,
            "retain_screenshot": false,
        }))
        .unwrap();
        assert!(!disabled.retain_screenshot);
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "需要本机 Chrome、Apple Events JavaScript 与录屏权限"]
    fn background_browser_window_keeps_front_app_and_user_tab_unchanged() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2048];
            let _ = stream.read(&mut request);
            let body = format!(
                "<html><head><title>MemoryBread 后台预览自测</title></head><body><h1>经营看板</h1><p>{}</p></body></html>",
                "本周订单 1200，环比增长 8%。".repeat(80)
            );
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        });
        let browser_context = || {
            let script = r#"
                tell application "System Events" to set front_app to name of first application process whose frontmost is true
                tell application "Google Chrome"
                    if (count of windows) is 0 then return front_app & "|no-window"
                    return front_app & "|" & (id of front window as text) & "|" & (active tab index of front window as text)
                end tell
            "#;
            let output = run_browser_script(script).unwrap();
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        };
        let before = browser_context();
        let output_dir = tempfile::tempdir().unwrap();
        let evidence_path = output_dir
            .path()
            .join("2d870d80-e2a2-4424-a732-069e174f2796.jpg");
        let result = scrape_with_browser(
            &format!("http://{address}/dashboard"),
            Some("chrome"),
            Some("Google Chrome"),
            Some("2d870d80-e2a2-4424-a732-069e174f2796"),
            Some(&evidence_path),
        )
        .unwrap();
        server.join().unwrap();

        assert_eq!(result.interaction_mode, "background_browser_window");
        assert_eq!(result.title, "MemoryBread 后台预览自测");
        assert!(result.content_text.contains("本周订单 1200"));
        assert!(result.screenshot.is_some());
        assert!(evidence_path.is_file());
        let (_, height) = image::image_dimensions(&evidence_path).unwrap();
        assert!(height > 740, "长页面证据应由多个视口拼接");
        assert_eq!(browser_context(), before);
    }
}
