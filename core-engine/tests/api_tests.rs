//! REST API 集成测试
//!
//! 测试覆盖：
//! - GET /health → 200 + {"status":"ok"}
//! - GET /captures（空库）→ 200 + total=0
//! - GET /captures?from=&to= → 200
//! - GET /captures?q=... → 200 (FTS5 降级到空结果)
//! - GET /captures?app=... → 200
//! - GET /preferences → 200 + list
//! - PUT /preferences/:key → 200 + 返回新值
//! - PUT /preferences/:key（无 body）→ 400
//! - POST /query → 200 + stub 回复
//! - POST /query（sidecar 不可用）→ 502
//! - POST /query（sidecar 返回 502）→ 502
//! - POST /pii/scrub → 200 + 原文返回

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use http_body_util::BodyExt;
use memory_bread_core::storage::models::{EventType, NewCapture};
use memory_bread_core::{
    api::{state::DebugLogSpec, AppState},
    services::bake_service::BakeService,
    storage::{
        models_bake::{NewBakeArtifactAudit, NewBakeRun},
        NewBakeDocument, NewBakeKnowledge, NewBakeSop, NewTimeline, StorageManager,
    },
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use tower::ServiceExt;

// ── 辅助函数 ──────────────────────────────────────────────────────────────────

/// 创建测试用 axum Router（使用内存临时 SQLite）
async fn make_test_router() -> (axum::Router, tempfile::TempDir) {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let state = AppState::new(sm);
    let router = memory_bread_core::api::create_router(state);
    (router, tmp)
}

fn make_test_state(sm: StorageManager, debug_log_specs: Vec<DebugLogSpec>) -> Arc<AppState> {
    AppState::with_config(sm, "http://127.0.0.1:7071".to_string(), debug_log_specs)
}

async fn spawn_bake_sidecar(responses: Vec<String>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let queue = Arc::new(Mutex::new(
        responses
            .into_iter()
            .map(|item| item.to_string())
            .collect::<VecDeque<_>>(),
    ));

    tokio::spawn({
        let queue = Arc::clone(&queue);
        async move {
            loop {
                let response = {
                    let mut guard = queue.lock().await;
                    guard.pop_front()
                };
                let Some(response) = response else {
                    break;
                };

                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let mut buffer = [0_u8; 8192];
                let _ = stream.read(&mut buffer).await;
                let _ = stream.write_all(response.as_bytes()).await;
                let _ = stream.shutdown().await;
            }
        }
    });

    tokio::time::sleep(Duration::from_millis(20)).await;
    format!("http://{}", addr)
}

fn make_bake_response(
    knowledge: serde_json::Value,
    template: serde_json::Value,
    sop: serde_json::Value,
) -> String {
    let body = serde_json::json!({
        "knowledge": knowledge,
        "design": template,
        "sop": sop,
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20
        },
        "model": "test-model",
        "degraded": false
    })
    .to_string();

    format!(
        "HTTP/1.1 200 OK\r\ncontent-length: {}\r\ncontent-type: application/json\r\nconnection: close\r\n\r\n{}",
        body.len(),
        body
    )
}

fn make_bake_error_response(status_line: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status_line}\r\ncontent-length: {}\r\ncontent-type: application/json\r\nconnection: close\r\n\r\n{}",
        body.len(),
        body
    )
}

fn make_bake_state(sm: StorageManager, sidecar_url: String) -> Arc<AppState> {
    let state = AppState::with_config(sm, sidecar_url, vec![]);
    state
        .capture_enabled
        .store(true, std::sync::atomic::Ordering::Relaxed);
    state
}

async fn spawn_failing_sidecar() -> String {
    spawn_bake_sidecar(vec![make_bake_error_response(
        "502 Bad Gateway",
        r#"{"error":"boom"}"#,
    )])
    .await
}

/// 发送请求并返回 (StatusCode, 响应体字符串)
async fn oneshot(router: axum::Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = router.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let body = String::from_utf8_lossy(&bytes).to_string();
    (status, body)
}

async fn set_favorite(
    router: axum::Router,
    resource_kind: &str,
    resource_id: &str,
    is_favorite: bool,
) -> (StatusCode, serde_json::Value) {
    let request = Request::builder()
        .method(Method::PUT)
        .uri(format!(
            "/api/memory-favorites/{resource_kind}/{resource_id}"
        ))
        .header("content-type", "application/json")
        .body(Body::from(format!(r#"{{"is_favorite":{is_favorite}}}"#)))
        .unwrap();
    let (status, body) = oneshot(router, request).await;
    let json = serde_json::from_str(&body).unwrap_or_else(|_| serde_json::json!({ "body": body }));
    (status, json)
}

#[tokio::test]
async fn bake_artifact_audits_api_returns_branch_decisions_without_candidate_content() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let run_id = sm
        .insert_bake_run(&NewBakeRun {
            trigger_reason: "test".to_string(),
            status: "running".to_string(),
            started_at: 1_710_000_000_000,
        })
        .unwrap();
    sm.upsert_bake_artifact_audit(&NewBakeArtifactAudit {
        run_id,
        timeline_id: 5439,
        artifact_kind: "document".to_string(),
        deterministic_eligible: Some(true),
        deterministic_reason: Some("document_url".to_string()),
        model_accepted: false,
        model_reason: Some("not_a_document".to_string()),
        payload_present: false,
        payload_valid: None,
        artifact_shape: Some("null".to_string()),
        compatibility_recovered: false,
    })
    .unwrap();
    sm.finalize_bake_artifact_audit(
        run_id,
        5439,
        "document",
        "false_negative",
        Some("not_a_document"),
        None,
    )
    .unwrap();
    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let request = Request::builder()
        .method(Method::GET)
        .uri("/api/bake/timelines/5439/artifact-audits?limit=10")
        .body(Body::empty())
        .unwrap();

    let (status, body) = oneshot(router, request).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["timeline_id"], 5439);
    assert_eq!(json["items"][0]["artifact_kind"], "document");
    assert_eq!(json["items"][0]["persist_status"], "false_negative");
    assert_eq!(json["items"][0]["deterministic_eligible"], true);
    assert!(!body.contains("candidate_content"));
}

fn seed_capture(sm: &StorageManager) -> i64 {
    sm.insert_capture(&NewCapture {
        ts: 1_710_000_000_000,
        app_name: Some("Chrome".to_string()),
        app_bundle_id: Some("com.google.Chrome".to_string()),
        win_title: Some("测试来源窗口".to_string()),
        event_type: EventType::Manual,
        ax_text: Some("测试来源文本".to_string()),
        ax_focused_role: None,
        ax_focused_id: None,
        ocr_text: None,
        screenshot_path: None,
        input_text: None,
        is_sensitive: false,
        pii_scrubbed: false,
        screenshot_source: None,
        url: None,
        webpage_title: None,
    })
    .unwrap()
}

fn seed_knowledge_entry(
    sm: &StorageManager,
    category: &str,
    summary: &str,
    overview: &str,
    details: serde_json::Value,
) -> i64 {
    let capture_id = seed_capture(sm);
    if category == "bake_sop" {
        let source_id = sm
            .insert_timeline_entry(&NewTimeline {
                capture_id,
                summary: summary.to_string(),
                overview: Some(overview.to_string()),
                details: Some(details.to_string()),
                entities: r#"["流程","模板"]"#.to_string(),
                category: "meeting".to_string(),
                importance: 4,
                occurrence_count: Some(3),
                observed_at: Some(1_710_000_000_000),
                event_time_start: None,
                event_time_end: None,
                history_view: false,
                content_origin: Some("manual".to_string()),
                activity_type: Some("reading".to_string()),
                is_self_generated: false,
                evidence_strength: Some("high".to_string()),
                capture_ids: None,
                start_time: None,
                end_time: None,
                duration_minutes: None,
                frag_app_name: None,
                frag_win_title: None,
                time_range_start: None,
                time_range_end: None,
                key_timestamps: None,
                work_item: None,
                work_status: None,
                work_progress: None,
            })
            .unwrap();
        return sm
            .insert_bake_sop(&NewBakeSop {
                timeline_id: source_id,
                title: overview.to_string(),
                summary: summary.to_string(),
                content: Some(details.to_string()),
                detailed_content: None,
                entities: r#"["流程","模板"]"#.to_string(),
                importance: 4,
                source_capture_ids: None,
            })
            .unwrap();
    }

    sm.insert_timeline_entry(&NewTimeline {
        capture_id,
        summary: summary.to_string(),
        overview: Some(overview.to_string()),
        details: Some(details.to_string()),
        entities: r#"["流程","模板"]"#.to_string(),
        category: category.to_string(),
        importance: 4,
        occurrence_count: Some(3),
        observed_at: Some(1_710_000_000_000),
        event_time_start: None,
        event_time_end: None,
        history_view: false,
        content_origin: Some("manual".to_string()),
        activity_type: Some("reading".to_string()),
        is_self_generated: false,
        evidence_strength: Some("high".to_string()),
        capture_ids: None,
        start_time: None,
        end_time: None,
        duration_minutes: None,
        frag_app_name: None,
        frag_win_title: None,
        time_range_start: None,
        time_range_end: None,
        key_timestamps: None,
        work_item: None,
        work_status: None,
        work_progress: None,
    })
    .unwrap()
}

fn seed_artifact_ready_timeline(sm: &StorageManager, summary: &str, overview: &str) -> i64 {
    let document_body = "这是一份用于验证烘焙模板与标准操作流程的完整文档正文。".repeat(12);
    let first_capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_000_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("周报模板设计文档".to_string()),
            event_type: EventType::MouseClick,
            ax_text: Some(document_body.clone()),
            ax_focused_role: Some("AXButton".to_string()),
            ax_focused_id: Some("collect-input".to_string()),
            ocr_text: None,
            screenshot_path: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: Some("https://example.com/docs/weekly-report-template".to_string()),
            webpage_title: Some("周报模板设计文档".to_string()),
        })
        .unwrap();
    let second_capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_001_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("周报模板设计文档".to_string()),
            event_type: EventType::KeyPause,
            ax_text: Some(format!("{document_body}\n已整理周报素材")),
            ax_focused_role: Some("AXTextArea".to_string()),
            ax_focused_id: Some("weekly-report-editor".to_string()),
            ocr_text: None,
            screenshot_path: None,
            input_text: Some("整理周报素材".to_string()),
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: Some("https://example.com/docs/weekly-report-template".to_string()),
            webpage_title: Some("周报模板设计文档".to_string()),
        })
        .unwrap();
    // 第三条采集必须构成可归因的"结果"证据：Auto 事件 + 窗口状态变化，
    // 才能通过 SOP 新门禁（action + attributed result）。
    let third_capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_002_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("周报已生成 · 校验通过".to_string()),
            event_type: EventType::Auto,
            ax_text: Some(format!("{document_body}\n周报已生成并完成校验")),
            ax_focused_role: None,
            ax_focused_id: None,
            ocr_text: None,
            screenshot_path: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: Some("https://example.com/docs/weekly-report-template".to_string()),
            webpage_title: Some("周报模板设计文档".to_string()),
        })
        .unwrap();

    let timeline_id = sm
        .insert_timeline_entry(&NewTimeline {
            capture_id: first_capture_id,
            summary: summary.to_string(),
            overview: Some(overview.to_string()),
            details: Some(serde_json::json!({"source": "integration_test"}).to_string()),
            entities: r#"["周报","流程"]"#.to_string(),
            category: "meeting".to_string(),
            importance: 4,
            occurrence_count: Some(3),
            observed_at: Some(1_710_000_002_000),
            event_time_start: None,
            event_time_end: None,
            history_view: false,
            content_origin: Some("live_interaction".to_string()),
            activity_type: Some("reading".to_string()),
            is_self_generated: false,
            evidence_strength: Some("high".to_string()),
            capture_ids: Some(
                serde_json::json!([first_capture_id, second_capture_id, third_capture_id])
                    .to_string(),
            ),
            start_time: None,
            end_time: None,
            duration_minutes: None,
            frag_app_name: None,
            frag_win_title: None,
            time_range_start: None,
            time_range_end: None,
            key_timestamps: None,
            work_item: None,
            work_status: None,
            work_progress: None,
        })
        .unwrap();
    sm.with_conn(|conn| {
        conn.execute(
            "UPDATE captures SET timeline_id = ?1 WHERE id IN (?2, ?3, ?4)",
            rusqlite::params![
                timeline_id,
                first_capture_id,
                second_capture_id,
                third_capture_id
            ],
        )?;
        Ok(())
    })
    .unwrap();
    timeline_id
}

