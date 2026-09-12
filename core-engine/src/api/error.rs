//! API 错误类型 → HTTP 响应映射

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

use crate::storage::StorageError;

#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    #[error("storage error: {0}")]
    Storage(#[from] StorageError),

    #[error("not found: {0}")]
    NotFound(String),

    #[error("bad request: {0}")]
    BadRequest(String),

    #[error("internal error: {0}")]
    Internal(String),

    #[error("upstream error ({status}): {message}")]
    Upstream {
        status: StatusCode,
        code: &'static str,
        message: String,
    },
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code, message) = match &self {
            ApiError::NotFound(msg) => (StatusCode::NOT_FOUND, "NOT_FOUND", msg.as_str()),
            ApiError::BadRequest(msg) => (StatusCode::BAD_REQUEST, "BAD_REQUEST", msg.as_str()),
            ApiError::Storage(StorageError::DocumentAutomaticWritesPaused { .. }) => (
                StatusCode::CONFLICT,
                "DOCUMENT_AUTOMATIC_WRITES_PAUSED",
                "自动文档写入已暂停，候选内容保留等待恢复",
            ),
            ApiError::Storage(e) => {
                tracing::error!("storage error: {e}");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "STORAGE_ERROR",
                    "数据库操作失败",
                )
            }
            ApiError::Internal(msg) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                msg.as_str(),
            ),
            ApiError::Upstream {
                status,
                code,
                message,
            } => (*status, *code, message.as_str()),
        };
        (status, Json(json!({ "error": code, "message": message }))).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn automatic_document_pause_is_not_a_database_failure_response() {
        let response=ApiError::Storage(StorageError::DocumentAutomaticWritesPaused { bucket: 0 }).into_response();
        assert_eq!(response.status(),StatusCode::CONFLICT);
        let bytes=axum::body::to_bytes(response.into_body(),4096).await.unwrap();
        let body:serde_json::Value=serde_json::from_slice(&bytes).unwrap();
        assert_eq!(body["error"],"DOCUMENT_AUTOMATIC_WRITES_PAUSED");
    }
}
