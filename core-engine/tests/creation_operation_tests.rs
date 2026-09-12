use axum::{
    body::Body,
    http::{Request, StatusCode},
    routing::post,
    Json, Router,
};
use http_body_util::BodyExt;
use memory_bread_core::{api::AppState, storage::StorageManager};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use tower::ServiceExt;

async fn request(router: &Router, path: &str, payload: Value) -> (StatusCode, String) {
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(payload.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    (
        status,
        String::from_utf8(
            response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .to_vec(),
        )
        .unwrap(),
    )
}

#[tokio::test]
async fn durable_retry_restarts_for_changed_brief_but_resumes_identical_inputs() {
    for changed in [false, true] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = calls.clone();
        let sidecar = Router::new().route("/creation/agent/run", post(move |Json(payload): Json<Value>| {
            let counter = counter.clone();
            async move {
                let call = counter.fetch_add(1, Ordering::SeqCst);
                if call == 1 {
                    assert_eq!(payload["resume_checkpoint"].is_object(), !changed, "{payload}");
                    if changed {
                        assert!(payload["resume_state"].is_null());
                        assert!(payload["model_result"].is_null());
                        assert_eq!(payload["creation_brief"]["revision"], 2);
                    }
                }
                let checkpoint = json!({"session_id":payload["session_id"], "run_id":payload["run_id"],
                    "sequence":1, "cursor":1, "root_request":payload["root_request"],
                    "environment":{"creation_brief":payload["creation_brief"]},
                    "pending_model_step":{"request_id":"model-old"}});
                [json!({"type":"operation.checkpoint", "sequence":1,"data":{"checkpoint":checkpoint}}),
                 json!({"type":"run.paused", "sequence":2,"data":{"reason":"external_model", "continuation":checkpoint}})]
                    .iter().map(|event|format!("data: {}\n\n",event)).collect::<String>()
            }
        }));
        let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
        let tmp = tempfile::tempdir().unwrap();
        let storage = StorageManager::open(&tmp.path().join("retry.db")).unwrap();
        storage.with_conn(|conn| {
            memory_bread_core::storage::repo::creation_history::insert(conn,
                "goal", "existing document", None, None, 0, None, None, None,
                Some("source-retry"), Some("[]"), Some("[]"), None, Some("goal"),
                None, 1, "create_document", None)?;
            Ok(())
        }).unwrap();
        let router = memory_bread_core::api::create_router(AppState::with_service_urls(storage.clone(), url.clone(), url, vec![]));
        let mut payload = json!({"user_prompt":"开始写作", "session_id":"source-retry", "instruction_id":"same-id", "model_mode":"external",
            "creation_brief":{"revision":1,"root_request":"允许检索个人资料"}});
        let (status, first) = request(&router, "/api/creation/agent/run", payload.clone()).await;
        assert_eq!(status, StatusCode::OK, "{first}");
        if changed {
            payload["creation_brief"] = json!({"revision":2,"root_request":"不检索个人资料"});
            payload["model_result"] = json!("late result using old sources");
            payload["model_request_id"] = json!("model-old");
        }
        let (status, second) = request(&router, "/api/creation/agent/run", payload).await;
        assert_eq!(status, StatusCode::OK, "{second}");
        assert!(second.contains("run.paused"), "{second}");
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        let saved: String = storage.with_conn(|conn| Ok(conn.query_row(
            "SELECT checkpoint_json FROM creation_operations WHERE session_id='source-retry'",
            [], |row| row.get(0))?)).unwrap();
        let saved: Value = serde_json::from_str(&saved).unwrap();
        assert_eq!(saved["environment"]["creation_brief"]["revision"], if changed { 2 } else { 1 });
        server.abort();
    }
}

