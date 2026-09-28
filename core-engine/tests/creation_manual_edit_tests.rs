use axum::{
    body::Body,
    http::{Method, Request, StatusCode},
    routing::post,
    Json, Router,
};
use http_body_util::BodyExt;
use memory_bread_core::{
    api::AppState,
    storage::{repo::creation_history, StorageManager},
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

fn hash(content: &str) -> String {
    format!("{:x}", Sha256::digest(content.as_bytes()))
}

fn router(storage: StorageManager) -> Router {
    memory_bread_core::api::create_router(AppState::with_service_urls(
        storage,
        "http://127.0.0.1:1".into(),
        "http://127.0.0.1:1".into(),
        vec![],
    ))
}

async fn send(router: &Router, method: Method, path: &str, body: Value) -> (StatusCode, Value) {
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let json = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| json!({"body": String::from_utf8_lossy(&bytes)}));
    (status, json)
}

fn request(session_id: &str, revision: i64, base: &str, content: &str) -> Value {
    json!({
        "session_id": session_id,
        "base_revision_no": revision,
        "base_document_hash": hash(base),
        "content": content,
    })
}

#[tokio::test]
async fn manual_edit_commits_once_preserves_metadata_and_blocks_stale_legacy_save() {
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("manual-edit.db")).unwrap();
    let id = storage
        .with_conn(|conn| {
            let id = creation_history::insert(
                conn,
                "原始需求",
                "# 原文\n一段文字",
                Some("report"),
                Some("team"),
                2,
                Some("[{\"id\":\"reference\"}]"),
                Some("model"),
                Some(17),
                Some("session-1"),
                Some("[{\"role\":\"user\",\"content\":\"原始需求\"}]"),
                Some("[{\"type\":\"run.completed\"}]"),
                None,
                Some("原始需求"),
                None,
                3,
                "create_document",
                None,
            )?;
            conn.execute(
                "UPDATE creation_history SET evidence_json='[{\"id\":\"e1\"}]',
            creation_brief_json='{\"revision\":2}' WHERE id=?1",
                [id],
            )?;
            Ok(id)
        })
        .unwrap();
    let app = router(storage.clone());
    let base = "# 原文\n一段文字";
    let edited = "# 原文\n用户手动修改了文字";
    let path = format!("/api/creation/history/{id}/document");

    let (status, saved) = send(
        &app,
        Method::PUT,
        &path,
        request("session-1", 3, base, edited),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{saved}");
    assert_eq!(saved["history_id"], id);
    assert_eq!(saved["revision_no"], 4);
    assert_eq!(saved["document_hash"], hash(edited));
    assert_eq!(saved["content"], edited);
    assert_eq!(saved["changed"], true);
    assert_eq!(saved["document_patch"]["operation"], "manual_edit");

    let (status, unchanged) = send(
        &app,
        Method::PUT,
        &path,
        request("session-1", 4, edited, edited),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{unchanged}");
    assert_eq!(unchanged["revision_no"], 4);
    assert_eq!(unchanged["changed"], false);

    let (status, stale) = send(
        &app,
        Method::PUT,
        &path,
        request("session-1", 3, base, "stale write"),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(stale["error"]["code"], "CREATION_BASE_CHANGED");

    let (status, legacy) = send(
        &app,
        Method::POST,
        "/api/creation/history",
        json!({
            "session_id": "session-1", "history_id": id, "prompt": "旧请求",
            "generated_content": "旧页面覆盖", "reference_count": 0,
        }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{legacy}");
    assert_eq!(legacy["body"], "CREATION_BASE_CHANGED");

    let (status, _) = send(
        &app,
        Method::PATCH,
        &format!("/api/creation/history/{id}/progress"),
        json!({
            "lifecycle_status": "running", "generated_content": "late progress",
        }),
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);

    storage
        .with_conn(|conn| {
            let history = creation_history::get_by_id(conn, id)?.unwrap();
            assert_eq!(history.generated_content, edited);
            assert_eq!(history.revision_no, 4);
            assert_eq!(history.edit_operation, "manual_edit");
            assert_eq!(history.prompt, "原始需求");
            assert_eq!(history.reference_count, 2);
            assert_eq!(
                history.conversation_json.as_deref(),
                Some("[{\"role\":\"user\",\"content\":\"原始需求\"}]")
            );
            assert_eq!(history.evidence_json.as_deref(), Some("[{\"id\":\"e1\"}]"));
            assert_eq!(
                history.creation_brief_json.as_deref(),
                Some("{\"revision\":2}")
            );
            assert_eq!(history.lifecycle_status, "completed");
            Ok(())
        })
        .unwrap();

    // A compatibility generation can continue from the edited body only when
    // it names the exact manual-edit base; the earlier stale POST above cannot.
    let (status, fallback) = send(
        &app,
        Method::POST,
        "/api/creation/history",
        json!({
            "session_id": "session-1", "history_id": id,
            "base_revision_no": 4, "base_document_hash": hash(edited),
            "prompt": "继续完善", "generated_content": "# 后续生成\n基于手改正文",
            "reference_count": 0,
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{fallback}");
    storage
        .with_conn(|conn| {
            let history = creation_history::get_by_id(conn, id)?.unwrap();
            assert_eq!(history.generated_content, "# 后续生成\n基于手改正文");
            assert_eq!(history.revision_no, 5);
            Ok(())
        })
        .unwrap();
}

#[tokio::test]
async fn manual_edit_supports_legacy_and_terminal_content_but_rejects_running_or_invalid_body() {
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("manual-edit.db")).unwrap();
    let (legacy_id, cancelled_id, failed_id, running_id) = storage
        .with_conn(|conn| {
            let legacy = creation_history::insert(
                conn,
                "旧历史",
                "旧正文",
                None,
                None,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                "create_document",
                None,
            )?;
            let cancelled = creation_history::insert(
                conn,
                "已取消",
                "保留的正文",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("cancelled-session"),
                None,
                None,
                None,
                None,
                None,
                1,
                "create_document",
                None,
            )?;
            let running = creation_history::insert(
                conn,
                "运行中",
                "预览正文",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("running-session"),
                None,
                None,
                None,
                None,
                None,
                1,
                "create_document",
                None,
            )?;
            let failed = creation_history::insert(
                conn,
                "执行失败",
                "已保留的候选正文",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("failed-session"),
                None,
                None,
                None,
                None,
                None,
                1,
                "create_document",
                None,
            )?;
            creation_history::set_lifecycle_status(conn, cancelled, "cancelled")?;
            creation_history::set_lifecycle_status(conn, failed, "failed")?;
            creation_history::set_lifecycle_status(conn, running, "running")?;
            Ok((legacy, cancelled, failed, running))
        })
        .unwrap();
    let app = router(storage.clone());
    let legacy_path = format!("/api/creation/history/{legacy_id}/document");
    let (status, legacy) = send(
        &app,
        Method::PUT,
        &legacy_path,
        request("", 1, "旧正文", "旧正文可编辑"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{legacy}");
    assert_eq!(legacy["session_id"], "");
    assert_eq!(legacy["revision_no"], 2);
    let (status, wrong_session) = send(
        &app,
        Method::PUT,
        &legacy_path,
        request("history-1", 2, "旧正文可编辑", "非法绑定"),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        wrong_session["error"]["code"],
        "CREATION_DOCUMENT_EDIT_NOT_FOUND"
    );

    let cancelled_path = format!("/api/creation/history/{cancelled_id}/document");
    let (status, cancelled) = send(
        &app,
        Method::PUT,
        &cancelled_path,
        request("cancelled-session", 1, "保留的正文", "改好的正文"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{cancelled}");
    storage
        .with_conn(|conn| {
            assert_eq!(
                creation_history::get_by_id(conn, cancelled_id)?
                    .unwrap()
                    .lifecycle_status,
                "completed"
            );
            Ok(())
        })
        .unwrap();

    let failed_path = format!("/api/creation/history/{failed_id}/document");
    let (status, failed) = send(
        &app,
        Method::PUT,
        &failed_path,
        request("failed-session", 1, "已保留的候选正文", "用户手工修好正文"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{failed}");
    storage
        .with_conn(|conn| {
            assert_eq!(
                creation_history::get_by_id(conn, failed_id)?
                    .unwrap()
                    .lifecycle_status,
                "completed"
            );
            Ok(())
        })
        .unwrap();

    let running_path = format!("/api/creation/history/{running_id}/document");
    let (status, running) = send(
        &app,
        Method::PUT,
        &running_path,
        request("running-session", 1, "预览正文", "不能写入"),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(running["error"]["code"], "CREATION_DOCUMENT_EDIT_NOT_READY");

    let (status, empty) = send(
        &app,
        Method::PUT,
        &legacy_path,
        request("", 2, "旧正文可编辑", "  \n "),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(empty["error"]["code"], "CREATION_DOCUMENT_EMPTY");
    let (status, too_large) = send(
        &app,
        Method::PUT,
        &legacy_path,
        request("", 2, "旧正文可编辑", &"文".repeat(400_000)),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(too_large["error"]["code"], "CREATION_DOCUMENT_TOO_LARGE");
}

#[tokio::test]
async fn manual_edit_requires_latest_session_row_and_matching_hash() {
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("manual-edit.db")).unwrap();
    let (old_id, latest_id) = storage
        .with_conn(|conn| {
            let old_id = creation_history::insert(
                conn,
                "first",
                "first body",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("session-2"),
                None,
                None,
                None,
                None,
                None,
                1,
                "create_document",
                None,
            )?;
            let latest_id = creation_history::insert(
                conn,
                "second",
                "second body",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("session-2"),
                None,
                None,
                None,
                None,
                Some(old_id),
                2,
                "rewrite_document",
                None,
            )?;
            Ok((old_id, latest_id))
        })
        .unwrap();
    let app = router(storage);
    let (status, old) = send(
        &app,
        Method::PUT,
        &format!("/api/creation/history/{old_id}/document"),
        request("session-2", 1, "first body", "must not edit older row"),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(old["error"]["code"], "CREATION_BASE_CHANGED");
    let mut wrong_hash = request("session-2", 2, "second body", "new body");
    wrong_hash["base_document_hash"] = json!(hash("some other body"));
    let (status, stale) = send(
        &app,
        Method::PUT,
        &format!("/api/creation/history/{latest_id}/document"),
        wrong_hash,
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(stale["error"]["code"], "CREATION_BASE_CHANGED");
}

#[tokio::test]
async fn durable_agent_operation_uses_manual_edit_as_its_new_base() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let sidecar = Router::new().route(
        "/creation/agent/run",
        post(|Json(payload): Json<Value>| async move {
            assert_eq!(payload["current_document"], "# 手工正文");
            let event = json!({
                "type": "run.completed", "event_id": "manual-base-complete",
                "session_id": payload["session_id"], "run_id": payload["run_id"],
                "sequence": 1, "status": "completed",
                "data": {
                    "document": "# Agent 后续正文",
                    "edit_intent": {"operation": "revise_document"},
                    "goal": {"status": "complete"}
                }
            });
            format!("data: {event}\n\n")
        }),
    );
    let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("manual-agent.db")).unwrap();
    let id = storage
        .with_conn(|conn| {
            Ok(creation_history::insert(
                conn,
                "写方案",
                "# 初稿",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("manual-agent-session"),
                Some("[]"),
                Some("[]"),
                None,
                Some("写方案"),
                None,
                1,
                "create_document",
                None,
            )?)
        })
        .unwrap();
    let app = memory_bread_core::api::create_router(AppState::with_service_urls(
        storage.clone(),
        url.clone(),
        url,
        vec![],
    ));
    let (status, manual) = send(
        &app,
        Method::PUT,
        &format!("/api/creation/history/{id}/document"),
        request("manual-agent-session", 1, "# 初稿", "# 手工正文"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{manual}");
    assert_eq!(manual["revision_no"], 2);

    let (status, run) = send(
        &app,
        Method::POST,
        "/api/creation/agent/run",
        json!({
            "user_prompt": "继续完善", "session_id": "manual-agent-session",
            "instruction_id": "after-manual", "model_mode": "local",
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{run}");
    assert!(
        run["body"]
            .as_str()
            .unwrap_or_default()
            .contains("committed_operation_id"),
        "{run}"
    );
    storage
        .with_conn(|conn| {
            let history = creation_history::get_by_id(conn, id)?.unwrap();
            assert_eq!(history.generated_content, "# Agent 后续正文");
            assert_eq!(history.revision_no, 3);
            assert_eq!(history.edit_operation, "revise_document");
            Ok(())
        })
        .unwrap();
    server.abort();
}