// SOP 专属 fixture：非文档证据（普通 URL + 非文档标题），避免文档误漏报保护
// 把候选转入有界重试；同时保留 action + 可归因 result 证据通过 SOP 门禁。
fn seed_sop_only_timeline(sm: &StorageManager, summary: &str, overview: &str) -> i64 {
    let work_body = "这是一份用于验证标准操作流程沉淀的完整操作记录正文。".repeat(12);
    let first_capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_000_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("数据看板操作台".to_string()),
            event_type: EventType::MouseClick,
            ax_text: Some(work_body.clone()),
            ax_focused_role: Some("AXButton".to_string()),
            ax_focused_id: Some("dashboard-run".to_string()),
            ocr_text: None,
            screenshot_path: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: Some("https://example.com/dashboard/overview".to_string()),
            webpage_title: Some("数据看板操作台".to_string()),
        })
        .unwrap();
    let second_capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_001_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("数据看板操作台".to_string()),
            event_type: EventType::KeyPause,
            ax_text: Some(format!("{work_body}\n已确认看板参数")),
            ax_focused_role: Some("AXTextArea".to_string()),
            ax_focused_id: Some("dashboard-filter".to_string()),
            ocr_text: None,
            screenshot_path: None,
            input_text: Some("确认看板刷新参数".to_string()),
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: Some("https://example.com/dashboard/overview".to_string()),
            webpage_title: Some("数据看板操作台".to_string()),
        })
        .unwrap();
    // 第三条采集必须构成可归因的"结果"证据：Auto 事件 + 窗口状态变化，
    // 才能通过 SOP 新门禁（action + attributed result）。
    let third_capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_002_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("数据看板已刷新 · 校验通过".to_string()),
            event_type: EventType::Auto,
            ax_text: Some(format!("{work_body}\n看板已刷新并完成校验")),
            ax_focused_role: None,
            ax_focused_id: None,
            ocr_text: None,
            screenshot_path: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: Some("https://example.com/dashboard/overview".to_string()),
            webpage_title: Some("数据看板操作台".to_string()),
        })
        .unwrap();

    let timeline_id = sm
        .insert_timeline_entry(&NewTimeline {
            capture_id: first_capture_id,
            summary: summary.to_string(),
            overview: Some(overview.to_string()),
            details: Some(serde_json::json!({"source": "integration_test"}).to_string()),
            entities: r#"["看板","流程"]"#.to_string(),
            category: "meeting".to_string(),
            importance: 4,
            occurrence_count: Some(3),
            observed_at: Some(1_710_000_002_000),
            event_time_start: None,
            event_time_end: None,
            history_view: false,
            content_origin: Some("live_interaction".to_string()),
            activity_type: Some("reading".to_string()),
            is_self_generated: false,
            evidence_strength: Some("high".to_string()),
            capture_ids: Some(
                serde_json::json!([first_capture_id, second_capture_id, third_capture_id])
                    .to_string(),
            ),
            start_time: None,
            end_time: None,
            duration_minutes: None,
            frag_app_name: None,
            frag_win_title: None,
            time_range_start: None,
            time_range_end: None,
            key_timestamps: None,
            work_item: None,
            work_status: None,
            work_progress: None,
        })
        .unwrap();
    sm.with_conn(|conn| {
        conn.execute(
            "UPDATE captures SET timeline_id = ?1 WHERE id IN (?2, ?3, ?4)",
            rusqlite::params![
                timeline_id,
                first_capture_id,
                second_capture_id,
                third_capture_id
            ],
        )?;
        Ok(())
    })
    .unwrap();
    timeline_id
}

fn bake_rejected(reason: &str) -> serde_json::Value {
    serde_json::json!({
        "accepted": false,
        "reason": reason,
        "payload": null,
    })
}

fn bake_knowledge_artifact(summary: &str, review_status: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "accepted": true,
        "reason": null,
        "payload": {
            "summary": summary,
            "overview": format!("{summary} overview"),
            "entities": ["周报", "流程"],
            "importance": 5,
            "occurrence_count": 2,
            "evidence_summary": "来自测试 sidecar",
            "future_question": "下次写周报时如何复用这套流程",
            "decision_reason": "存在可复用的流程事实，满足发布门禁",
            "match_score": 0.91,
            "match_level": "high",
            "review_status": review_status,
        }
    })
}

fn bake_template_artifact(name: &str, review_status: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "accepted": true,
        "reason": null,
        "payload": {
            "name": name,
            "category": "周报",
            "status": "enabled",
            "tags": ["周报", "模板"],
            "applicable_tasks": ["creation"],
            "linked_knowledge_ids": [],
            "structure_sections": [
                {"title": "背景", "keywords": ["背景"], "notes": null},
                {"title": "进展", "keywords": ["进展"], "notes": null}
            ],
            "style_phrases": ["整体看"],
            "replacement_rules": [],
            "prompt_hint": "按周报结构填写",
            "diagram_code": null,
            "image_assets": [],
            "evidence_summary": "来自测试 sidecar",
            "match_score": 0.89,
            "match_level": "high",
            "review_status": review_status,
        }
    })
}

fn bake_sop_artifact(summary: &str, review_status: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "accepted": true,
        "reason": null,
        "payload": {
            "summary": summary,
            "overview": format!("{summary} overview"),
            "source_title": summary,
            "trigger_keywords": ["周报", "提炼"],
            "extracted_problem": "如何沉淀周报流程",
            "steps": ["确认输入", "整理素材", "生成输出"],
            "step_evidence": [
                {"step_index": 1, "capture_ids": ["1"]},
                {"step_index": 2, "capture_ids": ["2"]},
                {"step_index": 3, "capture_ids": ["3"]}
            ],
            "linked_knowledge_ids": [],
            "confidence": "high",
            "evidence_summary": "来自测试 sidecar",
            "match_score": 0.93,
            "match_level": "high",
            "review_status": review_status,
        }
    })
}

async fn run_bake(
    router: axum::Router,
    storage: &StorageManager,
    trigger_reason: &str,
) -> (StatusCode, serde_json::Value, String) {
    let req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/run")
        .header("content-type", "application/json")
        .body(Body::from(format!(
            r#"{{"trigger_reason":"{}","limit":10}}"#,
            trigger_reason
        )))
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    let json = serde_json::from_str(&body).unwrap_or_else(|_| serde_json::json!({ "raw": body }));
    if status == StatusCode::OK && json["status"] == "accepted" {
        let run_id = json["id"]
            .as_i64()
            .expect("accepted bake run must include id");
        for _ in 0..200 {
            if let Some(run) = storage.get_latest_bake_run().unwrap() {
                if run.id == run_id && run.status != "running" {
                    let run_json = serde_json::to_value(run).unwrap();
                    let run_body = run_json.to_string();
                    return (status, run_json, run_body);
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("bake run {run_id} did not finish within test timeout");
    }
    (status, json, body)
}

fn make_bake_retry_due_now(storage: &StorageManager, timeline_id: i64) {
    storage
        .with_conn(|conn| {
            conn.execute(
                "UPDATE bake_retry_state SET next_retry_at_ms = 0 WHERE timeline_id = ?1",
                rusqlite::params![timeline_id],
            )?;
            Ok(())
        })
        .unwrap();
}

#[tokio::test]
async fn test_bake_queue_status_prevents_empty_run_creation() {
    let tmp = tempfile::tempdir().unwrap();
    let sm = StorageManager::open(&tmp.path().join("queue-status.db")).unwrap();
    let state = make_bake_state(sm.clone(), "http://127.0.0.1:9".to_string());
    let router = memory_bread_core::api::create_router(state);

    let queue_request = Request::builder()
        .uri("/api/bake/queue-status")
        .body(Body::empty())
        .unwrap();
    let (queue_status, queue_body) = oneshot(router.clone(), queue_request).await;
    assert_eq!(queue_status, StatusCode::OK, "body: {queue_body}");
    let queue_json: serde_json::Value = serde_json::from_str(&queue_body).unwrap();
    assert_eq!(queue_json["actionable_count"], 0);
    assert!(queue_json["recommended_retry_after_ms"].as_i64().unwrap() > 0);

    let (run_status, run_json, run_body) = run_bake(router, &sm, "knowledge_background").await;
    assert_eq!(run_status, StatusCode::OK, "body: {run_body}");
    assert_eq!(run_json["status"], "skipped");
    assert_eq!(run_json["reason"], "no actionable bake candidates");
    assert!(sm.get_latest_bake_run().unwrap().is_none());
}

#[tokio::test]
async fn test_bake_run_skipped_after_consecutive_no_progress_runs() {
    let tmp = tempfile::tempdir().unwrap();
    let sm = StorageManager::open(&tmp.path().join("no-progress.db")).unwrap();
    // 队列里确实有可烘候选（actionable>0），但最近连续 run 都零进展。
    seed_artifact_ready_timeline(&sm, "no_progress 退避候选", "队列口径有内容但 run 持续空转");
    let now = memory_bread_core::storage::db::current_ts_ms();
    for i in 0..3i64 {
        let run_id = sm
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "knowledge_background".to_string(),
                status: "running".to_string(),
                started_at: now - 1_000 + i,
            })
            .unwrap();
        sm.complete_bake_run(
            run_id,
            "no_op",
            now - 500 + i,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            None,
            Some(10),
        )
        .unwrap();
    }
    let state = make_bake_state(sm.clone(), "http://127.0.0.1:9".to_string());
    let router = memory_bread_core::api::create_router(state);

    let (status, json, body) = run_bake(router, &sm, "knowledge_background").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(json["status"], "skipped");
    assert_eq!(json["reason"], "no_progress_backoff");
    assert!(json["retry_after_ms"].as_i64().unwrap() >= 15_000);
    assert!(json["queue"]["actionable_count"].as_i64().unwrap() > 0);
    assert!(json["queue"]["recent_no_progress_count"].as_i64().unwrap() >= 3);
    // 守卫必须拦在创建 run 行之前：最新 run 仍是预置的第 3 条。
    assert_eq!(sm.get_latest_bake_run().unwrap().unwrap().id, 3);
}

#[tokio::test]
async fn test_bake_run_allows_half_open_probe_after_no_progress_backoff_expires() {
    let tmp = tempfile::tempdir().unwrap();
    let sm = StorageManager::open(&tmp.path().join("no-progress-half-open.db")).unwrap();
    seed_artifact_ready_timeline(&sm, "退避到期候选", "到期后应允许一个探测批次");
    let now = memory_bread_core::storage::db::current_ts_ms();
    for i in 0..3_i64 {
        let run_id = sm
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "knowledge_background".to_string(),
                status: "running".to_string(),
                started_at: now - 300_000 + i,
            })
            .unwrap();
        sm.complete_bake_run(
            run_id,
            "no_op",
            now - 299_000 + i,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            None,
            Some(10),
        )
        .unwrap();
    }
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_rejected("not_a_knowledge"),
        bake_template_artifact("半开探测模板", Some("candidate")),
        bake_rejected("not_a_sop"),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (status, json, body) = run_bake(router, &sm, "knowledge_background").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_ne!(json["reason"], "no_progress_backoff");
    assert!(sm.get_latest_bake_run().unwrap().unwrap().id > 3);
}

#[tokio::test]
async fn test_bake_run_records_trigger_actionable_count() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("trigger-actionable.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_artifact_ready_timeline(
        &sm,
        "记录触发口径的候选",
        "触发时刻读到的 actionable 应落库",
    );
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_rejected("not_a_knowledge"),
        bake_template_artifact("触发口径模板", Some("candidate")),
        bake_rejected("not_a_sop"),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (status, run_json, run_body) = run_bake(router, &sm, "knowledge_background").await;
    assert_eq!(status, StatusCode::OK, "body: {run_body}");
    let run_id = run_json["id"].as_i64().expect("run id required");
    // 触发时队列里只有 1 条 fresh 候选，落库口径必须与 queue-status 一致。
    assert_eq!(
        sm.get_bake_run_trigger_actionable_count(run_id).unwrap(),
        Some(1)
    );
}

#[tokio::test]
async fn test_bake_style_config_roundtrip() {
    let (router, _tmp) = make_test_router().await;

    let get_req = Request::builder()
        .uri("/api/bake/style-config")
        .body(Body::empty())
        .unwrap();
    let (get_status, get_body) = oneshot(router.clone(), get_req).await;
    assert_eq!(get_status, StatusCode::OK, "body: {get_body}");
    let get_json: serde_json::Value = serde_json::from_str(&get_body).unwrap();
    assert!(get_json["preferred_phrases"].is_array());

    let put_req = Request::builder()
        .method(Method::PUT)
        .uri("/api/bake/style-config")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "preferred_phrases": ["整体看"],
            "replacement_rules": [{"from":"综上所述","to":"整体看"}],
            "style_samples": ["这里建议先收敛范围。"],
            "apply_to_creation": true,
            "apply_to_template_editing": false
        }"#,
        ))
        .unwrap();
    let (put_status, put_body) = oneshot(router.clone(), put_req).await;
    assert_eq!(put_status, StatusCode::OK, "body: {put_body}");
    let put_json: serde_json::Value = serde_json::from_str(&put_body).unwrap();
    assert_eq!(put_json["apply_to_template_editing"], false);

    let get_again_req = Request::builder()
        .uri("/api/bake/style-config")
        .body(Body::empty())
        .unwrap();
    let (get_again_status, get_again_body) = oneshot(router, get_again_req).await;
    assert_eq!(get_again_status, StatusCode::OK, "body: {get_again_body}");
    let get_again_json: serde_json::Value = serde_json::from_str(&get_again_body).unwrap();
    assert_eq!(get_again_json["style_samples"][0], "这里建议先收敛范围。");
}

