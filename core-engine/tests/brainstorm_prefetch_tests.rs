//! Real-router regressions for breadth-first exploration and disposable model
//! candidates. The Sidecar responds from the requested branch, not call order.
use axum::{
    body::Body,
    http::{Request, StatusCode},
    routing::post,
    Json, Router,
};
use http_body_util::BodyExt;
use memory_bread_core::{
    api::{create_router, state::AppState},
    storage::StorageManager,
};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::sync::Semaphore;
use tower::ServiceExt;

#[derive(Clone, Debug)]
struct Call {
    payload: Value,
    result: Value,
    returned: bool,
}

struct Mock {
    batch_goals: AtomicBool,
    call_permits: Mutex<std::collections::HashMap<usize, Arc<Semaphore>>>,
    calls: Mutex<Vec<Call>>,
    gate_background: AtomicBool,
    gate_foreground: AtomicBool,
    duplicate_background_prompt: AtomicBool,
    permits: Semaphore,
    foreground_permits: Semaphore,
}

// All selected directions have distinct labels, including in descendants, so
// an incorrect ancestry path changes the mock result and fails the assertions.
fn model_result(payload: &Value, ordinal: usize, safe: bool) -> Value {
    let stage = payload["exploration_stage"].as_str().unwrap_or("");
    if stage.is_empty() && !payload["decisions"].as_array().unwrap().is_empty() {
        return json!({"status":"ready", "open_flags":[], "continuation_directions":[
            {"id":"expand", "label":"继续拓展", "description":"探索新的方向"},
            {"id":"review", "label":"重新审视", "description":"讨论取舍"}
        ]});
    }
    let root = stage.is_empty();
    let stage = if root { "explore" } else { stage };
    let direction = if payload["focus_hint"]
        .as_str()
        .unwrap_or("")
        .contains("方向丙")
    {
        "方向丙"
    } else if payload["focus_hint"]
        .as_str()
        .unwrap_or("")
        .contains("方向乙")
    {
        "方向乙"
    } else {
        "方向甲"
    };
    let options = if root {
        json!([
            {"id":"a", "label":"方向甲", "description":"第一条独立方向", "recommended":true},
            {"id":"b", "label":"方向乙", "description":"第二条独立方向", "recommended":false},
            {"id":"c", "label":"方向丙", "description":"第三条独立方向", "recommended":false}
        ])
    } else {
        json!([
            {"id":"next", "label":format!("{direction}的常规推进"), "description":"继续明确这个方向", "recommended":true},
            {"id":"alternative", "label":format!("{direction}的替代推进"), "description":"比较另一种可行路径", "recommended":false}
        ])
    };
    let mut result = json!({
        "status":"question", "open_flags":[format!("{stage}待确认")],
        "readiness_reason":"仍需用户确认下一步",
        "question":{
            "id":if root {"root".to_owned()} else {format!("generated-{ordinal}")},
            "dimension_id":format!("{stage}-{direction}"), "dimension":stage,
            "exploration_stage":stage, "type":"multi_choice",
            "prompt":format!("{stage} / {direction} / response-{ordinal} 如何推进？"),
            "why_now":"明确该阶段的选择。", "required":true,
            "allow_custom":true, "answer_template":"补充你的需求", "options":options
        }
    });
    if safe {
        result["prefetch_safe_option_ids"] = if root {
            json!(["a", "b", "c"])
        } else {
            json!(["next", "alternative"])
        };
    }
    result
}

struct Fixture {
    router: Router,
    state: Arc<AppState>,
    mock: Arc<Mock>,
    server: tokio::task::JoinHandle<()>,
    turn_timeout: Duration,
    _tmp: tempfile::TempDir,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.server.abort();
        // Let already accepted handlers exit; discarded Core requests cannot
        // turn these responses into persisted user answers.
        self.mock.permits.add_permits(32);
        self.mock.foreground_permits.add_permits(32);
    }
}

impl Fixture {
    async fn new(safe: bool) -> Self {
        Self::with_upstream(safe, None).await
    }

