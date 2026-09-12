//! GET /health — 服务健康检查

use crate::{api::state::AppState, storage::StorageManager};
use axum::{extract::State, Json};
use serde::Serialize;
use std::sync::Arc;

/// 健康检查响应体
#[derive(Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub service: &'static str,
    pub version: &'static str,
    pub database: Option<DatabaseHealth>,
}

#[derive(Serialize)]
pub struct DatabaseHealth {
    pub path: String,
    pub required_migrations: Vec<&'static str>,
}

pub async fn health_handler(State(state): State<Arc<AppState>>) -> Json<HealthResponse> {
    // Keep liveness independent of the database mutex during long user writes.
    let database = Some(DatabaseHealth {
        path: state.database_path.clone(),
        required_migrations: StorageManager::required_migrations(),
    });
    Json(HealthResponse {
        status: if database.is_some() { "ok" } else { "degraded" },
        service: "memory-bread-core",
        version: env!("CARGO_PKG_VERSION"),
        database,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_health_returns_ok() {
        let state = AppState::new(StorageManager::open_in_memory().unwrap());
        let Json(resp) = health_handler(State(state)).await;
        assert_eq!(resp.status, "ok");
        assert_eq!(resp.service, "memory-bread-core");
        assert!(!resp.version.is_empty());
        assert_eq!(
            resp.database.unwrap().required_migrations,
            StorageManager::required_migrations()
        );
    }
}