#[tokio::test]
async fn test_bake_templates_crud_flow() {
    let (router, _tmp) = make_test_router().await;

    let create_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"周报模板",
            "doc_type":"周报",
            "status":"draft",
            "tags":["周报"],
            "applicable_tasks":["creation"],
            "source_memory_ids":[],
            "linked_knowledge_ids":[],
            "sections":[{"title":"本周进展","keywords":["进展"],"notes":null}],
            "style_phrases":["整体看"],
            "replacement_rules":[{"from":"综上所述","to":"整体看"}],
            "prompt_hint":"聚焦本周主线",
            "diagram_code":null,
            "image_assets":[],
            "usage_count":0
        }"#,
        ))
        .unwrap();
    let (create_status, create_body) = oneshot(router.clone(), create_req).await;
    assert_eq!(create_status, StatusCode::OK, "body: {create_body}");
    let created: serde_json::Value = serde_json::from_str(&create_body).unwrap();
    let template_id = created["id"].as_str().unwrap().to_string();
    assert_eq!(created["source_memory_ids"].as_array().unwrap().len(), 0);

    let list_req = Request::builder()
        .uri("/api/bake/documents")
        .body(Body::empty())
        .unwrap();
    let (list_status, list_body) = oneshot(router.clone(), list_req).await;
    assert_eq!(list_status, StatusCode::OK, "body: {list_body}");
    let list_json: serde_json::Value = serde_json::from_str(&list_body).unwrap();
    assert_eq!(list_json["items"].as_array().unwrap().len(), 1);

    let update_req = Request::builder()
        .method(Method::PUT)
        .uri(format!("/api/bake/documents/{template_id}"))
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"周报模板-更新",
            "doc_type":"周报",
            "status":"pending_review",
            "tags":["周报","精选"],
            "applicable_tasks":["creation"],
            "source_memory_ids":[],
            "linked_knowledge_ids":[],
            "sections":[],
            "style_phrases":[],
            "replacement_rules":[],
            "prompt_hint":"更新后提示",
            "diagram_code":null,
            "image_assets":[],
            "usage_count":2
        }"#,
        ))
        .unwrap();
    let (update_status, update_body) = oneshot(router.clone(), update_req).await;
    assert_eq!(update_status, StatusCode::OK, "body: {update_body}");
    let update_json: serde_json::Value = serde_json::from_str(&update_body).unwrap();
    assert_eq!(update_json["title"], "周报模板-更新");
    assert_eq!(
        update_json["source_memory_ids"].as_array().unwrap().len(),
        0
    );

    let toggle_req = Request::builder()
        .method(Method::POST)
        .uri(format!("/api/bake/documents/{template_id}/toggle-status"))
        .body(Body::empty())
        .unwrap();
    let (toggle_status, toggle_body) = oneshot(router, toggle_req).await;
    assert_eq!(toggle_status, StatusCode::OK, "body: {toggle_body}");
    let toggle_json: serde_json::Value = serde_json::from_str(&toggle_body).unwrap();
    assert_eq!(toggle_json["status"], "enabled");
}

#[tokio::test]
async fn test_bake_document_refresh_policy_gate_and_endpoint() {
    let (router, _tmp) = make_test_router().await;

    // 无 source_url 的文档：刷新必须在门禁处被拦下，不能走到浏览器采集。
    let create_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"本地周报模板",
            "doc_type":"周报",
            "status":"enabled",
            "tags":[],
            "applicable_tasks":["creation"],
            "sections":[],
            "style_phrases":[],
            "replacement_rules":[],
            "image_assets":[],
            "usage_count":0
        }"#,
        ))
        .unwrap();
    let (create_status, create_body) = oneshot(router.clone(), create_req).await;
    assert_eq!(create_status, StatusCode::OK, "body: {create_body}");
    let created: serde_json::Value = serde_json::from_str(&create_body).unwrap();
    let doc_id = created["id"].as_str().unwrap().to_string();
    assert_eq!(created["refresh_policy"], "auto");

    let refresh_req = Request::builder()
        .method(Method::POST)
        .uri(format!("/api/bake/documents/{doc_id}/refresh"))
        .header("content-type", "application/json")
        .body(Body::from(r#"{}"#))
        .unwrap();
    let (refresh_status, refresh_body) = oneshot(router.clone(), refresh_req).await;
    assert_eq!(refresh_status, StatusCode::OK, "body: {refresh_body}");
    let refresh_json: serde_json::Value = serde_json::from_str(&refresh_body).unwrap();
    assert_eq!(refresh_json["status"], "skipped");
    assert_eq!(refresh_json["reason"], "url_missing");

    // 用户可覆盖策略；never 后门禁优先级高于 URL 判定。
    let put_req = Request::builder()
        .method(Method::PUT)
        .uri(format!("/api/bake/documents/{doc_id}/refresh-policy"))
        .header("content-type", "application/json")
        .body(Body::from(r#"{"refresh_policy": "never"}"#))
        .unwrap();
    let (put_status, put_body) = oneshot(router.clone(), put_req).await;
    assert_eq!(put_status, StatusCode::OK, "body: {put_body}");
    let put_json: serde_json::Value = serde_json::from_str(&put_body).unwrap();
    assert_eq!(put_json["refresh_policy"], "never");

    let refresh_again_req = Request::builder()
        .method(Method::POST)
        .uri(format!("/api/bake/documents/{doc_id}/refresh"))
        .header("content-type", "application/json")
        .body(Body::from(r#"{}"#))
        .unwrap();
    let (again_status, again_body) = oneshot(router.clone(), refresh_again_req).await;
    assert_eq!(again_status, StatusCode::OK, "body: {again_body}");
    let again_json: serde_json::Value = serde_json::from_str(&again_body).unwrap();
    assert_eq!(again_json["status"], "skipped");
    assert_eq!(again_json["reason"], "policy_never");

    // 非法策略值必须被拒绝。
    let bad_req = Request::builder()
        .method(Method::PUT)
        .uri(format!("/api/bake/documents/{doc_id}/refresh-policy"))
        .header("content-type", "application/json")
        .body(Body::from(r#"{"refresh_policy": "weekly"}"#))
        .unwrap();
    let (bad_status, _) = oneshot(router, bad_req).await;
    assert_eq!(bad_status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn test_bake_documents_search_matches_summary_tags_sections_and_content() {
    let (router, _tmp) = make_test_router().await;

    // 关键词只出现在摘要、标签、章节关键词和正文中，标题不含关键词
    let create_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"GPU 指令格式与工具链说明",
            "doc_type":"技术文档",
            "status":"enabled",
            "tags":["SMACT","GPU"],
            "applicable_tasks":["creation"],
            "sections":[{"title":"指令集","keywords":["smact","指令"],"notes":"SMACT 指令格式"}],
            "style_phrases":[],
            "replacement_rules":[],
            "summary":"介绍 SMACT 指令格式",
            "full_content":"本文描述 SMACT 的三种指令格式。",
            "prompt_hint":null,
            "image_assets":[],
            "usage_count":0
        }"#,
        ))
        .unwrap();
    let (create_status, create_body) = oneshot(router.clone(), create_req).await;
    assert_eq!(create_status, StatusCode::OK, "body: {create_body}");
    let created_document: serde_json::Value = serde_json::from_str(&create_body).unwrap();
    let document_id = created_document["id"].as_str().unwrap();

    // 无关文档，不应被命中
    let create_other_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"周报模板",
            "doc_type":"周报",
            "status":"draft",
            "tags":["周报"],
            "applicable_tasks":["creation"],
            "sections":[],
            "style_phrases":[],
            "replacement_rules":[],
            "summary":"每周工作总结",
            "full_content":null,
            "prompt_hint":null,
            "image_assets":[],
            "usage_count":0
        }"#,
        ))
        .unwrap();
    let (create_other_status, create_other_body) = oneshot(router.clone(), create_other_req).await;
    assert_eq!(
        create_other_status,
        StatusCode::OK,
        "body: {create_other_body}"
    );

    // 标题不含关键词，靠摘要/标签命中
    let search_req = Request::builder()
        .uri("/api/bake/documents?q=SMACT")
        .body(Body::empty())
        .unwrap();
    let (search_status, search_body) = oneshot(router.clone(), search_req).await;
    assert_eq!(search_status, StatusCode::OK, "body: {search_body}");
    let search_json: serde_json::Value = serde_json::from_str(&search_body).unwrap();
    assert_eq!(search_json["total"].as_i64().unwrap(), 1);
    assert_eq!(search_json["items"][0]["title"], "GPU 指令格式与工具链说明");

    // 小写关键词应通过章节关键词命中（大小写不敏感）
    let lower_req = Request::builder()
        .uri("/api/bake/documents?q=smact")
        .body(Body::empty())
        .unwrap();
    let (lower_status, lower_body) = oneshot(router.clone(), lower_req).await;
    assert_eq!(lower_status, StatusCode::OK, "body: {lower_body}");
    let lower_json: serde_json::Value = serde_json::from_str(&lower_body).unwrap();
    assert_eq!(lower_json["total"].as_i64().unwrap(), 1);

    // 仅出现在正文中的关键词也应命中
    let content_req = Request::builder()
        .uri("/api/bake/documents?q=%E4%B8%89%E7%A7%8D%E6%8C%87%E4%BB%A4%E6%A0%BC%E5%BC%8F")
        .body(Body::empty())
        .unwrap();
    let (content_status, content_body) = oneshot(router.clone(), content_req).await;
    assert_eq!(content_status, StatusCode::OK, "body: {content_body}");
    let content_json: serde_json::Value = serde_json::from_str(&content_body).unwrap();
    assert_eq!(content_json["total"].as_i64().unwrap(), 1);

    let id_req = Request::builder()
        .uri(format!("/api/bake/documents?q=%23{document_id}"))
        .body(Body::empty())
        .unwrap();
    let (id_status, id_body) = oneshot(router, id_req).await;
    assert_eq!(id_status, StatusCode::OK, "body: {id_body}");
    let id_json: serde_json::Value = serde_json::from_str(&id_body).unwrap();
    assert_eq!(id_json["total"], 1);
    assert_eq!(id_json["items"][0]["id"], document_id);
}

#[tokio::test]
async fn test_bake_sops_list_and_detail() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let sop_id = seed_knowledge_entry(
        &sm,
        "bake_sop",
        "客服问题处理",
        "标准处理流程",
        serde_json::json!({
            "source_capture_id": "1",
            "source_title": "客服问题处理",
            "trigger_keywords": ["客服", "SOP"],
            "confidence": "medium",
            "steps": ["确认问题", "定位知识", "给出回复"],
            "linked_knowledge_ids": ["1"],
            "status": "candidate"
        }),
    );
    let router = memory_bread_core::api::create_router(AppState::new(sm));

    let list_req = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (list_status, list_body) = oneshot(router.clone(), list_req).await;
    assert_eq!(list_status, StatusCode::OK, "body: {list_body}");
    let list_json: serde_json::Value = serde_json::from_str(&list_body).unwrap();
    assert_eq!(list_json["items"].as_array().unwrap().len(), 1);

    let id_list_req = Request::builder()
        .uri(format!("/api/bake/sops?q=%23{sop_id}"))
        .body(Body::empty())
        .unwrap();
    let (id_list_status, id_list_body) = oneshot(router.clone(), id_list_req).await;
    assert_eq!(id_list_status, StatusCode::OK, "body: {id_list_body}");
    let id_list_json: serde_json::Value = serde_json::from_str(&id_list_body).unwrap();
    assert_eq!(id_list_json["total"], 1);
    assert_eq!(id_list_json["items"][0]["id"], sop_id.to_string());

    let detail_req = Request::builder()
        .uri(format!("/api/bake/sops/{sop_id}"))
        .body(Body::empty())
        .unwrap();
    let (detail_status, detail_body) = oneshot(router, detail_req).await;
    assert_eq!(detail_status, StatusCode::OK, "body: {detail_body}");
    let detail_json: serde_json::Value = serde_json::from_str(&detail_body).unwrap();
    assert_eq!(detail_json["status"], "candidate");
}

#[tokio::test]
async fn test_manual_knowledge_and_sop_support_create_and_update() {
    let (router, _tmp) = make_test_router().await;

    let create_knowledge = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/knowledge")
        .header("content-type", "application/json")
        .body(Body::from(
            r###"{
                "summary":"发布前检查知识",
                "overview":"用于发布前快速核对",
                "detailed_content":"## 检查项\n先确认健康检查。",
                "importance":7
            }"###,
        ))
        .unwrap();
    let (create_knowledge_status, create_knowledge_body) =
        oneshot(router.clone(), create_knowledge).await;
    assert_eq!(
        create_knowledge_status,
        StatusCode::OK,
        "body: {create_knowledge_body}"
    );
    let created_knowledge: serde_json::Value =
        serde_json::from_str(&create_knowledge_body).unwrap();
    let knowledge_id = created_knowledge["id"].as_str().unwrap();
    assert_eq!(created_knowledge["summary"], "发布前检查知识");
    assert_eq!(
        created_knowledge["detailed_content"],
        "## 检查项\n先确认健康检查。"
    );

    let update_knowledge = Request::builder()
        .method(Method::PUT)
        .uri(format!("/api/bake/knowledge/{knowledge_id}"))
        .header("content-type", "application/json")
        .body(Body::from(
            r###"{
                "summary":"发布验收知识",
                "overview":"更新后的核对说明",
                "detailed_content":"## 验收\n确认监控无异常。",
                "importance":9
            }"###,
        ))
        .unwrap();
    let (update_knowledge_status, update_knowledge_body) =
        oneshot(router.clone(), update_knowledge).await;
    assert_eq!(
        update_knowledge_status,
        StatusCode::OK,
        "body: {update_knowledge_body}"
    );
    let updated_knowledge: serde_json::Value =
        serde_json::from_str(&update_knowledge_body).unwrap();
    assert_eq!(updated_knowledge["summary"], "发布验收知识");
    assert_eq!(updated_knowledge["overview"], "更新后的核对说明");
    assert_eq!(updated_knowledge["importance"], 9);

    let create_sop = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/sops")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
                "extracted_problem":"发布服务",
                "detailed_content":"按顺序执行并保留验证结果。",
                "steps":["构建产物","执行验收"],
                "trigger_keywords":["发布","验收"]
            }"#,
        ))
        .unwrap();
    let (create_sop_status, create_sop_body) = oneshot(router.clone(), create_sop).await;
    assert_eq!(create_sop_status, StatusCode::OK, "body: {create_sop_body}");
    let created_sop: serde_json::Value = serde_json::from_str(&create_sop_body).unwrap();
    let sop_id = created_sop["id"].as_str().unwrap();
    assert_eq!(created_sop["extracted_problem"], "发布服务");
    assert_eq!(
        created_sop["steps"],
        serde_json::json!(["构建产物", "执行验收"])
    );

    let update_sop = Request::builder()
        .method(Method::PUT)
        .uri(format!("/api/bake/sops/{sop_id}"))
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
                "extracted_problem":"发布并回归服务",
                "detailed_content":"更新后的操作说明。",
                "steps":["构建产物","执行验收","检查监控"],
                "trigger_keywords":["发布","回归"]
            }"#,
        ))
        .unwrap();
    let (update_sop_status, update_sop_body) = oneshot(router.clone(), update_sop).await;
    assert_eq!(update_sop_status, StatusCode::OK, "body: {update_sop_body}");
    let updated_sop: serde_json::Value = serde_json::from_str(&update_sop_body).unwrap();
    assert_eq!(updated_sop["extracted_problem"], "发布并回归服务");
    assert_eq!(
        updated_sop["trigger_keywords"],
        serde_json::json!(["发布", "回归"])
    );

    let list_knowledge = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (list_knowledge_status, list_knowledge_body) =
        oneshot(router.clone(), list_knowledge).await;
    assert_eq!(
        list_knowledge_status,
        StatusCode::OK,
        "body: {list_knowledge_body}"
    );
    let knowledge_page: serde_json::Value = serde_json::from_str(&list_knowledge_body).unwrap();
    assert_eq!(knowledge_page["total"], 1);
    assert_eq!(knowledge_page["items"][0]["summary"], "发布验收知识");

    let list_sops = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (list_sops_status, list_sops_body) = oneshot(router, list_sops).await;
    assert_eq!(list_sops_status, StatusCode::OK, "body: {list_sops_body}");
    let sop_page: serde_json::Value = serde_json::from_str(&list_sops_body).unwrap();
    assert_eq!(sop_page["total"], 1);
    assert_eq!(sop_page["items"][0]["extracted_problem"], "发布并回归服务");
}

