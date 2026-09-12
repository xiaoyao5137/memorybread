use axum::{
    body::Body,
    http::{Request, StatusCode},
    routing::post,
    Json, Router,
};
use http_body_util::BodyExt;
use memory_bread_core::{api::AppState, storage::StorageManager};
use serde_json::{json, Value};
use tower::ServiceExt;

#[tokio::test]
async fn creation_agent_modes_ignore_legacy_cloud_preferences_and_request_credentials() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, mut rx) = tokio::sync::mpsc::channel(8);
    let sidecar = Router::new().route(
        "/creation/agent/run",
        post(move |Json(payload): Json<Value>| {
            let tx = tx.clone();
            async move {
                tx.send(payload).await.unwrap();
                "data: {\"type\":\"run.completed\"}\n\n"
            }
        }),
    );
    let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("test.db")).unwrap();
    storage.upsert_preference("creation.models", &json!([{
        "id":"legacy-cloud", "enabled":true, "apiKey":"test-secret", "baseUrl":"https://invalid.example"
    }]).to_string(), "user", 1.0).unwrap();
    let state = AppState::with_service_urls(storage, url.clone(), url, vec![]);
    let router = memory_bread_core::api::create_router(state);
    for mode in ["local", "external"] {
        for creation_mode in ["direct", "brainstorm"] {
            for explicit_credentials in [false, true] {
                let mut payload = json!({"user_prompt":"生成测试文档", "model_mode":mode,
                    "creation_mode":creation_mode, "enable_rag":false});
                if explicit_credentials {
                    payload["creation_model"] = json!("legacy-cloud");
                    payload["creation_api_key"] = json!("test-secret");
                    payload["creation_base_url"] = json!("https://invalid.example");
                }
                let response = router
                    .clone()
                    .oneshot(
                        Request::builder()
                            .method("POST")
                            .uri("/api/creation/agent/run")
                            .header("content-type", "application/json")
                            .body(Body::from(payload.to_string()))
                            .unwrap(),
                    )
                    .await
                    .unwrap();
                assert_eq!(response.status(), StatusCode::OK);
                let body = response.into_body().collect().await.unwrap().to_bytes();
                assert!(String::from_utf8_lossy(&body).contains("run.completed"));
                let forwarded = rx.recv().await.unwrap();
                assert_eq!(forwarded["model_mode"], mode);
                assert_eq!(forwarded["creation_mode"], creation_mode);
                assert_eq!(forwarded["execution_origin"], "interactive");
                for field in ["creation_model", "creation_api_key", "creation_base_url"] {
                    assert!(
                        forwarded[field].is_null(),
                        "unexpected {field}: {forwarded}"
                    );
                }
            }
        }
    }
    server.abort();
}

#[tokio::test]
async fn scheduled_creation_origin_survives_core_proxy_and_invalid_origin_is_rejected() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, mut rx) = tokio::sync::mpsc::channel(4);
    let sidecar = Router::new().route(
        "/creation/agent/run",
        post(move |Json(payload): Json<Value>| {
            let tx = tx.clone();
            async move {
                tx.send(payload).await.unwrap();
                "data: {\"type\":\"run.completed\"}\n\n"
            }
        }),
    );
    let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("test.db")).unwrap();
    let router = memory_bread_core::api::create_router(AppState::with_service_urls(
        storage, url.clone(), url, vec![],
    ));
    for origin in ["scheduled_task", "interactive", "unknown"] {
        let response = router.clone().oneshot(
            Request::builder().method("POST").uri("/api/creation/agent/run")
                .header("content-type", "application/json")
                .body(Body::from(json!({"user_prompt":"调度来源测试", "enable_rag":false,
                    "execution_origin":origin}).to_string())).unwrap(),
        ).await.unwrap();
        if origin == "unknown" {
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
            let body = response.into_body().collect().await.unwrap().to_bytes();
            assert!(String::from_utf8_lossy(&body).contains("execution_origin"));
            assert!(rx.try_recv().is_err(), "invalid origin reached sidecar");
        } else {
            assert_eq!(response.status(), StatusCode::OK);
            response.into_body().collect().await.unwrap();
            assert_eq!(rx.recv().await.unwrap()["execution_origin"], origin);
        }
    }
    server.abort();
}

#[tokio::test]
async fn exploring_brainstorm_draft_preserves_saved_questions_and_answers() {
    use memory_bread_core::storage::repo::creation_brainstorm;

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, mut rx) = tokio::sync::mpsc::channel(4);
    let sidecar = Router::new().route(
        "/creation/agent/run",
        post(move |Json(payload): Json<Value>| {
            let tx = tx.clone();
            async move {
                let fail = payload["current_document"].as_str().unwrap_or("") == "retry-base";
                tx.send(payload).await.unwrap();
                if fail {
                    (StatusCode::SERVICE_UNAVAILABLE, "draft unavailable")
                } else {
                    (StatusCode::OK, "data: {\"type\":\"run.completed\"}\n\n")
                }
            }
        }),
    );
    let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("test.db")).unwrap();
    let session_id = "brainstorm-draft-exploring";
    let root_request = "先脑暴跨团队分享会方案，逐题确定方向";
    let saved_state = json!({
        "root_request": root_request,
        "turns": [{"question":{"id":"q-format","prompt":"使用什么形式？"},
            "answer":{"selected_option_ids":["roundtable"],"source":"user","custom_text":""}}],
        "current_question":{"id":"q-budget","prompt":"预算是多少？","required":true},
        "open_flags":["预算待确认"]
    });
    let before = storage.with_conn(|conn| {
        creation_brainstorm::create(conn, session_id, root_request, "exploring", &saved_state.to_string())
            .map_err(Into::into)
    }).unwrap();
    let brief = json!({
        "session_id":session_id,"root_request":root_request,"phase":"exploring","revision":0,
        "current_question":saved_state["current_question"],"open_flags":saved_state["open_flags"],
        "history":saved_state["turns"],
        "decisions":[{"question_id":"q-format","dimension":"形式","summary":"圆桌讨论","source":"user"}]
    });
    let state = AppState::with_service_urls(storage, url.clone(), url, vec![]);
    let router = memory_bread_core::api::create_router(state.clone());
    // Both a completed request and a failed attempt leave exploration untouched.
    for current_document in ["", "retry-base"] {
        let response = router.clone().oneshot(
            Request::builder().method("POST").uri("/api/creation/agent/run")
                .header("content-type", "application/json")
                .body(Body::from(json!({"session_id":session_id,
                    "user_prompt":"先生成一版，未回答内容标注待确认，不编造事实。",
                    "root_request":root_request,"current_document":current_document,
                    "creation_mode":"brainstorm","creation_brief":brief,"enable_rag":false
                }).to_string())).unwrap(),
        ).await.unwrap();
        let status = response.status();
        response.into_body().collect().await.unwrap();
        assert_eq!(status, if current_document.is_empty() {StatusCode::OK} else {StatusCode::BAD_GATEWAY});
        let forwarded = rx.recv().await.unwrap();
        assert_eq!(forwarded["creation_brief"], brief);
        assert_eq!(forwarded["root_request"], root_request);
        assert_eq!(forwarded["current_document"], current_document);
        let after = state.storage.with_conn(|conn| {
            creation_brainstorm::get(conn, session_id).map_err(Into::into)
        }).unwrap().unwrap();
        assert_eq!(after.phase, before.phase);
        assert_eq!(after.revision, before.revision);
        assert_eq!(after.state_json, before.state_json);
        assert_eq!(after.updated_at, before.updated_at);
        assert_eq!(after.completed_at, None);
    }
    server.abort();
}
