//! Exercise response headers, idle keepalive and persisted terminal events through
//! the real router. The model is gated so receipt cannot be mistaken for completion.
use axum::{
    body::Body,
    http::{Request, StatusCode},
    routing::post,
    Json, Router,
};
use futures::StreamExt;
use http_body_util::BodyExt;
use memory_bread_core::{
    api::{create_router, state::AppState},
    storage::StorageManager,
};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::sync::Notify;
use tower::ServiceExt;

struct Fixture {
    router: Router,
    state: Arc<AppState>,
    release: Arc<Notify>,
    entered: Arc<Notify>,
    calls: Arc<AtomicUsize>,
    server: tokio::task::JoinHandle<()>,
    _tmp: tempfile::TempDir,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.server.abort();
    }
}

impl Fixture {
    async fn new() -> Self {
        let release = Arc::new(Notify::new());
        let entered = Arc::new(Notify::new());
        let calls = Arc::new(AtomicUsize::new(0));
        let gate = release.clone();
        let count = calls.clone();
        let arrival = entered.clone();
        let sidecar = Router::new().route("/creation/brainstorm/next", post(move || {
            let gate = gate.clone();
            let count = count.clone();
            let arrival = arrival.clone();
            async move {
                count.fetch_add(1, Ordering::SeqCst);
                arrival.notify_one();
                gate.notified().await;
                Json(json!({
                    "status": "question", "readiness_reason": "仍需确认目标", "open_flags": ["目标"],
                    "question": {
                        "id": "goal", "dimension_id": "business_outcome", "dimension": "目标",
                        "type": "multi_choice", "prompt": "优先改善什么？", "why_now": "明确范围。",
                        "required": true, "allow_custom": true, "options": [
                            {"id":"quality", "label":"改善质量", "description":"先验证效果。", "recommended":true},
                            {"id":"coverage", "label":"扩大覆盖", "description":"先扩大范围。", "recommended":false}
                        ], "answer_template": "补充目标"
                    }
                }))
            }
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, sidecar).await.unwrap();
        });
        let tmp = tempfile::tempdir().unwrap();
        let storage = StorageManager::open(&tmp.path().join("brainstorm.db")).unwrap();
        let state = AppState::with_service_urls(storage, "http://127.0.0.1:9".into(), url, vec![]);
        Self {
            router: create_router(state.clone()),
            state,
            release,
            entered,
            calls,
            server,
            _tmp: tmp,
        }
    }

    fn saved_count(&self) -> i64 {
        self.state
            .storage
            .with_conn(|conn| {
                Ok(conn.query_row(
                    "SELECT count(*) FROM creation_brainstorm_sessions",
                    [],
                    |row| row.get(0),
                )?)
            })
            .unwrap()
    }
}

fn request(accept: &str, root: &str) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri("/api/creation/brainstorm/turn")
        .header("content-type", "application/json")
        .header("accept", accept)
        .body(Body::from(
            json!({"session_id":"transport-session", "root_request":root, "action":"start"})
                .to_string(),
        ))
        .unwrap()
}

#[tokio::test]
async fn brainstorm_stream_keeps_idle_connection_alive_and_persists_before_completion() {
    let fixture = Fixture::new().await;
    let response = tokio::time::timeout(
        Duration::from_secs(1),
        fixture
            .router
            .clone()
            .oneshot(request("text/event-stream", "制定方案")),
    )
    .await
    .expect("headers must not wait for inference")
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    let mut stream = response.into_body().into_data_stream();
    let started = tokio::time::timeout(Duration::from_secs(1), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(String::from_utf8_lossy(&started).contains("event: brainstorm.started"));
    assert_eq!(fixture.saved_count(), 0);
    // Production keepalive interval, with the upstream still gated.
    let heartbeat = tokio::time::timeout(Duration::from_secs(12), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(String::from_utf8_lossy(&heartbeat).contains(": keep-alive"));
    assert_eq!(fixture.calls.load(Ordering::SeqCst), 1);
    assert_eq!(fixture.saved_count(), 0);
    fixture.release.notify_one();
    let completed = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let completed = String::from_utf8_lossy(&completed);
    assert!(completed.contains("event: brainstorm.completed"));
    let data: Value = serde_json::from_str(
        completed
            .lines()
            .find_map(|line| line.strip_prefix("data: "))
            .unwrap(),
    )
    .unwrap();
    assert_eq!(data["state"]["current_question"]["id"], "goal");
    assert_eq!(data["state"]["revision"], 0);
    assert_eq!(data["state"]["answered_count"], 0);
    assert_eq!(
        fixture.saved_count(),
        1,
        "terminal success must follow the database write"
    );
    assert!(stream.next().await.is_none());

    // The legacy JSON client restores the same result without another model call.
    let restored = fixture
        .router
        .clone()
        .oneshot(request("application/json", "制定方案"))
        .await
        .unwrap();
    assert_eq!(restored.status(), StatusCode::OK);
    let state: Value =
        serde_json::from_slice(&restored.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(state, data["state"]);
    assert_eq!(fixture.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn brainstorm_stream_failure_preserves_code_and_original_status() {
    let fixture = Fixture::new().await;
    let streamed = fixture
        .router
        .clone()
        .oneshot(request("text/event-stream", ""))
        .await
        .unwrap();
    assert_eq!(streamed.status(), StatusCode::OK);
    let body = String::from_utf8(
        streamed
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .to_vec(),
    )
    .unwrap();
    assert!(body.contains("event: brainstorm.failed"));
    assert!(body.contains("\"code\":\"BRAINSTORM_INVALID_REQUEST\""));
    assert!(body.contains("\"status\":400"));
    assert!(!body.contains("brainstorm.completed"));
    let legacy = fixture
        .router
        .clone()
        .oneshot(request("application/json", ""))
        .await
        .unwrap();
    assert_eq!(legacy.status(), StatusCode::BAD_REQUEST);
    let disabled_stream = fixture
        .router
        .clone()
        .oneshot(request("text/event-stream;q=0, application/json", ""))
        .await
        .unwrap();
    assert_eq!(disabled_stream.status(), StatusCode::BAD_REQUEST);
    assert_eq!(
        disabled_stream.headers()["content-type"],
        "application/json"
    );
    assert_eq!(fixture.saved_count(), 0);
    assert_eq!(fixture.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn brainstorm_stream_disconnect_does_not_persist_a_late_result() {
    let fixture = Fixture::new().await;
    let response = fixture
        .router
        .clone()
        .oneshot(request("text/event-stream", "制定方案"))
        .await
        .unwrap();
    let mut stream = response.into_body().into_data_stream();
    stream.next().await.unwrap().unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        tokio::select! {
            _ = fixture.entered.notified() => {},
            item = stream.next() => panic!("unexpected event before upstream release: {item:?}"),
        }
    })
    .await
    .expect("upstream must be executing before we disconnect");
    assert_eq!(fixture.calls.load(Ordering::SeqCst), 1);
    drop(stream);
    fixture.release.notify_one();
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(fixture.saved_count(), 0);
}