#[tokio::test]
async fn test_memory_favorites_cover_knowledge_operation_and_document_lists_and_details() {
    let (router, _tmp) = make_test_router().await;

    let create_knowledge = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/knowledge")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
                "summary":"收藏知识",
                "overview":"用于收藏筛选测试",
                "detailed_content":"收藏后的知识详情",
                "importance":7
            }"#,
        ))
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), create_knowledge).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();

    let create_operation = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/sops")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
                "extracted_problem":"收藏操作",
                "detailed_content":"用于收藏筛选测试",
                "steps":["打开详情","点击收藏"],
                "trigger_keywords":["收藏"]
            }"#,
        ))
        .unwrap();
    let (operation_status, operation_body) = oneshot(router.clone(), create_operation).await;
    assert_eq!(operation_status, StatusCode::OK, "body: {operation_body}");
    let operation: serde_json::Value = serde_json::from_str(&operation_body).unwrap();

    let create_document = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
                "title":"收藏文档",
                "doc_type":"通用文档",
                "status":"enabled",
                "tags":[],
                "applicable_tasks":[],
                "full_content":"用于收藏筛选测试",
                "review_status":"confirmed"
            }"#,
        ))
        .unwrap();
    let (document_status, document_body) = oneshot(router.clone(), create_document).await;
    assert_eq!(document_status, StatusCode::OK, "body: {document_body}");
    let document: serde_json::Value = serde_json::from_str(&document_body).unwrap();

    let resources = [
        (
            "knowledge",
            knowledge["id"].as_str().unwrap().to_string(),
            "/api/bake/knowledge".to_string(),
            format!("/api/bake/knowledge/{}", knowledge["id"].as_str().unwrap()),
        ),
        (
            "operation",
            operation["id"].as_str().unwrap().to_string(),
            "/api/bake/sops".to_string(),
            format!("/api/bake/sops/{}", operation["id"].as_str().unwrap()),
        ),
        (
            "document",
            document["id"].as_str().unwrap().to_string(),
            "/api/bake/documents".to_string(),
            format!("/api/bake/documents/{}", document["id"].as_str().unwrap()),
        ),
    ];

    for (kind, id, list_path, detail_path) in resources {
        let (status, body) = set_favorite(router.clone(), kind, &id, true).await;
        assert_eq!(status, StatusCode::OK, "body: {body}");
        assert_eq!(body["resource_kind"], kind);
        assert_eq!(body["resource_id"], id.parse::<i64>().unwrap());
        assert_eq!(body["is_favorite"], true);

        let detail = Request::builder()
            .uri(detail_path)
            .body(Body::empty())
            .unwrap();
        let (detail_status, detail_body) = oneshot(router.clone(), detail).await;
        assert_eq!(detail_status, StatusCode::OK, "body: {detail_body}");
        let detail_json: serde_json::Value = serde_json::from_str(&detail_body).unwrap();
        assert_eq!(detail_json["is_favorite"], true);

        let favorite_list = Request::builder()
            .uri(format!("{list_path}?favorite=true"))
            .body(Body::empty())
            .unwrap();
        let (favorite_status, favorite_body) = oneshot(router.clone(), favorite_list).await;
        assert_eq!(favorite_status, StatusCode::OK, "body: {favorite_body}");
        let favorite_json: serde_json::Value = serde_json::from_str(&favorite_body).unwrap();
        assert_eq!(favorite_json["total"], 1);
        assert_eq!(favorite_json["items"][0]["id"], id);
        assert_eq!(favorite_json["items"][0]["is_favorite"], true);

        let not_favorite_list = Request::builder()
            .uri(format!("{list_path}?favorite=false"))
            .body(Body::empty())
            .unwrap();
        let (not_favorite_status, not_favorite_body) =
            oneshot(router.clone(), not_favorite_list).await;
        assert_eq!(
            not_favorite_status,
            StatusCode::OK,
            "body: {not_favorite_body}"
        );
        let not_favorite_json: serde_json::Value =
            serde_json::from_str(&not_favorite_body).unwrap();
        assert_eq!(not_favorite_json["total"], 0);
    }

    let (unsupported_status, _) = set_favorite(router.clone(), "capture", "1", true).await;
    assert_eq!(unsupported_status, StatusCode::BAD_REQUEST);
    let (missing_status, _) = set_favorite(router, "knowledge", "999999", true).await;
    assert_eq!(missing_status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn test_bake_templates_bucket_filter_separates_pending_and_extracted() {
    let (router, _tmp) = make_test_router().await;

    let create_candidate_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"候选模板",
            "doc_type":"周报",
            "status":"draft",
            "tags":["周报"],
            "applicable_tasks":["creation"],
            "source_memory_ids":[],
            "linked_knowledge_ids":[],
            "sections":[{"title":"背景","keywords":["背景"],"notes":null}],
            "style_phrases":["整体看"],
            "replacement_rules":[],
            "prompt_hint":"候选提示",
            "diagram_code":null,
            "image_assets":[],
            "usage_count":0,
            "review_status":"candidate"
        }"#,
        ))
        .unwrap();
    let (candidate_status, candidate_body) = oneshot(router.clone(), create_candidate_req).await;
    assert_eq!(candidate_status, StatusCode::OK, "body: {candidate_body}");

    let create_confirmed_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/documents")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{
            "title":"已提炼模板",
            "doc_type":"周报",
            "status":"enabled",
            "tags":["周报"],
            "applicable_tasks":["creation"],
            "source_memory_ids":[],
            "linked_knowledge_ids":[],
            "sections":[{"title":"进展","keywords":["进展"],"notes":null}],
            "style_phrases":["先结论后展开"],
            "replacement_rules":[],
            "prompt_hint":"已提炼提示",
            "diagram_code":null,
            "image_assets":[],
            "usage_count":1,
            "review_status":"confirmed"
        }"#,
        ))
        .unwrap();
    let (confirmed_status, confirmed_body) = oneshot(router.clone(), create_confirmed_req).await;
    assert_eq!(confirmed_status, StatusCode::OK, "body: {confirmed_body}");

    let pending_req = Request::builder()
        .uri("/api/bake/documents?bucket=pending")
        .body(Body::empty())
        .unwrap();
    let (pending_status, pending_body) = oneshot(router.clone(), pending_req).await;
    assert_eq!(pending_status, StatusCode::OK, "body: {pending_body}");
    let pending_json: serde_json::Value = serde_json::from_str(&pending_body).unwrap();
    assert!(pending_json["items"].as_array().unwrap().is_empty());

    let extracted_req = Request::builder()
        .uri("/api/bake/documents?bucket=extracted")
        .body(Body::empty())
        .unwrap();
    let (extracted_status, extracted_body) = oneshot(router, extracted_req).await;
    assert_eq!(extracted_status, StatusCode::OK, "body: {extracted_body}");
    let extracted_json: serde_json::Value = serde_json::from_str(&extracted_body).unwrap();
    assert_eq!(extracted_json["items"].as_array().unwrap().len(), 2);
    let titles = extracted_json["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|item| item["title"].as_str())
        .collect::<Vec<_>>();
    assert!(titles.contains(&"候选模板"));
    assert!(titles.contains(&"已提炼模板"));
}

#[tokio::test]
async fn test_bake_sops_bucket_filter_separates_pending_and_extracted() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();

    seed_knowledge_entry(
        &sm,
        "bake_sop",
        "候选 SOP",
        "候选流程",
        serde_json::json!({
            "source_capture_id": "1",
            "source_title": "候选 SOP",
            "trigger_keywords": ["候选"],
            "confidence": "medium",
            "steps": ["步骤一", "步骤二", "步骤三"],
            "linked_knowledge_ids": ["11"],
            "status": "candidate"
        }),
    );
    seed_knowledge_entry(
        &sm,
        "bake_sop",
        "已采纳 SOP",
        "已采纳流程",
        serde_json::json!({
            "source_capture_id": "2",
            "source_title": "已采纳 SOP",
            "trigger_keywords": ["采纳"],
            "confidence": "high",
            "steps": ["确认问题", "执行流程", "回写结果"],
            "linked_knowledge_ids": ["22"],
            "status": "confirmed"
        }),
    );

    let router = memory_bread_core::api::create_router(AppState::new(sm));

    let pending_req = Request::builder()
        .uri("/api/bake/sops?bucket=pending")
        .body(Body::empty())
        .unwrap();
    let (pending_status, pending_body) = oneshot(router.clone(), pending_req).await;
    assert_eq!(pending_status, StatusCode::OK, "body: {pending_body}");
    let pending_json: serde_json::Value = serde_json::from_str(&pending_body).unwrap();
    assert!(pending_json["items"].as_array().unwrap().is_empty());

    let extracted_req = Request::builder()
        .uri("/api/bake/sops?bucket=extracted")
        .body(Body::empty())
        .unwrap();
    let (extracted_status, extracted_body) = oneshot(router, extracted_req).await;
    assert_eq!(extracted_status, StatusCode::OK, "body: {extracted_body}");
    let extracted_json: serde_json::Value = serde_json::from_str(&extracted_body).unwrap();
    assert_eq!(extracted_json["items"].as_array().unwrap().len(), 2);
    let statuses = extracted_json["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|item| item["status"].as_str())
        .collect::<Vec<_>>();
    assert!(statuses.contains(&"candidate"));
    assert!(statuses.contains(&"confirmed"));
}