#[tokio::test]
async fn durable_operation_survives_handoff_resume_replay_acknowledgement_and_undo() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let sidecar = Router::new().route("/creation/agent/run",post(move |Json(payload): Json<Value>| {
        let calls = counter.clone();
        async move {
            calls.fetch_add(1,Ordering::SeqCst);
            let session = payload["session_id"].as_str().unwrap();
            let run = payload["run_id"].as_str().unwrap();
            let mut events = vec![];
            if payload["resume_checkpoint"].is_object() {
                assert_eq!(payload["resume_checkpoint"]["pending_model_step"]["request_id"],"model-1");
                events.push(json!({"type":"run.completed","event_id":"completed","session_id":session,"run_id":run,
                    "sequence":12,"status":"completed","data":{"document":"## A\nchanged\n","goal":{"status":"complete"}}}));
            } else if payload["user_prompt"] == "resume" {
                let pending = &payload["operation_context"]["pending_operations"];
                assert_eq!(pending.as_array().unwrap().len(),1);
                assert_eq!(pending[0]["instruction"],"modify");
                events.push(json!({"type":"operation.resume.requested","event_id":"resume","session_id":session,"run_id":run,
                    "sequence":2,"data":{"operation_id":pending[0]["operation_id"]}}));
            } else if payload["user_prompt"] == "undo" {
                let target = &payload["operation_context"]["undo_candidates"][0]["operation_id"];
                assert!(target.is_string());
                events.push(json!({"type":"operation.undo.requested","event_id":"undo","session_id":session,"run_id":run,
                    "sequence":2,"data":{"operation_id":target}}));
            } else {
                let checkpoint = json!({"session_id":session,"run_id":run,"sequence":1,"cursor":1,
                    "pending_model_step":{"request_id":"model-1"}});
                events.push(json!({"type":"operation.checkpoint","event_id":"checkpoint","session_id":session,"run_id":run,
                    "sequence":1,"data":{"checkpoint":checkpoint}}));
                events.push(json!({"type":"model.request","event_id":"model","session_id":session,"run_id":run,
                    "sequence":2,"data":{"request_id":"model-1","messages":[{"role":"user","content":"private model input"}]}}));
                events.push(json!({"type":"run.paused","event_id":"paused","session_id":session,"run_id":run,
                    "sequence":3,"data":{"reason":"external_model","continuation":checkpoint}}));
            }
            events.iter().map(|event|format!("data: {}\n\n",event)).collect::<String>()
        }
    }));
    let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("operations.db")).unwrap();
    // Reproduce an installed database that already applied the first version of 111.
    storage.with_conn(|conn| {
        conn.execute_batch("ALTER TABLE creation_operations DROP COLUMN original_document;
            ALTER TABLE creation_operations DROP COLUMN retry_checkpoint_json;
            DELETE FROM schema_migrations WHERE version='112_creation_operation_recovery_columns';")?;
        Ok(())
    }).unwrap();
    drop(storage);
    let storage = StorageManager::open(&tmp.path().join("operations.db")).unwrap();
    let history_id = storage
        .with_conn(|conn| {
            Ok(memory_bread_core::storage::repo::creation_history::insert(
                conn,
                "goal",
                "## A\nbase\n",
                None,
                None,
                0,
                None,
                None,
                None,
                Some("s"),
                Some("[]"),
                Some("[]"),
                None,
                Some("goal"),
                None,
                1,
                "create_document",
                None,
            )?)
        })
        .unwrap();
    let state = AppState::with_service_urls(storage.clone(), url.clone(), url, vec![]);
    let router = memory_bread_core::api::create_router(state);
    let (status,body) = request(&router,"/api/creation/agent/run",json!({"user_prompt":"modify","session_id":"s","instruction_id":"i1","model_mode":"external"})).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!body.contains("operation.checkpoint"));
    let operation_id = storage
        .with_conn(|conn| {
            Ok(
                memory_bread_core::storage::repo::creation_operation::pending(conn, "s", "")?[0]
                    ["operation_id"]
                    .as_str()
                    .unwrap()
                    .to_string(),
            )
        })
        .unwrap();
    // A browser cannot attach a missing or stale model result to the authoritative checkpoint.
    for request_id in [Value::Null, json!("old-model-request")] {
        let before = calls.load(Ordering::SeqCst);
        let (status, body) = request(
            &router,
            "/api/creation/agent/run",
            json!({"user_prompt":"modify","session_id":"s","instruction_id":"i1",
            "model_request_id":request_id,"model_result":"stale","resume_state":{"cursor":999}}),
        )
        .await;
        assert_eq!(status, StatusCode::CONFLICT, "{body}");
        assert!(body.contains("CREATION_MODEL_RESULT_STALE"));
        assert_eq!(calls.load(Ordering::SeqCst), before);
    }
    let (_, body) = request(
        &router,
        "/api/creation/agent/run",
        json!({"user_prompt":"resume","session_id":"s","instruction_id":"i2"}),
    )
    .await;
    assert!(body.contains("operation.resume.requested"));
    let payload = json!({"user_prompt":"resume","session_id":"s","instruction_id":"i2","resume_operation_id":operation_id});
    let (_, body) = request(&router, "/api/creation/agent/run", payload.clone()).await;
    assert!(body.contains("committed_operation_id"), "{body}");
    // A late browser progress snapshot cannot revert a committed state or document.
    let progress_response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("PATCH")
                .uri(format!("/api/creation/history/{history_id}/progress"))
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"lifecycle_status":"running","generated_content":"stale preview"})
                        .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(progress_response.status(), StatusCode::NO_CONTENT);
    storage
        .with_conn(|conn| {
            let history =
                memory_bread_core::storage::repo::creation_history::get_by_id(conn, history_id)?
                    .unwrap();
            assert_eq!(history.lifecycle_status, "completed");
            assert_eq!(history.generated_content, "## A\nchanged\n");
            Ok(())
        })
        .unwrap();
    let before = calls.load(Ordering::SeqCst);
    let (_, replayed) = request(&router, "/api/creation/agent/run", payload).await;
    assert!(replayed.contains("committed_operation_id"));
    assert_eq!(calls.load(Ordering::SeqCst), before);
    let (status,_) = request(&router,"/api/creation/history",json!({"session_id":"s","committed_operation_id":operation_id,
        "prompt":"resume","generated_content":"stale UI preview must not overwrite","reference_count":0,"conversation":[]})).await;
    assert_eq!(status, StatusCode::OK);
    storage
        .with_conn(|conn| {
            let history =
                memory_bread_core::storage::repo::creation_history::get_by_id(conn, history_id)?
                    .unwrap();
            assert_eq!(history.generated_content, "## A\nchanged\n");
            assert_eq!(history.revision_no, 2);
            assert!(!history
                .agent_trace_json
                .unwrap()
                .contains("private model input"));
            Ok(())
        })
        .unwrap();
    let (_, body) = request(
        &router,
        "/api/creation/agent/run",
        json!({"user_prompt":"undo","session_id":"s","instruction_id":"i3"}),
    )
    .await;
    assert!(body.contains("committed_operation_id"), "{body}");
    storage
        .with_conn(|conn| {
            let history =
                memory_bread_core::storage::repo::creation_history::get_by_id(conn, history_id)?
                    .unwrap();
            assert_eq!(history.generated_content, "## A\nbase\n");
            assert_eq!(history.revision_no, 3);
            Ok(())
        })
        .unwrap();
    server.abort();
}

