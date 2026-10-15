use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use memory_bread_core::{
    api::{create_router, AppState},
    capture::BlacklistChecker,
    storage::StorageManager,
};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn request(
    router: &Router,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder().method(method).uri(path);
    let body = if let Some(body) = body {
        builder = builder.header("content-type", "application/json");
        Body::from(body.to_string())
    } else {
        Body::empty()
    };
    let response = router
        .clone()
        .oneshot(builder.body(body).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn name_only_blacklist_round_trip_blocks_and_updates_immediately() {
    let temp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp.path().join("privacy.db")).unwrap();
    let checker = BlacklistChecker::new(storage.clone());
    let router = create_router(AppState::new(storage));
    let observed_bundle = Some("com.alibaba.DingTalk");

    assert!(!checker.is_blacklisted_by_bundle_or_name(observed_bundle, Some("钉钉")));

    let (status, created) = request(
        &router,
        "POST",
        "/api/privacy/blacklist",
        Some(json!({"app_name": "  钉钉  ", "reason": "隐私聊天"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{created}");
    let id = created["data"].as_i64().unwrap();

    let (status, listed) = request(&router, "GET", "/api/privacy/blacklist", None).await;
    assert_eq!(status, StatusCode::OK);
    let row = listed["data"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["id"] == id)
        .unwrap();
    assert_eq!(row["bundle_id"], Value::Null);
    assert_eq!(row["app_name"], "钉钉");
    assert_eq!(row["reason"], "隐私聊天");
    assert!(checker.is_blacklisted_by_bundle_or_name(observed_bundle, Some("钉钉")));

    let (status, duplicate) = request(
        &router,
        "POST",
        "/api/privacy/blacklist",
        Some(json!({"app_name": " 钉钉 ", "bundle_id": ""})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{duplicate}");

    let (status, invalid) = request(
        &router,
        "POST",
        "/api/privacy/blacklist",
        Some(json!({"app_name": "  "})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{invalid}");

    let path = format!("/api/privacy/blacklist/{id}/enabled");
    let (status, body) = request(&router, "PATCH", &path, Some(json!({"enabled": false}))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(!checker.is_blacklisted_by_bundle_or_name(observed_bundle, Some("钉钉")));

    let (status, body) = request(&router, "PATCH", &path, Some(json!({"enabled": true}))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(checker.is_blacklisted_by_bundle_or_name(observed_bundle, Some("钉钉")));

    let path = format!("/api/privacy/blacklist/{id}");
    let (status, body) = request(&router, "DELETE", &path, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(!checker.is_blacklisted_by_bundle_or_name(observed_bundle, Some("钉钉")));
}