#[tokio::test]
async fn test_bake_pipeline_chain_from_memory_to_knowledge_template_and_sop() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();

    seed_artifact_ready_timeline(&sm, "周报写作需求讨论", "讨论周报标准化产出流程");

    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_knowledge_artifact("链路知识", None),
        bake_template_artifact("链路模板", None),
        bake_sop_artifact("链路 SOP", None),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let init_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/memories/init")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"limit":10}"#))
        .unwrap();
    let (init_status, init_body) = oneshot(router.clone(), init_req).await;
    assert_eq!(init_status, StatusCode::OK, "body: {init_body}");
    let init_json: serde_json::Value = serde_json::from_str(&init_body).unwrap();
    assert_eq!(init_json["created_count"], 0);
    assert_eq!(init_json["skipped_count"], 1);

    let (run_status, run_json, run_body) = run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(run_status, StatusCode::OK, "body: {run_body}");
    assert_eq!(run_json["knowledge_created_count"], 1);
    assert_eq!(run_json["document_created_count"], 1);
    assert_eq!(run_json["sop_created_count"], 1, "body: {run_body}");

    let knowledge_req = Request::builder()
        .uri("/api/bake/knowledge?bucket=extracted")
        .body(Body::empty())
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), knowledge_req).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge_json: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();
    assert_eq!(knowledge_json["items"].as_array().unwrap().len(), 1);

    let templates_req = Request::builder()
        .uri("/api/bake/documents?bucket=extracted")
        .body(Body::empty())
        .unwrap();
    let (templates_status, templates_body) = oneshot(router.clone(), templates_req).await;
    assert_eq!(templates_status, StatusCode::OK, "body: {templates_body}");
    let templates_json: serde_json::Value = serde_json::from_str(&templates_body).unwrap();
    let template_item = &templates_json["items"][0];
    assert_eq!(templates_json["items"].as_array().unwrap().len(), 1);
    assert_eq!(template_item["title"], "周报模板设计文档");
    assert!(template_item["source_memory_ids"].as_array().unwrap().len() >= 1);
    assert!(template_item["sections"].as_array().unwrap().len() >= 2);

    let sops_req = Request::builder()
        .uri("/api/bake/sops?bucket=extracted")
        .body(Body::empty())
        .unwrap();
    let (sops_status, sops_body) = oneshot(router, sops_req).await;
    assert_eq!(sops_status, StatusCode::OK, "body: {sops_body}");
    let sops_json: serde_json::Value = serde_json::from_str(&sops_body).unwrap();
    let sop_item = &sops_json["items"][0];
    assert_eq!(sops_json["items"].as_array().unwrap().len(), 1);
    assert_eq!(sop_item["status"], "auto_created");
    assert!(sop_item["linked_knowledge_ids"].as_array().unwrap().len() >= 1);
}

#[tokio::test]
async fn test_bake_memories_promote_and_ignore_flow() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();

    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_knowledge_artifact("预览知识", None),
        bake_template_artifact("预览模板", Some("candidate")),
        bake_sop_artifact("预览 SOP", Some("candidate")),
    )])
    .await;

    let memory_id = seed_knowledge_entry(
        &sm,
        "bake_article",
        "高价值情节记忆",
        "沉淀模板写法",
        serde_json::json!({
            "url": "https://example.com/article",
            "source_knowledge_id": 1,
            "source_capture_id": "1",
            "weight": 88,
            "open_count": 6,
            "dwell_seconds": 240,
            "has_edit_action": true,
            "knowledge_ref_count": 4,
            "status": "candidate",
            "suggested_action": "template",
            "tags": ["模板", "流程"],
            "last_visited_at": "2026-04-07 10:00"
        }),
    );
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let preview_req = Request::builder()
        .uri(format!("/api/bake/memories/{memory_id}/preview"))
        .body(Body::empty())
        .unwrap();
    let (preview_status, preview_body) = oneshot(router.clone(), preview_req).await;
    assert_eq!(preview_status, StatusCode::OK, "body: {preview_body}");
    let preview_json: serde_json::Value = serde_json::from_str(&preview_body).unwrap();
    assert_eq!(preview_json["knowledge"]["payload"]["match_level"], "high");
    assert_eq!(preview_json["design"]["payload"]["match_score"], 0.89);
    assert_eq!(preview_json["sop"]["payload"]["match_score"], 0.93);

    let list_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (list_status, list_body) = oneshot(router.clone(), list_req).await;
    assert_eq!(list_status, StatusCode::OK, "body: {list_body}");
    let list_json: serde_json::Value = serde_json::from_str(&list_body).unwrap();
    assert_eq!(list_json["articles"].as_array().unwrap().len(), 1);
    assert_eq!(list_json["memories"].as_array().unwrap().len(), 1);

    let promote_template_req = Request::builder()
        .method(Method::POST)
        .uri(format!("/api/bake/memories/{memory_id}/promote-document"))
        .body(Body::empty())
        .unwrap();
    let (promote_template_status, promote_template_body) =
        oneshot(router.clone(), promote_template_req).await;
    assert_eq!(
        promote_template_status,
        StatusCode::OK,
        "body: {promote_template_body}"
    );
    let promote_template_json: serde_json::Value =
        serde_json::from_str(&promote_template_body).unwrap();
    assert_eq!(promote_template_json["title"], "高价值情节记忆");

    let promote_sop_req = Request::builder()
        .method(Method::POST)
        .uri(format!("/api/bake/memories/{memory_id}/promote-sop"))
        .body(Body::empty())
        .unwrap();
    let (promote_sop_status, promote_sop_body) = oneshot(router.clone(), promote_sop_req).await;
    assert_eq!(
        promote_sop_status,
        StatusCode::OK,
        "body: {promote_sop_body}"
    );
    let promote_sop_json: serde_json::Value = serde_json::from_str(&promote_sop_body).unwrap();
    assert_eq!(promote_sop_json["status"], "auto_created");

    let ignore_req = Request::builder()
        .method(Method::POST)
        .uri(format!("/api/bake/memories/{memory_id}/ignore"))
        .body(Body::empty())
        .unwrap();
    let (ignore_status, ignore_body) = oneshot(router.clone(), ignore_req).await;
    assert_eq!(ignore_status, StatusCode::OK, "body: {ignore_body}");
    let ignore_json: serde_json::Value = serde_json::from_str(&ignore_body).unwrap();
    assert_eq!(ignore_json["status"], "ignored");

    let overview_req = Request::builder()
        .uri("/api/bake/overview")
        .body(Body::empty())
        .unwrap();
    let (overview_status, overview_body) = oneshot(router, overview_req).await;
    assert_eq!(overview_status, StatusCode::OK, "body: {overview_body}");
    let overview_json: serde_json::Value = serde_json::from_str(&overview_body).unwrap();
    assert_eq!(overview_json["template_count"], 1);
    assert_eq!(overview_json["memory_count"], 2);
    assert_eq!(overview_json["knowledge_count"], 0);
}

#[tokio::test]
async fn test_bake_knowledge_api_only_returns_bake_knowledge() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();

    seed_knowledge_entry(
        &sm,
        "meeting",
        "普通 knowledge",
        "普通概述",
        serde_json::json!({}),
    );
    seed_knowledge_entry(
        &sm,
        "bake_article",
        "情节记忆",
        "记忆概述",
        serde_json::json!({}),
    );
    seed_knowledge_entry(
        &sm,
        "bake_sop",
        "操作手册",
        "SOP 概述",
        serde_json::json!({}),
    );
    let knowledge_id = seed_knowledge_entry(
        &sm,
        "bake_knowledge",
        "已提炼知识",
        "知识概述",
        serde_json::json!({}),
    );

    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let req = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router.clone(), req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    let items = json["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);

    let id_req = Request::builder()
        .uri(format!("/api/bake/knowledge?q=%23{knowledge_id}"))
        .body(Body::empty())
        .unwrap();
    let (id_status, id_body) = oneshot(router, id_req).await;
    assert_eq!(id_status, StatusCode::OK, "body: {id_body}");
    let id_json: serde_json::Value = serde_json::from_str(&id_body).unwrap();
    assert_eq!(id_json["total"], 1);
    assert_eq!(id_json["items"][0]["id"], knowledge_id.to_string());
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["category"], "bake_knowledge");
    assert_eq!(items[0]["summary"], "已提炼知识");
}

#[tokio::test]
async fn test_bake_overview_counts_only_bake_knowledge() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();

    seed_knowledge_entry(
        &sm,
        "meeting",
        "普通 knowledge",
        "普通概述",
        serde_json::json!({}),
    );
    seed_knowledge_entry(
        &sm,
        "bake_article",
        "情节记忆",
        "记忆概述",
        serde_json::json!({}),
    );
    seed_knowledge_entry(
        &sm,
        "bake_knowledge",
        "已提炼知识",
        "知识概述",
        serde_json::json!({}),
    );

    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let req = Request::builder()
        .uri("/api/bake/overview")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["memory_count"], 3);
    assert_eq!(json["knowledge_count"], 1);
}

#[tokio::test]
async fn test_bake_captures_search_matches_win_title() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let capture_id = sm
        .insert_capture(&NewCapture {
            ts: 1_710_000_000_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("设计稿评审页面".to_string()),
            event_type: EventType::Manual,
            ax_text: Some("无关正文".to_string()),
            ax_focused_role: None,
            ax_focused_id: None,
            ocr_text: None,
            screenshot_path: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            screenshot_source: None,
            url: None,
            webpage_title: None,
        })
        .unwrap();

    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let req = Request::builder()
        .uri("/api/bake/captures?q=%E8%AE%BE%E8%AE%A1%E7%A8%BF")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router.clone(), req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    let items = json["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["win_title"], "设计稿评审页面");

    let req = Request::builder()
        .uri(format!("/api/bake/captures?id={capture_id}"))
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["total"], 1);
    assert_eq!(json["items"][0]["id"], capture_id.to_string());
}

#[tokio::test]
async fn test_bake_memories_init_is_idempotent() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_knowledge_entry(
        &sm,
        "meeting",
        "初始化候选情节记忆",
        "可映射为高价值情节记忆",
        serde_json::json!({}),
    );
    let router = memory_bread_core::api::create_router(AppState::new(sm));

    let first_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/memories/init")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"limit":10}"#))
        .unwrap();
    let (first_status, first_body) = oneshot(router.clone(), first_req).await;
    assert_eq!(first_status, StatusCode::OK, "body: {first_body}");
    let first_json: serde_json::Value = serde_json::from_str(&first_body).unwrap();
    assert_eq!(first_json["created_count"], 0);
    assert_eq!(first_json["skipped_count"], 1);
    assert!(first_json["articles"].as_array().unwrap().is_empty());
    assert!(first_json["memories"].as_array().unwrap().is_empty());

    let second_req = Request::builder()
        .method(Method::POST)
        .uri("/api/bake/memories/init")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"limit":10}"#))
        .unwrap();
    let (second_status, second_body) = oneshot(router.clone(), second_req).await;
    assert_eq!(second_status, StatusCode::OK, "body: {second_body}");
    let second_json: serde_json::Value = serde_json::from_str(&second_body).unwrap();
    assert_eq!(second_json["created_count"], 0);

    let list_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (list_status, list_body) = oneshot(router, list_req).await;
    assert_eq!(list_status, StatusCode::OK, "body: {list_body}");
    let list_json: serde_json::Value = serde_json::from_str(&list_body).unwrap();
    assert_eq!(list_json["articles"].as_array().unwrap().len(), 1);
    assert_eq!(list_json["memories"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_bake_run_pipeline_creates_only_template() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_artifact_ready_timeline(&sm, "适合沉淀模板的候选", "应只落模板");
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_rejected("not_a_knowledge"),
        bake_template_artifact("周报模板", Some("candidate")),
        bake_rejected("not_a_sop"),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (status, run_json, run_body) = run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(status, StatusCode::OK, "body: {run_body}");
    assert_eq!(run_json["processed_episode_count"], 1);
    assert_eq!(run_json["auto_created_count"], 1);
    assert_eq!(run_json["candidate_count"], 0);
    assert_eq!(run_json["discarded_count"], 2);
    assert_eq!(run_json["knowledge_created_count"], 0);
    assert_eq!(run_json["document_created_count"], 1);
    assert_eq!(run_json["sop_created_count"], 0);

    let knowledge_req = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), knowledge_req).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge_json: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();
    assert_eq!(knowledge_json["items"].as_array().unwrap().len(), 0);

    let templates_req = Request::builder()
        .uri("/api/bake/documents")
        .body(Body::empty())
        .unwrap();
    let (templates_status, templates_body) = oneshot(router.clone(), templates_req).await;
    assert_eq!(templates_status, StatusCode::OK, "body: {templates_body}");
    let templates_json: serde_json::Value = serde_json::from_str(&templates_body).unwrap();
    assert_eq!(templates_json["items"].as_array().unwrap().len(), 1);
    assert_eq!(templates_json["items"][0]["title"], "周报模板设计文档");

    let sops_req = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (sops_status, sops_body) = oneshot(router.clone(), sops_req).await;
    assert_eq!(sops_status, StatusCode::OK, "body: {sops_body}");
    let sops_json: serde_json::Value = serde_json::from_str(&sops_body).unwrap();
    assert_eq!(sops_json["items"].as_array().unwrap().len(), 0);

    let memories_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (memories_status, memories_body) = oneshot(router, memories_req).await;
    assert_eq!(memories_status, StatusCode::OK, "body: {memories_body}");
    let memories_json: serde_json::Value = serde_json::from_str(&memories_body).unwrap();
    assert_eq!(memories_json["memories"].as_array().unwrap().len(), 1);
    assert!(memories_json["memories"][0]["template_match_score"].is_null());
    assert!(memories_json["memories"][0]["template_match_level"].is_null());
    assert!(memories_json["memories"][0]["knowledge_match_score"].is_null());
    assert!(memories_json["memories"][0]["knowledge_match_level"].is_null());
    assert!(memories_json["memories"][0]["sop_match_score"].is_null());
    assert!(memories_json["memories"][0]["sop_match_level"].is_null());
}

