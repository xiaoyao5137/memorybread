use axum::{
    body::Body,
    http::{Method, Request, StatusCode},
};
use http_body_util::BodyExt;
use memory_bread_core::{
    api::{create_router, AppState},
    storage::StorageManager,
};
use tower::ServiceExt;

#[tokio::test]
async fn capture_status_is_enabled_by_default_and_persists_updates() {
    let temp_dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp_dir.path().join("runtime.db")).unwrap();
    let router = create_router(AppState::new(storage.clone()));

    let initial = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/runtime/status")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(initial.status(), StatusCode::OK);
    let initial_body = initial.into_body().collect().await.unwrap().to_bytes();
    let initial_json: serde_json::Value = serde_json::from_slice(&initial_body).unwrap();
    // 新安装默认开启采集，让用户可以立即体验核心功能。
    assert_eq!(initial_json["capture_enabled"], true);

    let updated = router
        .oneshot(
            Request::builder()
                .method(Method::PUT)
                .uri("/api/runtime/status")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"capture_enabled":false}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(updated.status(), StatusCode::OK);

    let restored_state = AppState::new(storage);
    assert!(!restored_state.is_capture_enabled());
}