    async fn with_upstream(safe: bool, upstream: Option<String>) -> Self {
        let live = upstream.is_some();
        let mock = Arc::new(Mock {
            batch_goals: AtomicBool::new(false),
            call_permits: Mutex::new(Default::default()),
            calls: Mutex::new(Vec::new()),
            gate_background: AtomicBool::new(!live),
            gate_foreground: AtomicBool::new(false),
            duplicate_background_prompt: AtomicBool::new(false),
            permits: Semaphore::new(0),
            foreground_permits: Semaphore::new(0),
        });
        let handler_mock = mock.clone();
        let sidecar = Router::new().route(
            "/creation/brainstorm/next",
            post(move |Json(payload): Json<Value>| {
                let mock = handler_mock.clone();
                let upstream = upstream.clone();
                async move {
                    let (index, result) = {
                        let mut calls = mock.calls.lock().unwrap();
                        let index = calls.len();
                        let mut result = if upstream.is_some() {
                            Value::Null
                        } else {
                            model_result(&payload, index + 1, safe)
                        };
                        if mock.batch_goals.load(Ordering::SeqCst) && upstream.is_none()
                            && !payload["exploration_stage"].as_str().unwrap_or("").is_empty()
                        {
                            let goal = payload["extension_goal"].as_str().unwrap_or("");
                            if goal.is_empty() && payload["question_batch_limit"].as_u64().unwrap_or(1) > 1 {
                                result["sibling_question_goals"] = json!(["怎样比较当前方向的取舍？", "怎样安排当前方向的协作？"]);
                            } else if !goal.is_empty() {
                                let direction = if payload["focus_hint"].as_str().unwrap_or("").contains("方向乙") {"乙"} else {"甲"};
                                result["question"]["prompt"] = json!(format!("{direction}方向：{goal}"));
                            }
                        }
                        if payload["prefetch"] == true
                            && mock.duplicate_background_prompt.load(Ordering::SeqCst)
                        {
                            let visible = calls
                                .iter()
                                .rev()
                                .find(|call| call.payload["prefetch"] != true)
                                .unwrap();
                            result["question"]["prompt"] =
                                visible.result["question"]["prompt"].clone();
                        }
                        calls.push(Call {
                            payload: payload.clone(),
                            result: result.clone(),
                            returned: false,
                        });
                        (index, result)
                    };
                    if payload["prefetch"] == true && mock.gate_background.load(Ordering::SeqCst) {
                        let permit = Arc::new(Semaphore::new(0));
                        mock.call_permits.lock().unwrap().insert(index, permit.clone());
                        tokio::select! {
                            global = mock.permits.acquire() => global.unwrap().forget(),
                            specific = permit.acquire() => specific.unwrap().forget(),
                        }
                    }
                    if payload["prefetch"] != true && mock.gate_foreground.load(Ordering::SeqCst) {
                        mock.foreground_permits.acquire().await.unwrap().forget();
                    }
                    let result = if let Some(upstream) = upstream {
                        reqwest::Client::new()
                            .post(format!(
                                "{}/creation/brainstorm/next",
                                upstream.trim_end_matches('/')
                            ))
                            .timeout(Duration::from_secs(180))
                            .json(&payload)
                            .send()
                            .await
                            .unwrap()
                            .json::<Value>()
                            .await
                            .unwrap()
                    } else {
                        result
                    };
                    {
                        let mut calls = mock.calls.lock().unwrap();
                        calls[index].result = result.clone();
                        calls[index].returned = true;
                    }
                    Json(result)
                }
            }),
        );
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
            mock,
            server,
            turn_timeout: Duration::from_secs(if live { 190 } else { 3 }),
            _tmp: tmp,
        }
    }

    async fn turn(&self, mut body: Value) -> Value {
        let object = body.as_object_mut().unwrap();
        object
            .entry("session_id")
            .or_insert(json!("prefetch-session"));
        object
            .entry("root_request")
            .or_insert(json!("设计工作方案"));
        let response = tokio::time::timeout(
            self.turn_timeout,
            self.router.clone().oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/creation/brainstorm/turn")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            ),
        )
        .await
        .expect("a foreground turn must not wait for the gated background request")
        .unwrap();
        let status = response.status();
        let value: Value =
            serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes())
                .unwrap();
        assert_eq!(status, StatusCode::OK, "request={body}; response={value}");
        value
    }

    async fn open_pair(&self) -> Value {
        let root = self.turn(json!({"action":"start"})).await;
        self.answer(&root, json!({"selected_option_ids":["a", "b"]}))
            .await
    }

    async fn answer(&self, current: &Value, answer: Value) -> Value {
        self.turn(json!({"action":"answer", "revision":current["revision"],
            "question_id":current["current_question"]["id"], "answer":answer}))
            .await
    }

    fn calls(&self) -> Vec<Call> {
        self.mock.calls.lock().unwrap().clone()
    }

    fn foreground_count(&self) -> usize {
        self.calls()
            .iter()
            .filter(|call| call.payload["prefetch"] != true)
            .count()
    }

    async fn wait_background(&self, after: usize) -> (usize, Call) {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if let Some(found) = self
                    .calls()
                    .into_iter()
                    .enumerate()
                    .skip(after)
                    .find(|(_, call)| call.payload["prefetch"] == true)
                {
                    return found;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("a sibling must start generating while the user sees the current question")
    }

    async fn wait_background_direction(&self, direction: &str) -> (usize, Call) {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if let Some(found) = self.calls().into_iter().enumerate().find(|(_, call)| {
                    call.payload["prefetch"] == true && call.payload["focus_hint"].as_str().unwrap_or("").contains(direction)
                }) { return found; }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }).await.unwrap()
    }

    async fn release_background(&self, index: usize) {
        let permit = self.mock.call_permits.lock().unwrap().get(&index).cloned().unwrap();
        permit.add_permits(1);
        tokio::time::timeout(Duration::from_secs(3), async {
            while !self.calls()[index].returned {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        // Give the local HTTP response time to reach Core before acting as the
        // next user. Inference itself is controlled by the explicit gate above.
        tokio::time::sleep(Duration::from_millis(40)).await;
    }

    async fn release_cancelled_background(&self, index: usize) {
        let permit = self.mock.call_permits.lock().unwrap().get(&index).cloned().unwrap();
        permit.add_permits(1);
        // Core intentionally aborts an unfinished speculative request before a
        // foreground miss. Depending on the HTTP server version, the mock
        // handler may be cancelled with the disconnected client and therefore
        // never mark the synthetic response as returned.
        tokio::time::sleep(Duration::from_millis(40)).await;
    }

    fn saved(&self) -> (String, i64, String) {
        self.state.storage.with_conn(|conn| Ok(conn.query_row(
            "SELECT phase, revision, state_json FROM creation_brainstorm_sessions WHERE session_id = 'prefetch-session'",
            [], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        )?)).unwrap()
    }
}

#[tokio::test]
async fn brainstorm_prefetch_warms_siblings_before_answers_without_persisting_speculation() {
    let fixture = Fixture::new(true).await;
    let first = fixture.open_pair().await;
    assert_eq!(first["current_question"]["parent_option_id"], "a");
    assert_eq!(first["answered_count"], 1);
    let saved = fixture.saved();
    let (index, warm) = fixture.wait_background(0).await;
    assert_eq!(warm.payload["exploration_stage"], "solutions");
    assert!(warm.payload["focus_hint"]
        .as_str()
        .unwrap()
        .contains("方向乙"));
    assert_eq!(warm.payload["decisions"].as_array().unwrap().len(), 1);
    assert_eq!(fixture.foreground_count(), 2);
    assert_eq!(
        fixture.saved(),
        saved,
        "background inference cannot publish a question or an answer"
    );
    fixture.release_background(index).await;
    assert_eq!(
        fixture.saved(),
        saved,
        "completed candidates must remain disposable and outside SQLite"
    );

    let second = fixture
        .answer(&first, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(
        fixture.foreground_count(),
        2,
        "the prepared sibling should need no foreground model call"
    );
    assert_eq!(
        second["current_question"]["prompt"],
        warm.result["question"]["prompt"]
    );
    assert_eq!(second["current_question"]["parent_question_id"], "root");
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_eq!(second["current_question"]["exploration_stage"], "solutions");
    assert_eq!(
        second["revision"].as_i64(),
        first["revision"].as_i64().map(|revision| revision + 1)
    );
    assert_eq!(second["history"].as_array().unwrap().len(), 2);

    let (next_index, warm_next) = fixture.wait_background(index + 1).await;
    assert_eq!(warm_next.payload["exploration_stage"], "implementation");
    assert!(warm_next.payload["focus_hint"]
        .as_str()
        .unwrap()
        .contains("方向甲 → 方向甲的常规推进"));
    assert!(!warm_next.payload["focus_hint"]
        .as_str()
        .unwrap()
        .contains("方向乙"));
    assert_eq!(warm_next.payload["decisions"].as_array().unwrap().len(), 2);
    fixture.release_background(next_index).await;
    let implementation = fixture
        .answer(&second, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(fixture.foreground_count(), 2);
    assert_eq!(
        implementation["current_question"]["prompt"],
        warm_next.result["question"]["prompt"]
    );
    assert_eq!(
        implementation["current_question"]["parent_question_id"],
        first["current_question"]["id"]
    );
    assert_eq!(
        implementation["current_question"]["exploration_stage"],
        "implementation"
    );
    assert_eq!(implementation["history"].as_array().unwrap().len(), 3);
}

#[tokio::test]
async fn brainstorm_prefetch_promotes_an_unfinished_sibling_to_foreground_without_waiting() {
    let fixture = Fixture::new(true).await;
    let first = fixture.open_pair().await;
    let (index, old) = fixture.wait_background(0).await;
    let second = fixture
        .answer(&first, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(fixture.foreground_count(), 3);
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_ne!(
        second["current_question"]["prompt"],
        old.result["question"]["prompt"]
    );
    let saved = fixture.saved();
    fixture.release_cancelled_background(index).await;
    assert_eq!(
        fixture.saved(),
        saved,
        "a cancelled request returning late cannot overwrite the foreground answer"
    );
}

#[tokio::test]
async fn brainstorm_prefetch_custom_answer_invalidates_a_prepared_sibling() {
    let fixture = Fixture::new(true).await;
    let first = fixture.open_pair().await;
    let (index, old) = fixture.wait_background(0).await;
    fixture.release_background(index).await;
    let custom = "两个方向必须共同满足新增限制";
    let second = fixture.answer(&first, json!({"custom_text":custom})).await;
    assert_eq!(fixture.foreground_count(), 3);
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_ne!(
        second["current_question"]["prompt"],
        old.result["question"]["prompt"]
    );
    let foreground = fixture
        .calls()
        .into_iter()
        .filter(|call| call.payload["prefetch"] != true)
        .last()
        .unwrap();
    assert!(foreground.payload.to_string().contains(custom));
}

#[tokio::test]
async fn brainstorm_prefetch_brief_edit_discards_old_context_and_keeps_late_results_out_of_storage()
{
    let fixture = Fixture::new(true).await;
    let first = fixture.open_pair().await;
    let (old_index, old) = fixture.wait_background(0).await;
    let edited = fixture
        .turn(json!({"action":"edit_brief", "revision":first["revision"],
        "brief_edits":{"root_request":"仅讨论采用离线方案的团队"}}))
        .await;
    let saved = fixture.saved();
    fixture.release_background(old_index).await;
    assert_eq!(fixture.saved(), saved);
    let (new_index, refreshed) = fixture.wait_background(old_index + 1).await;
    assert_eq!(
        refreshed.payload["root_request"],
        "仅讨论采用离线方案的团队"
    );
    assert_ne!(
        old.payload["root_request"],
        refreshed.payload["root_request"]
    );
    fixture.release_background(new_index).await;
    let second = fixture
        .answer(&edited, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(
        second["current_question"]["prompt"],
        refreshed.result["question"]["prompt"]
    );
    assert_ne!(
        second["current_question"]["prompt"],
        old.result["question"]["prompt"]
    );
    assert_eq!(
        second["brief_edits"]["root_request"],
        "仅讨论采用离线方案的团队"
    );
}

#[tokio::test]
async fn brainstorm_prefetch_finish_and_abandon_discard_late_candidates() {
    for action in ["finish", "abandon"] {
        let fixture = Fixture::new(true).await;
        let first = fixture.open_pair().await;
        let (index, _) = fixture.wait_background(0).await;
        let terminal = fixture
            .turn(json!({"action":action, "revision":first["revision"], "accept_assumptions":true}))
            .await;
        assert_eq!(
            terminal["phase"],
            if action == "finish" {
                "ready"
            } else {
                "abandoned"
            }
        );
        let saved = fixture.saved();
        let count = fixture.calls().len();
        fixture.release_background(index).await;
        assert_eq!(
            fixture.saved(),
            saved,
            "{action} must prevent background state writes"
        );
        assert_eq!(
            fixture.calls().len(),
            count,
            "{action} must not schedule further candidates"
        );
        if action == "finish" {
            assert!(terminal["current_question"].is_null());
        } else {
            assert_eq!(terminal["current_question"], first["current_question"]);
        }
        assert_eq!(terminal["answered_count"], 1);
    }
}

#[tokio::test]
async fn brainstorm_prefetch_model_configuration_change_does_not_reuse_a_candidate() {
    for (key, value) in [
        ("creation_model", "different-model"),
        ("creation_base_url", "http://localhost:9998/v1"),
        ("creation_api_key", "test-only-account-key"),
    ] {
        let fixture = Fixture::new(true).await;
        let first = fixture.open_pair().await;
        let (index, old) = fixture.wait_background(0).await;
        fixture.release_background(index).await;
        let mut request = json!({"action":"answer", "revision":first["revision"],
            "question_id":first["current_question"]["id"], "answer":{"selected_option_ids":["next"]}});
        request[key] = json!(value);
        let second = fixture.turn(request).await;
        assert_eq!(
            fixture.foreground_count(),
            3,
            "changed {key} must isolate candidates"
        );
        assert_ne!(
            second["current_question"]["prompt"],
            old.result["question"]["prompt"]
        );
        let latest = fixture
            .calls()
            .into_iter()
            .filter(|call| call.payload["prefetch"] != true)
            .last()
            .unwrap();
        assert_eq!(latest.payload[key], value);
    }
}

#[tokio::test]
async fn brainstorm_prefetch_revised_ancestor_regenerates_the_remaining_direction() {
    let fixture = Fixture::new(true).await;
    let first = fixture.open_pair().await;
    let (index, old) = fixture.wait_background(0).await;
    fixture.release_background(index).await;
    let revised = fixture
        .turn(
            json!({"action":"revise_answer", "revision":first["revision"],
        "question_id":"root", "answer":{"selected_option_ids":["b"]}}),
        )
        .await;
    assert_eq!(fixture.foreground_count(), 3);
    assert_eq!(revised["current_question"]["parent_option_id"], "b");
    assert_ne!(
        revised["current_question"]["prompt"],
        old.result["question"]["prompt"]
    );
    assert_eq!(revised["history"].as_array().unwrap().len(), 1);
    assert_eq!(
        revised["history"][0]["answer"]["selected_option_ids"],
        json!(["b"])
    );
    assert_eq!(revised["archived_questions"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn brainstorm_prefetch_legacy_sidecar_keeps_breadth_without_speculation() {
    let fixture = Fixture::new(false).await;
    let first = fixture.open_pair().await;
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(fixture.calls().len(), 2);
    let second = fixture
        .answer(&first, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_eq!(second["current_question"]["exploration_stage"], "solutions");
    assert_eq!(fixture.foreground_count(), 3);
    assert!(fixture
        .calls()
        .iter()
        .all(|call| call.payload["prefetch"] != true));
}

#[tokio::test]
async fn brainstorm_prefetch_cache_hit_preserves_another_siblings_inflight_generation() {
    let fixture = Fixture::new(true).await;
    let root = fixture.turn(json!({"action":"start"})).await;
    let first = fixture
        .answer(&root, json!({"selected_option_ids":["a", "b", "c"]}))
        .await;
    let (b_index, b_warm) = fixture.wait_background_direction("方向乙").await;
    assert!(b_warm.payload["focus_hint"]
        .as_str()
        .unwrap()
        .contains("方向乙"));
    fixture.release_background(b_index).await;
    let (c_index, c_warm) = fixture.wait_background_direction("方向丙").await;
    assert!(c_warm.payload["focus_hint"]
        .as_str()
        .unwrap()
        .contains("方向丙"));
    let second = fixture
        .answer(&first, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_eq!(fixture.foreground_count(), 2);
    tokio::time::sleep(Duration::from_millis(40)).await;
    let c_calls = fixture
        .calls()
        .iter()
        .filter(|call| {
            call.payload["exploration_stage"] == "solutions"
                && call.payload["focus_hint"]
                    .as_str()
                    .unwrap_or("")
                    .contains("方向丙")
        })
        .count();
    assert_eq!(
        c_calls, 1,
        "publishing B must not cancel and restart the valid C inference"
    );
    fixture.release_background(c_index).await;
    let third = fixture
        .answer(&second, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(third["current_question"]["parent_option_id"], "c");
    assert_eq!(
        third["current_question"]["prompt"],
        c_warm.result["question"]["prompt"]
    );
    assert_eq!(third["current_question"]["exploration_stage"], "solutions");
    assert_eq!(fixture.foreground_count(), 2);
}

#[tokio::test]
async fn brainstorm_prefetch_rechecks_a_duplicate_prompt_against_the_latest_answered_question() {
    let fixture = Fixture::new(true).await;
    fixture
        .mock
        .duplicate_background_prompt
        .store(true, Ordering::SeqCst);
    let first = fixture.open_pair().await;
    let (index, warm) = fixture.wait_background(0).await;
    assert_eq!(
        warm.result["question"]["prompt"],
        first["current_question"]["prompt"]
    );
    fixture.release_background(index).await;
    let second = fixture
        .answer(&first, json!({"selected_option_ids":["next"]}))
        .await;
    assert_eq!(
        fixture.foreground_count(),
        3,
        "a repeated A prompt must be regenerated before publishing B"
    );
    assert_ne!(
        second["current_question"]["prompt"],
        first["current_question"]["prompt"]
    );
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_eq!(second["current_question"]["exploration_stage"], "solutions");
}

#[tokio::test]
async fn brainstorm_prefetch_old_restore_cannot_restart_revoked_background_context() {
    let fixture = Fixture::new(true).await;
    let first = fixture.open_pair().await;
    let (index, old) = fixture.wait_background(0).await;
    fixture.mock.gate_foreground.store(true, Ordering::SeqCst);
    let answer = fixture.answer(
        &first,
        json!({"custom_text":"只使用当前输入，后续不要检索历史资料"}),
    );
    tokio::pin!(answer);
    let foreground_entered = async {
        tokio::time::timeout(Duration::from_secs(2), async {
            while fixture.foreground_count() < 3 {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
    };
    tokio::select! {
        _ = &mut answer => panic!("the foreground model should still be gated"),
        _ = foreground_entered => {},
    }
    let restored = fixture.turn(json!({"action":"start"})).await;
    assert_eq!(restored["revision"], first["revision"]);
    assert_eq!(restored["current_question"], first["current_question"]);
    let saved = fixture.saved();
    fixture.release_cancelled_background(index).await;
    assert_eq!(fixture.saved(), saved);
    assert_eq!(
        fixture
            .calls()
            .iter()
            .filter(|call| call.payload["prefetch"] == true)
            .count(),
        1,
        "restoring the old revision must not restart a candidate after custom input revoked it"
    );
    fixture.mock.foreground_permits.add_permits(1);
    let second = answer.await;
    assert_eq!(second["current_question"]["parent_option_id"], "b");
    assert_ne!(
        second["current_question"]["prompt"],
        old.result["question"]["prompt"]
    );
    assert_eq!(second["history"].as_array().unwrap().len(), 2);
}

/// Isolated acceptance against an explicitly provided real Sidecar. It uses a
/// temporary database and synthetic choices, never an existing user session.
#[tokio::test]
#[ignore = "requires BRAINSTORM_LIVE_SIDECAR_URL and the real local model"]
async fn brainstorm_prefetch_live_sidecar_synthetic_breadth_acceptance() {
    let upstream =
        std::env::var("BRAINSTORM_LIVE_SIDECAR_URL").expect("provide an isolated Sidecar URL");
    let fixture = Fixture::with_upstream(true, Some(upstream)).await;
    let request = "这是隔离的合成验收任务，不涉及真实用户。请只使用当前输入，不检索个人记忆或历史资料。为一个虚构的六人读书会设计一次四十五分钟线上活动：材料是大家已读完的同一本虚构短篇，主题完全自由，没有费用、不用外部工具、不收集个人信息。请先给出可多选的讨论方向，再逐个讨论具体方式；不要代替参与者作答。";
    let started_at = std::time::Instant::now();
    let root = fixture
        .turn(json!({"action":"start", "root_request":request}))
        .await;
    let first_question_ms = started_at.elapsed().as_millis();
    eprintln!("live brainstorm: first question completed in {first_question_ms} ms");
    assert_eq!(root["current_question"]["type"], "multi_choice");
    let options = root["current_question"]["options"]
        .as_array()
        .expect("first turn should ask a question");
    assert!(options.len() >= 2);
    let selected = vec![options[0]["id"].clone(), options[1]["id"].clone()];
    let a_started = std::time::Instant::now();
    let first = fixture
        .answer(&root, json!({"selected_option_ids":selected}))
        .await;
    let first_direction_ms = a_started.elapsed().as_millis();
    eprintln!("live brainstorm: first direction completed in {first_direction_ms} ms; waiting for the sibling in background");
    assert_eq!(first["current_question"]["parent_option_id"], selected[0]);
    let saved = fixture.saved();
    let (warm_index, _) = fixture.wait_background(0).await;
    let warm_started = std::time::Instant::now();
    tokio::time::timeout(Duration::from_secs(180), async {
        while !fixture.calls()[warm_index].returned {
            assert_eq!(
                fixture.saved(),
                saved,
                "background generation must not alter the user's state"
            );
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    })
    .await
    .expect("the real model should finish the sibling while the synthetic user is reading");
    let warm_ms = warm_started.elapsed().as_millis();
    eprintln!(
        "live brainstorm: sibling Sidecar returned after {warm_ms} ms; checking cache publication"
    );
    tokio::time::sleep(Duration::from_millis(100)).await;
    let warm = fixture.calls()[warm_index].clone();
    assert_eq!(
        warm.result["status"], "question",
        "real Sidecar response: {}",
        warm.result
    );
    assert_eq!(fixture.saved(), saved);
    let stored: Value = serde_json::from_str(&saved.2).unwrap();
    let safe = stored["prefetch_safe_options"][first["current_question"]["id"].as_str().unwrap()]
        .as_array()
        .expect("Sidecar should identify ordinary branch choices");
    assert!(
        !safe.is_empty(),
        "the synthetic direction must include an ordinary answer"
    );
    let cold_count = fixture.foreground_count();
    let hit_started = std::time::Instant::now();
    let second = fixture
        .answer(&first, json!({"selected_option_ids":[safe[0]]}))
        .await;
    let hit_ms = hit_started.elapsed().as_millis();
    assert_eq!(
        fixture.foreground_count(),
        cold_count,
        "ready B must not invoke a foreground model"
    );
    assert_eq!(
        second["current_question"]["parent_question_id"],
        root["current_question"]["id"]
    );
    assert_eq!(second["current_question"]["parent_option_id"], selected[1]);
    assert_eq!(
        second["current_question"]["exploration_stage"],
        first["current_question"]["exploration_stage"]
    );
    assert_eq!(
        second["current_question"]["prompt"],
        warm.result["question"]["prompt"]
    );
    assert_eq!(
        second["revision"].as_i64().unwrap(),
        first["revision"].as_i64().unwrap() + 1
    );
    eprintln!("live brainstorm: first_question_ms={first_question_ms}, first_direction_ms={first_direction_ms}, background_sibling_ms={warm_ms}, prepared_sibling_answer_ms={hit_ms}, foreground_calls={cold_count}, revision_before={}, revision_after={}, stage={}",
        first["revision"], second["revision"], second["current_question"]["exploration_stage"]);
    // Explicitly cancel remaining disposable work in this synthetic session.
    fixture
        .turn(json!({"action":"abandon", "revision":second["revision"]}))
        .await;
}


#[tokio::test]
async fn brainstorm_extensions_generate_concurrently_and_publish_every_same_level_facet() {
    let fixture = Fixture::new(true).await;
    fixture.mock.batch_goals.store(true, Ordering::SeqCst);
    let first = fixture.open_pair().await;
    let baseline = fixture.saved();
    let plans: Value = serde_json::from_str(&baseline.2).unwrap();
    assert_eq!(plans["pending_extensions"].as_array().unwrap().len(), 2);
    assert_eq!(first["answered_count"], 1);
    assert!(!first["brief_markdown"].as_str().unwrap().contains("怎样比较当前方向的取舍"));
    tokio::time::timeout(Duration::from_secs(3), async {
        while fixture.calls().iter().filter(|call| call.payload["prefetch"] == true).count() < 3 {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.expect("B first question and A's two independent facets must enter generation concurrently");
    let warm: Vec<_> = fixture.calls().into_iter().filter(|call| call.payload["prefetch"] == true).collect();
    assert_eq!(warm.len(), 3);
    assert!(warm.iter().all(|call| !call.returned));
    assert!(warm.iter().all(|call| call.payload["decisions"].as_array().unwrap().len() == 1));
    assert_eq!(warm.iter().filter(|call| !call.payload["extension_goal"].as_str().unwrap().is_empty()).count(), 2);
    for call in warm.iter().filter(|call| !call.payload["extension_goal"].as_str().unwrap().is_empty()) {
        let context = call.payload["sibling_question_context"].as_array().unwrap();
        assert!(context.contains(&first["current_question"]["prompt"]), "the unanswered first question must be visible as repetition context");
        assert_eq!(context.len(), 2, "also include the other independent planned topic");
        assert!(!context.contains(&call.payload["extension_goal"]));
    }
    assert_eq!(fixture.saved(), baseline, "parallel preparation must never change user state");
    fixture.mock.gate_background.store(false, Ordering::SeqCst);
    fixture.mock.permits.add_permits(32);
    tokio::time::timeout(Duration::from_secs(3), async {
        while fixture.calls().iter().any(|call| call.payload["prefetch"] == true && !call.returned) {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.expect("all prepared facets should finish before the synthetic user consumes them");
    // The mock marks the HTTP response before Core publishes it into the
    // disposable cache. Wait for that final local handoff as a separate step.
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(fixture.saved(), baseline);
    let mut current = first;
    let mut ids = std::collections::HashSet::new();
    for option in ["a", "b", "a", "b", "a", "b"] {
        let question = &current["current_question"];
        assert_eq!(question["parent_question_id"], "root");
        assert_eq!(question["parent_option_id"], option);
        assert_eq!(question["exploration_stage"], "solutions");
        assert!(ids.insert(question["id"].as_str().unwrap().to_string()));
        assert_eq!(fixture.foreground_count(), 2,
            "prepared facets should not request foreground inference: option={option} prompt={} calls={} foreground={:?}",
            question["prompt"], fixture.calls().len(), fixture.calls().into_iter()
                .filter(|call| call.payload["prefetch"] != true)
                .map(|call| (call.payload["focus_hint"].clone(), call.payload["extension_goal"].clone()))
                .collect::<Vec<_>>());
        current = fixture.answer(&current, json!({"selected_option_ids":["next"]})).await;
        tokio::time::sleep(Duration::from_millis(80)).await;
    }
    assert_eq!(current["answered_count"], 7);
    assert_eq!(current["current_question"]["exploration_stage"], "implementation");
    assert_eq!(current["history"].as_array().unwrap().len(), 7);
}

#[tokio::test]
async fn brainstorm_extension_plans_restore_without_answers_and_revoke_on_permission_change() {
    let fixture = Fixture::new(true).await;
    fixture.mock.batch_goals.store(true, Ordering::SeqCst);
    let root = fixture.turn(json!({"action":"start"})).await;
    let first = fixture.answer(&root, json!({"selected_option_ids":["a"]})).await;
    let saved = fixture.saved();
    let restored = fixture.turn(json!({"action":"start"})).await;
    assert_eq!(restored, first);
    assert_eq!(fixture.saved(), saved, "restore cannot consume plans or add answers");
    fixture.mock.batch_goals.store(false, Ordering::SeqCst);
    let changed = fixture.answer(&first, json!({"custom_text":"仅依据这次输入，不使用历史记忆"})).await;
    let latest: Value = serde_json::from_str(&fixture.saved().2).unwrap();
    assert_eq!(latest["pending_extensions"], json!([]));
    assert_eq!(changed["answered_count"], 2);
    let last = fixture.calls().into_iter().filter(|call| call.payload["prefetch"] != true).last().unwrap();
    assert_eq!(last.payload["extension_goal"], "");
    assert!(last.payload["decisions"].to_string().contains("不使用历史记忆"));
}

#[tokio::test]
async fn brainstorm_pending_extension_promotes_to_foreground_without_losing_goal() {
    let fixture = Fixture::new(true).await;
    fixture.mock.batch_goals.store(true, Ordering::SeqCst);
    let root = fixture.turn(json!({"action":"start"})).await;
    let first = fixture.answer(&root, json!({"selected_option_ids":["a"]})).await;
    fixture.wait_background(0).await;
    let next = fixture.answer(&first, json!({"selected_option_ids":["next"]})).await;
    assert_eq!(next["current_question"]["parent_question_id"], "root");
    assert_eq!(next["current_question"]["parent_option_id"], "a");
    assert_eq!(next["current_question"]["exploration_stage"], "solutions");
    let calls = fixture.calls();
    let last = calls.iter().filter(|call| call.payload["prefetch"] != true).last().unwrap();
    assert_eq!(last.payload["extension_goal"], "怎样比较当前方向的取舍？");
    assert_eq!(last.payload["question_batch_limit"], 1);
    let latest: Value = serde_json::from_str(&fixture.saved().2).unwrap();
    assert_eq!(latest["pending_extensions"].as_array().unwrap().len(), 1);
    assert_eq!(fixture.foreground_count(), 3);
}


#[tokio::test]
async fn brainstorm_extension_plan_survives_a_core_cache_restart() {
    let mut fixture = Fixture::new(true).await;
    fixture.mock.batch_goals.store(true, Ordering::SeqCst);
    let root = fixture.turn(json!({"action":"start"})).await;
    let first = fixture.answer(&root, json!({"selected_option_ids":["a"]})).await;
    let before = fixture.saved();
    let storage = StorageManager::open(&fixture._tmp.path().join("brainstorm.db")).unwrap();
    let fresh_state = AppState::with_service_urls(storage, "http://127.0.0.1:9".into(),
        fixture.state.creation_sidecar_url.clone(), vec![]);
    fixture.router = create_router(fresh_state.clone());
    fixture.state = fresh_state;
    let restored = fixture.turn(json!({"action":"start"})).await;
    assert_eq!(restored, first);
    assert_eq!(fixture.saved(), before);
    let next = fixture.answer(&restored, json!({"selected_option_ids":["next"]})).await;
    assert_eq!(next["current_question"]["parent_question_id"], "root");
    assert_eq!(next["current_question"]["exploration_stage"], "solutions");
    assert_eq!(next["answered_count"], 2);
    let saved: Value = serde_json::from_str(&fixture.saved().2).unwrap();
    assert_eq!(saved["pending_extensions"].as_array().unwrap().len(), 1);
}