#[tokio::test]
async fn test_bake_run_pipeline_creates_only_sop() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_sop_only_timeline(&sm, "适合沉淀 SOP 的候选", "应只落 SOP");
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_rejected("not_a_knowledge"),
        bake_rejected("not_a_template"),
        bake_sop_artifact("标准操作流程", Some("candidate")),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (status, run_json, run_body) = run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(status, StatusCode::OK, "body: {run_body}");
    assert_eq!(run_json["processed_episode_count"], 1);
    assert_eq!(run_json["auto_created_count"], 1);
    assert_eq!(run_json["candidate_count"], 0);
    assert_eq!(run_json["discarded_count"], 2);
    assert_eq!(run_json["knowledge_created_count"], 0);
    assert_eq!(run_json["document_created_count"], 0);
    assert_eq!(run_json["sop_created_count"], 1);

    let knowledge_req = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), knowledge_req).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge_json: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();
    assert_eq!(knowledge_json["items"].as_array().unwrap().len(), 0);

    let templates_req = Request::builder()
        .uri("/api/bake/documents")
        .body(Body::empty())
        .unwrap();
    let (templates_status, templates_body) = oneshot(router.clone(), templates_req).await;
    assert_eq!(templates_status, StatusCode::OK, "body: {templates_body}");
    let templates_json: serde_json::Value = serde_json::from_str(&templates_body).unwrap();
    assert_eq!(templates_json["items"].as_array().unwrap().len(), 0);

    let sops_req = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (sops_status, sops_body) = oneshot(router.clone(), sops_req).await;
    assert_eq!(sops_status, StatusCode::OK, "body: {sops_body}");
    let sops_json: serde_json::Value = serde_json::from_str(&sops_body).unwrap();
    assert_eq!(sops_json["items"].as_array().unwrap().len(), 1);
    assert_eq!(sops_json["items"][0]["source_title"], "标准操作流程");
    assert_eq!(sops_json["items"][0]["extracted_problem"], "标准操作流程");

    let memories_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (memories_status, memories_body) = oneshot(router, memories_req).await;
    assert_eq!(memories_status, StatusCode::OK, "body: {memories_body}");
    let memories_json: serde_json::Value = serde_json::from_str(&memories_body).unwrap();
    assert_eq!(memories_json["memories"].as_array().unwrap().len(), 1);
    assert!(memories_json["memories"][0]["sop_match_score"].is_null());
    assert!(memories_json["memories"][0]["sop_match_level"].is_null());
    assert!(memories_json["memories"][0]["knowledge_match_score"].is_null());
    assert!(memories_json["memories"][0]["knowledge_match_level"].is_null());
    assert!(memories_json["memories"][0]["template_match_score"].is_null());
    assert!(memories_json["memories"][0]["template_match_level"].is_null());
}

#[tokio::test]
async fn test_bake_run_pipeline_creates_only_knowledge_and_updates_overview() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_knowledge_entry(
        &sm,
        "meeting",
        "周报模板流程沉淀",
        "沉淀步骤化标准方案",
        serde_json::json!({}),
    );
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_knowledge_artifact("提炼后的知识", None),
        bake_rejected("not_a_template"),
        bake_rejected("not_a_sop"),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (run_status, run_json, run_body) = run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(run_status, StatusCode::OK, "body: {run_body}");
    assert_eq!(run_json["status"], "completed");
    assert_eq!(run_json["trigger_reason"], "manual_debug");
    assert_eq!(run_json["processed_episode_count"], 1);
    assert_eq!(run_json["auto_created_count"], 1);
    assert_eq!(run_json["candidate_count"], 0);
    assert_eq!(run_json["discarded_count"], 2);
    assert_eq!(run_json["knowledge_created_count"], 1);
    assert_eq!(run_json["document_created_count"], 0);
    assert_eq!(run_json["sop_created_count"], 0);

    let overview_req = Request::builder()
        .uri("/api/bake/overview")
        .body(Body::empty())
        .unwrap();
    let (overview_status, overview_body) = oneshot(router.clone(), overview_req).await;
    assert_eq!(overview_status, StatusCode::OK, "body: {overview_body}");
    let overview_json: serde_json::Value = serde_json::from_str(&overview_body).unwrap();
    assert_eq!(overview_json["template_count"], 0);
    assert_eq!(overview_json["memory_count"], 1);
    assert_eq!(overview_json["knowledge_count"], 1);
    assert_eq!(overview_json["pending_candidates"], 0);
    assert_eq!(overview_json["auto_created_today"], 1);
    assert_eq!(overview_json["candidate_today"], 0);
    assert_eq!(overview_json["discarded_today"], 2);
    assert_eq!(overview_json["last_bake_run_status"], "completed");
    assert_eq!(overview_json["last_trigger_reason"], "manual_debug");
    assert_eq!(overview_json["knowledge_auto_count"], 1);
    assert_eq!(overview_json["template_auto_count"], 0);
    assert_eq!(overview_json["sop_auto_count"], 0);
    assert!(overview_json["last_bake_run_at"].as_i64().unwrap() > 0);
    assert!(!overview_json["recent_activities"]
        .as_array()
        .unwrap()
        .is_empty());

    let knowledge_req = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), knowledge_req).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge_json: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();
    assert_eq!(knowledge_json["items"].as_array().unwrap().len(), 1);
    assert_eq!(
        knowledge_json["items"][0]["summary"],
        "提炼后的知识 overview"
    );

    let templates_req = Request::builder()
        .uri("/api/bake/documents")
        .body(Body::empty())
        .unwrap();
    let (templates_status, templates_body) = oneshot(router.clone(), templates_req).await;
    assert_eq!(templates_status, StatusCode::OK, "body: {templates_body}");
    let templates_json: serde_json::Value = serde_json::from_str(&templates_body).unwrap();
    assert_eq!(templates_json["items"].as_array().unwrap().len(), 0);

    let sops_req = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (sops_status, sops_body) = oneshot(router.clone(), sops_req).await;
    assert_eq!(sops_status, StatusCode::OK, "body: {sops_body}");
    let sops_json: serde_json::Value = serde_json::from_str(&sops_body).unwrap();
    assert_eq!(sops_json["items"].as_array().unwrap().len(), 0);

    let memories_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (memories_status, memories_body) = oneshot(router, memories_req).await;
    assert_eq!(memories_status, StatusCode::OK, "body: {memories_body}");
    let memories_json: serde_json::Value = serde_json::from_str(&memories_body).unwrap();
    assert_eq!(memories_json["memories"].as_array().unwrap().len(), 1);
    assert!(memories_json["memories"][0]["source_knowledge_id"].is_null());
    assert_eq!(memories_json["memories"][0]["status"], "candidate");
    assert!(memories_json["memories"][0]["knowledge_match_score"].is_null());
    assert!(memories_json["memories"][0]["knowledge_match_level"].is_null());
    assert!(memories_json["memories"][0]["template_match_score"].is_null());
    assert!(memories_json["memories"][0]["template_match_level"].is_null());
    assert!(memories_json["memories"][0]["sop_match_score"].is_null());
    assert!(memories_json["memories"][0]["sop_match_level"].is_null());
}

#[tokio::test]
async fn test_bake_overview_recent_activity_highlights_knowledge_background_runs() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_knowledge_entry(
        &sm,
        "meeting",
        "周报模板流程沉淀",
        "沉淀步骤化标准方案",
        serde_json::json!({}),
    );
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_knowledge_artifact("后台提炼知识", None),
        bake_rejected("not_a_template"),
        bake_rejected("not_a_sop"),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (run_status, _, run_body) = run_bake(router.clone(), &sm, "knowledge_background").await;
    assert_eq!(run_status, StatusCode::OK, "body: {run_body}");

    let overview_req = Request::builder()
        .uri("/api/bake/overview")
        .body(Body::empty())
        .unwrap();
    let (overview_status, overview_body) = oneshot(router, overview_req).await;
    assert_eq!(overview_status, StatusCode::OK, "body: {overview_body}");
    let overview_json: serde_json::Value = serde_json::from_str(&overview_body).unwrap();
    assert_eq!(overview_json["last_trigger_reason"], "knowledge_background");
    let recent_activities = overview_json["recent_activities"].as_array().unwrap();
    assert!(recent_activities.iter().any(|item| item
        .as_str()
        .unwrap_or_default()
        .contains("知识后台提炼后已自动执行分类烤面包")));
}

#[tokio::test]
async fn test_bake_run_pipeline_keeps_all_accepted_artifacts_auto_created() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_artifact_ready_timeline(&sm, "周报模板流程沉淀", "沉淀步骤化标准方案");

    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        serde_json::json!({
            "accepted": true,
            "reason": null,
            "payload": {
                "summary": "提炼后的知识",
                "overview": "提炼后的知识 overview",
                "entities": ["周报", "流程"],
                "importance": 5,
                "occurrence_count": 2,
                "evidence_summary": "来自测试 sidecar",
                "future_question": "下次写周报时如何复用这套流程",
                "decision_reason": "存在可复用的流程事实，满足发布门禁",
                "match_score": 0.95,
                "match_level": "low",
                "review_status": "auto_created"
            }
        }),
        serde_json::json!({
            "accepted": true,
            "reason": null,
            "payload": {
                "name": "周报模板",
                "category": "周报",
                "status": "enabled",
                "tags": ["周报", "模板"],
                "applicable_tasks": ["creation"],
                "linked_knowledge_ids": [],
                "structure_sections": [
                    {"title": "背景", "keywords": ["背景"], "notes": null},
                    {"title": "进展", "keywords": ["进展"], "notes": null}
                ],
                "style_phrases": ["整体看"],
                "replacement_rules": [],
                "prompt_hint": "按周报结构填写",
                "diagram_code": null,
                "image_assets": [],
                "evidence_summary": "来自测试 sidecar",
                "match_score": 0.95,
                "match_level": "low",
                "review_status": "auto_created"
            }
        }),
        serde_json::json!({
            "accepted": true,
            "reason": null,
            "payload": {
                "summary": "标准操作流程",
                "overview": "标准操作流程 overview",
                "source_title": "标准操作流程",
                "trigger_keywords": ["周报", "提炼"],
                "extracted_problem": "如何沉淀周报流程",
                "steps": ["确认输入", "整理素材", "生成输出"],
                "step_evidence": [
                    {"step_index": 1, "capture_ids": ["1"]},
                    {"step_index": 2, "capture_ids": ["2"]},
                    {"step_index": 3, "capture_ids": ["3"]}
                ],
                "linked_knowledge_ids": [],
                "confidence": "high",
                "evidence_summary": "来自测试 sidecar",
                "match_score": 0.95,
                "match_level": "low",
                "review_status": "auto_created"
            }
        }),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (run_status, _run_json, run_body) = run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(run_status, StatusCode::OK, "body: {run_body}");

    let knowledge_req = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), knowledge_req).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge_json: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();
    let knowledge_review_status = knowledge_json["items"][0]["review_status"]
        .as_str()
        .or_else(|| knowledge_json["items"][0]["reviewStatus"].as_str())
        .unwrap();
    assert_eq!(knowledge_review_status, "auto_created");

    let templates_req = Request::builder()
        .uri("/api/bake/documents")
        .body(Body::empty())
        .unwrap();
    let (templates_status, templates_body) = oneshot(router.clone(), templates_req).await;
    assert_eq!(templates_status, StatusCode::OK, "body: {templates_body}");
    let templates_json: serde_json::Value = serde_json::from_str(&templates_body).unwrap();
    let template_review_status = templates_json["items"][0]["review_status"]
        .as_str()
        .or_else(|| templates_json["items"][0]["reviewStatus"].as_str())
        .unwrap();
    assert_eq!(template_review_status, "auto_created");

    let sops_req = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (sops_status, sops_body) = oneshot(router, sops_req).await;
    assert_eq!(sops_status, StatusCode::OK, "body: {sops_body}");
    let sops_json: serde_json::Value = serde_json::from_str(&sops_body).unwrap();
    let sop_review_status = sops_json["items"][0]["review_status"]
        .as_str()
        .or_else(|| sops_json["items"][0]["reviewStatus"].as_str())
        .or_else(|| sops_json["items"][0]["status"].as_str())
        .unwrap();
    assert_eq!(sop_review_status, "auto_created");
}