#[tokio::test]
async fn creation_storage_failure_is_not_reported_as_an_invalid_instruction() {
    let tmp = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&tmp.path().join("broken.db")).unwrap();
    let state = AppState::with_service_urls(storage.clone(), "http://127.0.0.1:1".into(), "http://127.0.0.1:1".into(), vec![]);
    let router = memory_bread_core::api::create_router(state);
    let (status, _) = request(&router, "/api/creation/history/start", json!({
        "prompt":"private instruction", "session_id":"s", "conversation":[]
    })).await;
    assert_eq!(status, StatusCode::OK);
    storage.with_conn(|conn| {
        conn.execute_batch("ALTER TABLE creation_operations DROP COLUMN original_document;")?;
        Ok(())
    }).unwrap();
    let (status, body) = request(&router, "/api/creation/agent/run", json!({
        "user_prompt":"private instruction", "session_id":"s", "instruction_id":"i", "model_mode":"local"
    })).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(body.contains("创作记录写入失败"));
    assert!(!body.contains("original_document"));
    assert!(!body.contains("private instruction"));
}


#[tokio::test]
async fn skill_catalog_and_explicit_selection_reach_sidecar_separately() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let sidecar = Router::new().route("/creation/agent/run", post(|Json(payload): Json<Value>| async move {
        assert_eq!(payload["available_skills"][0]["id"], "skill-1");
        assert_eq!(payload["explicit_skill_ids"], json!(["skill-1"]));
        assert_eq!(payload["selected_skills"], json!([]));
        format!("data: {}\n\n", json!({"type":"run.completed", "event_id":"complete", "session_id":"governance", "run_id":"run-g",
            "sequence":1, "status":"completed", "data":{"document":"# 商家增长方案\n\n业务内容", "goal":{"status":"complete"}}}))
    }));
    let server = tokio::spawn(async move { axum::serve(listener, sidecar).await.unwrap() });
    let dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&dir.path().join("test.db")).unwrap();
    let state = AppState::with_service_urls(storage, url.clone(), url, vec![]);
    let router = memory_bread_core::api::create_router(state);
    let (status, body) = request(&router, "/api/creation/agent/run", json!({"user_prompt":"写商家增长方案", "session_id":"governance", "run_id":"run-g",
        "available_skills":[{"id":"skill-1", "title":"某个技能模板"}], "explicit_skill_ids":["skill-1"], "selected_skills":[]})).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("商家增长方案"));
    server.abort();
}
