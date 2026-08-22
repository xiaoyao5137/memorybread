use std::sync::Arc;

use crate::{api::state::AppState, browser_extension::BrowserExtensionStatus};
use axum::{
    body::Body,
    extract::{Path, State},
    http::{header, Response, StatusCode},
    Json,
};

pub async fn get_browser_extension_status(
    State(state): State<Arc<AppState>>,
) -> Json<BrowserExtensionStatus> {
    Json(state.browser_extension.status())
}

pub async fn get_browser_extension_preview(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
) -> Result<Response<Body>, StatusCode> {
    let job_id = uuid::Uuid::parse_str(job_id.trim())
        .map_err(|_| StatusCode::BAD_REQUEST)?
        .to_string();
    let (mime_type, bytes) = state
        .browser_extension
        .preview(&job_id)
        .ok_or(StatusCode::NOT_FOUND)?;
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, mime_type)
        .header(header::CACHE_CONTROL, "private, no-store, max-age=0")
        .body(Body::from(bytes))
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}