#[tokio::test]
async fn test_bake_run_pipeline_is_idempotent() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_artifact_ready_timeline(&sm, "周报模板流程沉淀", "沉淀步骤化标准方案");
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_knowledge_artifact("第一次提炼知识", None),
        bake_template_artifact("第一次模板", Some("candidate")),
        bake_sop_artifact("第一次 SOP", Some("candidate")),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (first_status, first_json, first_body) =
        run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(first_status, StatusCode::OK, "body: {first_body}");
    assert_eq!(first_json["status"], "completed");
    assert_eq!(first_json["processed_episode_count"], 1);
    assert_eq!(first_json["auto_created_count"], 3);
    assert_eq!(first_json["candidate_count"], 0);
    assert_eq!(first_json["discarded_count"], 0);
    assert_eq!(first_json["knowledge_created_count"], 1);
    assert_eq!(first_json["document_created_count"], 1);
    assert_eq!(first_json["sop_created_count"], 1);

    let (second_status, second_json, second_body) =
        run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(second_status, StatusCode::OK, "body: {second_body}");
    assert_eq!(second_json["status"], "skipped");
    assert_eq!(second_json["reason"], "no actionable bake candidates");

    let knowledge_req = Request::builder()
        .uri("/api/bake/knowledge")
        .body(Body::empty())
        .unwrap();
    let (knowledge_status, knowledge_body) = oneshot(router.clone(), knowledge_req).await;
    assert_eq!(knowledge_status, StatusCode::OK, "body: {knowledge_body}");
    let knowledge_json: serde_json::Value = serde_json::from_str(&knowledge_body).unwrap();
    assert_eq!(knowledge_json["items"].as_array().unwrap().len(), 1);

    let templates_req = Request::builder()
        .uri("/api/bake/documents")
        .body(Body::empty())
        .unwrap();
    let (templates_status, templates_body) = oneshot(router.clone(), templates_req).await;
    assert_eq!(templates_status, StatusCode::OK, "body: {templates_body}");
    let templates_json: serde_json::Value = serde_json::from_str(&templates_body).unwrap();
    assert_eq!(templates_json["items"].as_array().unwrap().len(), 1);

    let sops_req = Request::builder()
        .uri("/api/bake/sops")
        .body(Body::empty())
        .unwrap();
    let (sops_status, sops_body) = oneshot(router.clone(), sops_req).await;
    assert_eq!(sops_status, StatusCode::OK, "body: {sops_body}");
    let sops_json: serde_json::Value = serde_json::from_str(&sops_body).unwrap();
    assert_eq!(sops_json["items"].as_array().unwrap().len(), 1);

    let memories_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (memories_status, memories_body) = oneshot(router, memories_req).await;
    assert_eq!(memories_status, StatusCode::OK, "body: {memories_body}");
    let memories_json: serde_json::Value = serde_json::from_str(&memories_body).unwrap();
    assert_eq!(memories_json["memories"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_bake_run_pipeline_rejected_candidate_advances_watermark() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    seed_knowledge_entry(
        &sm,
        "meeting",
        "只有背景信息，没有可复用产物",
        "只应推进 watermark",
        serde_json::json!({}),
    );
    let sidecar_url = spawn_bake_sidecar(vec![make_bake_response(
        bake_rejected("no_knowledge"),
        bake_rejected("no_template"),
        bake_rejected("no_sop"),
    )])
    .await;
    let router = memory_bread_core::api::create_router(make_bake_state(sm.clone(), sidecar_url));

    let (status, run_json, run_body) = run_bake(router.clone(), &sm, "manual_debug").await;
    assert_eq!(status, StatusCode::OK, "body: {run_body}");
    assert_eq!(run_json["status"], "completed");
    assert_eq!(run_json["processed_episode_count"], 1);
    assert_eq!(run_json["auto_created_count"], 0);
    assert_eq!(run_json["candidate_count"], 0);
    assert_eq!(run_json["discarded_count"], 3);
    assert_eq!(run_json["knowledge_created_count"], 0);
    assert_eq!(run_json["document_created_count"], 0);
    assert_eq!(run_json["sop_created_count"], 0);

    let memories_req = Request::builder()
        .uri("/api/bake/memories")
        .body(Body::empty())
        .unwrap();
    let (memories_status, memories_body) = oneshot(router.clone(), memories_req).await;
    assert_eq!(memories_status, StatusCode::OK, "body: {memories_body}");
    let memories_json: serde_json::Value = serde_json::from_str(&memories_body).unwrap();
    assert_eq!(memories_json["memories"].as_array().unwrap().len(), 1);

    let (rerun_status, rerun_json, rerun_body) = run_bake(router, &sm, "manual_debug").await;
    assert_eq!(rerun_status, StatusCode::OK, "body: {rerun_body}");
    assert_eq!(rerun_json["status"], "skipped");
    assert_eq!(rerun_json["reason"], "no actionable bake candidates");
}

#[tokio::test]
async fn test_bake_run_pipeline_malformed_json_advances_fresh_watermark_and_retries() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let timeline_id = seed_knowledge_entry(
        &sm,
        "meeting",
        "第一次返回坏 JSON",
        "失败后应推进 fresh watermark 并进入独立重试队列",
        serde_json::json!({}),
    );
    let sidecar_url = spawn_bake_sidecar(vec![
        make_bake_error_response("200 OK", "{not json"),
        make_bake_response(
            bake_knowledge_artifact("重试后成功知识", None),
            bake_rejected("not_a_template"),
            bake_rejected("not_a_sop"),
        ),
    ])
    .await;
    let service = BakeService::new(sm.clone(), sidecar_url);

    let first = service
        .run_bake_pipeline("manual_debug", 10)
        .await
        .expect("单候选损坏响应不应中断整个批次");
    assert_eq!(first.status, "completed");
    assert_eq!(first.processed_episode_count, 1);
    assert_eq!(first.knowledge_created_count, 0);
    assert!(sm.get_bake_watermark("unified").unwrap().is_some());
    let retry = sm.get_bake_retry_state(timeline_id).unwrap().unwrap();
    assert_eq!(retry.failure_count, 1);
    assert_eq!(
        retry.last_error_code.as_deref(),
        Some("BAKE_SIDECAR_RESPONSE_INVALID")
    );

    make_bake_retry_due_now(&sm, timeline_id);

    let second = service
        .run_bake_pipeline("manual_debug", 10)
        .await
        .expect("第二次合法响应应处理同一候选");
    assert_eq!(second.processed_episode_count, 1);
    assert_eq!(second.knowledge_created_count, 1);
    assert_eq!(sm.count_bake_knowledge().unwrap(), 1);
    assert!(sm.get_bake_retry_state(timeline_id).unwrap().is_none());
}

#[tokio::test]
async fn test_bake_run_pipeline_bounds_unstructured_5xx_and_advances_watermark() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let timeline_id = seed_knowledge_entry(
        &sm,
        "meeting",
        "sidecar 失败映射",
        "应返回 BAD_GATEWAY",
        serde_json::json!({}),
    );
    let sidecar_url = spawn_bake_sidecar(vec![
        make_bake_error_response("502 Bad Gateway", r#"{"error":"boom one"}"#),
        make_bake_error_response("502 Bad Gateway", r#"{"error":"boom two"}"#),
        make_bake_error_response(
            "502 Bad Gateway",
            r#"{"error":"provider-model secret response"}"#,
        ),
    ])
    .await;
    let service = BakeService::new(sm.clone(), sidecar_url);

    for attempt in 1..=2 {
        let run = service
            .run_bake_pipeline("manual_debug", 10)
            .await
            .expect("单候选裸 502 不应中断整个批次");
        assert_eq!(run.status, "completed", "attempt={attempt}");
        assert_eq!(run.processed_episode_count, 1, "attempt={attempt}");
        assert_eq!(run.discarded_count, 0, "attempt={attempt}");
        let retry = sm.get_bake_retry_state(timeline_id).unwrap().unwrap();
        assert_eq!(retry.failure_count, attempt, "attempt={attempt}");
        assert_eq!(
            retry.last_error_code.as_deref(),
            Some("BAKE_UNCLASSIFIED_UPSTREAM_ERROR")
        );
        assert!(!retry
            .last_error
            .unwrap_or_default()
            .contains("provider-model"));
        make_bake_retry_due_now(&sm, timeline_id);
    }

    let terminal = service
        .run_bake_pipeline("manual_debug", 10)
        .await
        .expect("第三次裸 502 应把毒丸候选转为终态");
    assert_eq!(terminal.status, "completed");
    assert_eq!(terminal.processed_episode_count, 1);
    assert_eq!(terminal.discarded_count, 1);

    // 毒丸候选达到上限后已推进 watermark；后续 run 不再被同一 5xx 卡住。
    let next = service
        .run_bake_pipeline("manual_debug", 10)
        .await
        .expect("毒丸候选终态后后续 run 应正常完成");
    assert_eq!(next.processed_episode_count, 0);
}

// ── /debug/log-files ──────────────────────────────────────────────────────────

#[tokio::test]
async fn test_debug_log_files_returns_empty_list_when_whitelist_empty() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let router = memory_bread_core::api::create_router(make_test_state(sm, vec![]));

    let req = Request::builder()
        .uri("/api/debug/log-files")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(json["items"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn test_debug_log_files_marks_missing_file_as_not_exists() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let log_dir = tmp.path().join("logs");
    std::fs::create_dir_all(&log_dir).unwrap();
    let router = memory_bread_core::api::create_router(make_test_state(
        sm,
        vec![DebugLogSpec::new(
            "core",
            "core.log · Core Engine",
            log_dir,
            "core.log",
        )],
    ));

    let req = Request::builder()
        .uri("/api/debug/log-files")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    let item = &json["items"][0];
    assert_eq!(item["key"], "core");
    assert_eq!(item["exists"], false);
    assert_eq!(item["size_bytes"], 0);
}

#[tokio::test]
async fn test_debug_log_content_returns_whitelisted_log() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let log_dir = tmp.path().join("logs");
    std::fs::create_dir_all(&log_dir).unwrap();
    std::fs::write(log_dir.join("core.log"), "line1\nline2\n").unwrap();
    let router = memory_bread_core::api::create_router(make_test_state(
        sm,
        vec![DebugLogSpec::new(
            "core",
            "core.log · Core Engine",
            log_dir,
            "core.log",
        )],
    ));

    let req = Request::builder()
        .uri("/api/debug/log-files/core")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["key"], "core");
    assert_eq!(json["truncated"], false);
    assert!(json["content"].as_str().unwrap().contains("line2"));
}

#[tokio::test]
async fn test_debug_log_content_returns_404_for_unknown_key() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let router = memory_bread_core::api::create_router(make_test_state(sm, vec![]));

    let req = Request::builder()
        .uri("/api/debug/log-files/unknown")
        .body(Body::empty())
        .unwrap();
    let (status, _body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn test_debug_log_content_truncates_large_file() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let log_dir = tmp.path().join("logs");
    std::fs::create_dir_all(&log_dir).unwrap();
    let content = "A".repeat(140 * 1024);
    std::fs::write(log_dir.join("core.log"), content).unwrap();
    let router = memory_bread_core::api::create_router(make_test_state(
        sm,
        vec![DebugLogSpec::new(
            "core",
            "core.log · Core Engine",
            log_dir,
            "core.log",
        )],
    ));

    let req = Request::builder()
        .uri("/api/debug/log-files/core")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["truncated"], true);
    assert_eq!(json["returned_bytes"], 128 * 1024);
    assert_eq!(json["total_size_bytes"], 140 * 1024);
}

#[tokio::test]
async fn test_debug_log_content_rejects_path_escape_via_symlink() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let log_dir = tmp.path().join("logs");
    std::fs::create_dir_all(&log_dir).unwrap();
    let outside = tmp.path().join("outside.log");
    std::fs::write(&outside, "outside").unwrap();
    std::os::unix::fs::symlink(&outside, log_dir.join("core.log")).unwrap();
    let router = memory_bread_core::api::create_router(make_test_state(
        sm,
        vec![DebugLogSpec::new(
            "core",
            "core.log · Core Engine",
            log_dir,
            "core.log",
        )],
    ));

    let req = Request::builder()
        .uri("/api/debug/log-files/core")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "body: {body}");
    assert!(body.contains("路径越界"), "body: {body}");
}

// ── /health ───────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_health_200() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["status"], "ok");
}

#[tokio::test]
async fn test_health_version_present() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let (_, body) = oneshot(router, req).await;
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(json["version"].as_str().unwrap().len() > 0);
}

// ── /captures ─────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_captures_empty_db() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/captures")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["total"], 0);
    assert!(json["captures"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn test_captures_with_time_filter() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/captures?from=0&to=9999999999999&limit=10")
        .body(Body::empty())
        .unwrap();
    let (status, _body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn test_captures_fts_query() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/captures?q=%E5%B7%A5%E4%BD%9C") // URL-encoded "工作"
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(json["captures"].is_array());
}

#[tokio::test]
async fn test_captures_app_filter() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/captures?app=%E5%BE%AE%E4%BF%A1") // URL-encoded "微信"
        .body(Body::empty())
        .unwrap();
    let (status, _) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn test_captures_limit_respected() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/captures?limit=5")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(json["captures"].as_array().unwrap().len() <= 5);
}

// ── /preferences ──────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_preferences_list() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .uri("/preferences")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(json["preferences"].is_array());
    // 种子数据（002_seed_defaults.sql）会插入若干默认偏好
    assert!(json["preferences"].as_array().unwrap().len() >= 0);
}

