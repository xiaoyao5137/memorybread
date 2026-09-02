//! GET /health — 服务健康检查

use axum::Json;
use serde::Serialize;

/// 健康检查响应体
#[derive(Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub service: &'static str,
    pub version: &'static str,
}

pub async fn health_handler() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: "memory-bread-core",
        version: env!("CARGO_PKG_VERSION"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_health_returns_ok() {
        let Json(resp) = health_handler().await;
        assert_eq!(resp.status, "ok");
        assert_eq!(resp.service, "memory-bread-core");
        assert!(!resp.version.is_empty());
    }
}