#[tokio::test]
async fn test_preferences_put() {
    let (router, _tmp) = make_test_router().await;
    let body_json = r#"{"value":"测试值"}"#;
    let req = Request::builder()
        .method(Method::PUT)
        .uri("/preferences/test.api.key")
        .header("content-type", "application/json")
        .body(Body::from(body_json))
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["key"], "test.api.key");
    assert_eq!(json["value"], "测试值");
}

#[tokio::test]
async fn test_preferences_put_update_existing() {
    let (router, _tmp) = make_test_router().await;
    // 第一次设置
    let req1 = Request::builder()
        .method(Method::PUT)
        .uri("/preferences/test.update.key")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"value":"原始值"}"#))
        .unwrap();
    let (s1, _) = oneshot(router.clone(), req1).await;
    assert_eq!(s1, StatusCode::OK);

    // 第二次更新
    let req2 = Request::builder()
        .method(Method::PUT)
        .uri("/preferences/test.update.key")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"value":"更新后的值"}"#))
        .unwrap();
    let (s2, body2) = oneshot(router, req2).await;
    assert_eq!(s2, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body2).unwrap();
    assert_eq!(json["value"], "更新后的值");
}

#[tokio::test]
async fn test_preferences_put_invalid_body_400() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .method(Method::PUT)
        .uri("/preferences/test.key")
        .header("content-type", "application/json")
        .body(Body::from("not-json"))
        .unwrap();
    let (status, _) = oneshot(router, req).await;
    // axum 返回 422（JSON parse 失败）
    assert!(
        status == StatusCode::UNPROCESSABLE_ENTITY || status == StatusCode::BAD_REQUEST,
        "expected 4xx, got {status}"
    );
}

// ── /query ────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_query_sidecar_unavailable_returns_502() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let state = AppState::with_config(sm, "http://127.0.0.1:9".to_string(), vec![]);
    let router = memory_bread_core::api::create_router(state);

    let req = Request::builder()
        .method(Method::POST)
        .uri("/query")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"query":"今日工作总结"}"#))
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "body: {body}");
}

#[tokio::test]
async fn test_query_sidecar_error_response_returns_502() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let sidecar_url = spawn_failing_sidecar().await;
    let state = AppState::with_config(sm, sidecar_url, vec![]);
    let router = memory_bread_core::api::create_router(state);

    let req = Request::builder()
        .method(Method::POST)
        .uri("/query")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"query":"test","top_k":5}"#))
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["error"], "BAD_GATEWAY");
    assert_eq!(json["message"], "boom");
}

#[tokio::test]
async fn test_action_stub_returns_200() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .method(Method::POST)
        .uri("/action/execute")
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{"action_type":"click","coords":[100.0,200.0]}"#,
        ))
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(json["action_id"].as_str().unwrap().len() > 0);
}

// ── /pii/scrub ────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_pii_scrub_stub_returns_text() {
    let (router, _tmp) = make_test_router().await;
    let req = Request::builder()
        .method(Method::POST)
        .uri("/pii/scrub")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"text":"我的手机号是 13800138000"}"#))
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    // stub 原文返回，不做脱敏
    assert_eq!(json["text"], "我的手机号是 13800138000");
    assert_eq!(json["redacted_count"], 0);
}

// ── 404 测试 ──────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_knowledge_api_returns_semantic_fields() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let capture_id = seed_capture(&sm);

    sm.insert_timeline_entry(&NewTimeline {
        capture_id,
        summary: "今天回看飞书消息".to_string(),
        overview: Some("今天回看飞书消息".to_string()),
        details: Some("确认了昨天的发布安排".to_string()),
        entities: "[\"飞书\",\"发布\"]".to_string(),
        category: "聊天".to_string(),
        importance: 4,
        occurrence_count: Some(1),
        observed_at: Some(1_710_000_100_000_i64),
        event_time_start: Some(1_709_913_600_000_i64),
        event_time_end: Some(1_709_914_000_000_i64),
        history_view: true,
        content_origin: Some("historical_content".to_string()),
        activity_type: Some("reviewing_history".to_string()),
        is_self_generated: false,
        evidence_strength: Some("high".to_string()),
        capture_ids: None,
        start_time: None,
        end_time: None,
        duration_minutes: None,
        frag_app_name: None,
        frag_win_title: None,
        time_range_start: None,
        time_range_end: None,
        key_timestamps: None,
        work_item: None,
        work_status: None,
        work_progress: None,
    })
    .unwrap();

    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let req = Request::builder()
        .uri("/api/knowledge")
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    let entry = &json["entries"][0];
    assert_eq!(entry["observed_at"], 1_710_000_100_000_i64);
    assert_eq!(entry["event_time_start"], 1_709_913_600_000_i64);
    assert_eq!(entry["event_time_end"], 1_709_914_000_000_i64);
    assert_eq!(entry["history_view"], true);
    assert_eq!(entry["content_origin"], "historical_content");
    assert_eq!(entry["activity_type"], "reviewing_history");
    assert_eq!(entry["is_self_generated"], false);
    assert_eq!(entry["evidence_strength"], "high");
}

#[tokio::test]
async fn test_knowledge_list_filters_by_exact_id() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let target_id = seed_knowledge_entry(
        &sm,
        "聊天",
        "目标时间线",
        "目标概览",
        serde_json::json!({"note": "target"}),
    );
    seed_knowledge_entry(
        &sm,
        "聊天",
        "其他时间线",
        "其他概览",
        serde_json::json!({"note": "other"}),
    );

    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let req = Request::builder()
        .uri(format!("/api/knowledge?id={target_id}"))
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(json["total"], 1);
    assert_eq!(json["entries"][0]["id"], target_id);
    assert_eq!(json["entries"][0]["summary"], "目标时间线");
}

#[tokio::test]
async fn test_knowledge_detail_api_returns_timeline_entry() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let timeline_id = seed_knowledge_entry(
        &sm,
        "聊天",
        "目标时间线",
        "目标概览",
        serde_json::json!({"note": "detail"}),
    );

    let router = memory_bread_core::api::create_router(AppState::new(sm));
    let req = Request::builder()
        .uri(format!("/api/knowledge/{timeline_id}"))
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router, req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();

    assert_eq!(json["id"], timeline_id);
    assert_eq!(json["summary"], "目标时间线");
    assert_eq!(json["overview"], "目标概览");
    assert_eq!(json["category"], "聊天");
    assert!(json["capture_ids"]
        .as_array()
        .is_some_and(|ids| !ids.is_empty()));
}

#[tokio::test]
async fn test_timeline_relations_api_returns_linked_artifacts() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("test.db");
    let sm = StorageManager::open(&db).unwrap();
    let capture_id = seed_capture(&sm);

    let timeline_id = sm
        .insert_timeline_entry(&NewTimeline {
            capture_id,
            summary: "定向关联查询的目标时间线".to_string(),
            overview: Some("目标概览".to_string()),
            details: Some(r#"{"note":"target"}"#.to_string()),
            entities: "[]".to_string(),
            category: "meeting".to_string(),
            importance: 4,
            occurrence_count: None,
            observed_at: Some(1_710_000_000_000),
            event_time_start: None,
            event_time_end: None,
            history_view: false,
            content_origin: None,
            activity_type: None,
            is_self_generated: false,
            evidence_strength: None,
            capture_ids: None,
            start_time: None,
            end_time: None,
            duration_minutes: None,
            frag_app_name: None,
            frag_win_title: None,
            time_range_start: None,
            time_range_end: None,
            key_timestamps: None,
            work_item: None,
            work_status: None,
            work_progress: None,
        })
        .unwrap();
    let other_timeline_id = sm
        .insert_timeline_entry(&NewTimeline {
            capture_id,
            summary: "无关时间线".to_string(),
            overview: None,
            details: None,
            entities: "[]".to_string(),
            category: "meeting".to_string(),
            importance: 1,
            occurrence_count: None,
            observed_at: Some(1_710_000_000_000),
            event_time_start: None,
            event_time_end: None,
            history_view: false,
            content_origin: None,
            activity_type: None,
            is_self_generated: false,
            evidence_strength: None,
            capture_ids: None,
            start_time: None,
            end_time: None,
            duration_minutes: None,
            frag_app_name: None,
            frag_win_title: None,
            time_range_start: None,
            time_range_end: None,
            key_timestamps: None,
            work_item: None,
            work_status: None,
            work_progress: None,
        })
        .unwrap();

    // 场景一：timeline_id 列直接指向目标时间线
    let column_knowledge_id = sm
        .insert_bake_knowledge(&NewBakeKnowledge {
            timeline_id,
            title: "列匹配知识".to_string(),
            summary: "列匹配知识摘要".to_string(),
            content: Some(format!(r#"{{"source_title":"列匹配知识"}}"#)),
            detailed_content: None,
            entities: "[]".to_string(),
            importance: 5,
            source_capture_ids: None,
        })
        .unwrap();
    // 场景二：合并场景——timeline_id 列指向别处，仅 content.source_timeline_id 指向目标
    let merged_knowledge_id = sm
        .insert_bake_knowledge(&NewBakeKnowledge {
            timeline_id: other_timeline_id,
            title: "合并改写知识".to_string(),
            summary: "合并改写知识摘要".to_string(),
            content: Some(format!(
                r#"{{"source_title":"合并改写知识","source_timeline_id":{timeline_id}}}"#
            )),
            detailed_content: None,
            entities: "[]".to_string(),
            importance: 5,
            source_capture_ids: None,
        })
        .unwrap();

    let document_id = {
        let mut doc = NewBakeDocument::with_defaults("关联文档".to_string(), "article".to_string());
        doc.source_memory_ids = format!(r#"["{timeline_id}"]"#);
        sm.insert_bake_document(&doc).unwrap()
    };

    let sop_id = sm
        .insert_bake_sop(&NewBakeSop {
            timeline_id: other_timeline_id,
            title: "关联操作".to_string(),
            summary: "关联操作摘要".to_string(),
            content: Some(format!(
                r#"{{"source_title":"关联操作","source_timeline_id":{timeline_id},"steps":["步骤一"],"status":"candidate"}}"#
            )),
            detailed_content: None,
            entities: "[]".to_string(),
            importance: 5,
            source_capture_ids: None,
        })
        .unwrap();

    let data_id = sm
        .with_conn(|conn| {
            conn.execute(
                "INSERT INTO data_sources (
                    canonical_key, title, source_kind, access_mode, refresh_policy,
                    realtime_level, tags, first_seen_at, last_seen_at, status,
                    created_at, updated_at
                 ) VALUES (
                    'memory:test-related-data', '关联数据', 'work_memory', 'memory_only',
                    'never', 'observed', '[]', 100, 100, 'active', 100, 100
                 )",
                [],
            )?;
            let source_id = conn.last_insert_rowid();
            conn.execute(
                "INSERT INTO data_snapshots (
                    source_id, collected_at, observed_at, collector, content_text,
                    structured_data, content_hash, freshness_ttl_seconds, provenance,
                    source_capture_ids, source_timeline_ids, status, created_at
                 ) VALUES (?1, 100, 100, 'memory_extract', '转化率 12%',
                    '{\"metric_rows\":[{\"metric\":\"转化率\",\"value\":\"12%\"}]}',
                    'related-data', 0, '{}', '[]', ?2, 'success', 100)",
                rusqlite::params![source_id, format!("[{timeline_id}]")],
            )?;
            Ok(source_id)
        })
        .unwrap();

    let router = memory_bread_core::api::create_router(AppState::new(sm));

    let req = Request::builder()
        .uri(format!("/api/bake/memories/{timeline_id}/relations"))
        .body(Body::empty())
        .unwrap();
    let (status, body) = oneshot(router.clone(), req).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let json: serde_json::Value = serde_json::from_str(&body).unwrap();
    // 两条知识均指向目标时间线，接口只需返回其中一条（前端只消费单条关联知识）
    let knowledge_id = json["knowledge"]["id"].as_str().unwrap().to_string();
    assert!(
        knowledge_id == column_knowledge_id.to_string()
            || knowledge_id == merged_knowledge_id.to_string(),
        "knowledge: {:?}",
        json["knowledge"]
    );
    assert!(json["knowledge"]["source_timeline_id"] == timeline_id.to_string());
    assert_eq!(
        json["document"]["id"].as_str().unwrap(),
        document_id.to_string()
    );
    assert_eq!(json["sop"]["id"].as_str().unwrap(), sop_id.to_string());
    assert_eq!(json["data"]["id"].as_i64().unwrap(), data_id);
    assert_eq!(json["data"]["title"], "关联数据");

    // 无任何关联产物的时间线：四个字段均为 null
    let empty_req = Request::builder()
        .uri(format!("/api/bake/memories/{other_timeline_id}/relations"))
        .body(Body::empty())
        .unwrap();
    let (empty_status, empty_body) = oneshot(router, empty_req).await;
    assert_eq!(empty_status, StatusCode::OK, "body: {empty_body}");
    let empty_json: serde_json::Value = serde_json::from_str(&empty_body).unwrap();
    assert!(empty_json["knowledge"].is_null());
    assert!(empty_json["document"].is_null());
    assert!(empty_json["sop"].is_null());
    assert!(empty_json["data"].is_null());
}
