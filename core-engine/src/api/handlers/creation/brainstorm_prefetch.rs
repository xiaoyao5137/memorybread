//! Disposable candidates; only the ordinary turn transaction publishes a question.
use super::*;
use std::collections::BTreeMap;
use tokio::task::JoinHandle;

const TTL: Duration = Duration::from_secs(600);
const MAX_SESSIONS: usize = 8;

#[derive(Default)]
pub(crate) struct BrainstormPrefetchCache {
    sessions: HashMap<String, Session>,
}

struct Session {
    token: String,
    touched: Instant,
    ready: BTreeMap<String, DynamicBrainstormResult>,
    running: BTreeMap<String, JoinHandle<()>>,
    queued: std::collections::VecDeque<Job>,
    minimum_revision: i64,
}

struct Job {
    key: String,
    snapshot: BrainstormStoredState,
    request: BrainstormTurnRequest,
}

impl Drop for Session {
    fn drop(&mut self) {
        for (_, task) in std::mem::take(&mut self.running) { task.abort(); }
    }
}

pub(super) fn branch_key(
    stored: &BrainstormStoredState, req: &BrainstormTurnRequest, branch: &PendingBrainstormBranch<'_>,
) -> String {
    let mut ancestry = Vec::new();
    let mut current = Some(branch.question);
    let mut seen = std::collections::HashSet::new();
    while let Some(question) = current {
        if !seen.insert(&question.id) { break; }
        let turn = stored.turns.iter().find(|turn| turn.question.id == question.id);
        ancestry.push(serde_json::json!([question, turn.map(|turn| &turn.answer), stored.user_input_revisions.get(&question.id)]));
        current = question.parent_question_id.as_ref().and_then(|id| {
            stored.turns.iter().find(|turn| &turn.question.id == id).map(|turn| &turn.question)
        });
    }
    // Never serialize credentials into logs or persist a cache entry. Hashing
    // configuration prevents an account/model switch reusing a prior result.
    sha256_hex(&serde_json::json!([
        req.session_id.trim(), stored.root_request, stored.brief_edits, stored.selected_skills,
        stored.user_input_revisions.get("root_request"), ancestry,
        branch.option.map(|option| &option.id), branch.focus, branch.exploration_stage, branch.extension,
        req.creation_model, req.creation_base_url, req.creation_api_key
    ]).to_string())
}

pub(super) fn answer_preserves_candidates(stored: &BrainstormStoredState, req: &BrainstormTurnRequest) -> bool {
    if req.action != "answer" { return false; }
    let Some(question) = stored.current_question.as_ref() else { return false; };
    let Some(answer) = req.answer.as_ref() else { return false; };
    let Some(safe) = stored.prefetch_safe_options.get(&question.id) else { return false; };
    req.question_id.as_deref() == Some(question.id.as_str())
        && answer.custom_text.trim().is_empty()
        && !answer.selected_option_ids.is_empty()
        && answer.selected_option_ids.iter().all(|id| safe.contains(id))
        && validate_brainstorm_answer(question, answer)
}

fn empty_session() -> Session {
    Session { token: uuid::Uuid::new_v4().to_string(), touched: Instant::now(),
        ready: BTreeMap::new(), running: BTreeMap::new(),
        queued: Default::default(), minimum_revision: 0 }
}

pub(super) fn invalidate(state: &AppState, session: &str) {
    state.brainstorm_prefetch.lock().unwrap().sessions.remove(session);
}

// Retain a bounded tombstone while an incompatible foreground operation is
// running. A concurrent restore of its old DB revision cannot restart old work.
pub(super) fn invalidate_revision(state: &AppState, session: &str, revision: i64) {
    let mut cache = state.brainstorm_prefetch.lock().unwrap();
    trim(&mut cache, session);
    let previous_min = cache.sessions.remove(session).map(|entry| entry.minimum_revision).unwrap_or(0);
    let mut entry = empty_session();
    entry.minimum_revision = previous_min.max(revision + 1);
    cache.sessions.insert(session.to_string(), entry);
}

fn trim(cache: &mut BrainstormPrefetchCache, session: &str) {
    cache.sessions.retain(|_, entry| entry.touched.elapsed() < TTL);
    if !cache.sessions.contains_key(session) && cache.sessions.len() >= MAX_SESSIONS {
        if let Some(oldest) = cache.sessions.iter().min_by_key(|(_, entry)| entry.touched).map(|(id, _)| id.clone()) {
            cache.sessions.remove(&oldest);
        }
    }
}

pub(super) fn stop_running(state: &AppState, session: &str) {
    if let Some(entry) = state.brainstorm_prefetch.lock().unwrap().sessions.get_mut(session) {
        for (_, task) in std::mem::take(&mut entry.running) { task.abort(); }
        entry.queued.clear();
        // Revoking the token also rejects a completion racing with cancellation.
        entry.token = uuid::Uuid::new_v4().to_string();
    }
}

pub(super) fn take(state: &AppState, stored: &BrainstormStoredState, req: &BrainstormTurnRequest) -> Option<DynamicBrainstormResult> {
    let mut effective = stored.clone();
    archive_superseded_brainstorm_turns(&mut effective);
    let branch = pending_brainstorm_branch(&effective)?;
    let key = branch_key(&effective, req, &branch);
    let mut cache = state.brainstorm_prefetch.lock().unwrap();
    cache.sessions.retain(|_, entry| entry.touched.elapsed() < TTL);
    let entry = cache.sessions.get_mut(req.session_id.trim())?;
    let mut result = entry.ready.remove(&key)?;
    let question = result.question.as_mut()?;
    if !apply_brainstorm_question_stage(question, Some(&branch)) { return None; }
    let fingerprint = brainstorm_question_fingerprint(&question.prompt);
    let repeats_answered = effective.turns.iter().any(|turn| {
        // An extension is deliberately another facet of the same branch, so
        // it may reuse the selected-option vocabulary from the parent turn.
        // Reject overlapping prompts, but do not collapse a distinct facet
        // merely because its choices resemble that earlier decision.
        if branch.extension.is_some() {
            brainstorm_questions_overlap(&turn.question, question)
        } else {
            brainstorm_question_repeats_answered(turn, question)
        }
    });
    if repeats_answered {
        log_brainstorm_stage(req, "prefetch_duplicate_discarded", Instant::now());
        return None;
    }
    // Speculative turns may share model-generated IDs because they have the
    // same answered_count. Give each unpublished candidate a Core-owned ID.
    question.id = format!("prefetch_{}", uuid::Uuid::new_v4());
    result.sibling_question_goals.retain(|goal| {
        let goal_fingerprint = brainstorm_question_fingerprint(goal);
        goal_fingerprint != fingerprint
            && !effective.turns.iter().any(|turn| brainstorm_question_fingerprint(&turn.question.prompt) == goal_fingerprint)
            && !effective.pending_extensions.iter().any(|plan| {
                plan.parent_question_id == branch.question.id
                    && plan.parent_option_id.as_deref() == branch.option.map(|option| option.id.as_str())
                    && brainstorm_question_fingerprint(&plan.goal) == goal_fingerprint
            })
    });
    // A sibling may have been answered since generation. Only publish the new
    // question; never roll back the current global brief or resolved flags.
    // Retain both sets of verified references for ordinary independent sibling
    // answers; explicit edits revoke the entire cache and clear derived memory.
    let mut references = stored.memory_brief.split("\n\n").filter(|part| !part.trim().is_empty())
        .map(str::to_string).collect::<Vec<_>>();
    for part in result.memory_brief.split("\n\n").filter(|part| !part.trim().is_empty()) {
        if !references.iter().any(|existing| existing == part) { references.push(part.to_string()); }
    }
    result.memory_brief = references.join("\n\n");
    result.open_flags = brainstorm_effective_open_flags(stored);
    if !stored.brief_edits.contains_key("open_flags") {
        // Natural-language facts cannot be deemed resolved from option clicks.
        // Remove only flags explicitly equal to an answered question; retain
        // other concrete unknowns until the next full-context model assessment.
        result.open_flags.retain(|flag| !effective.turns.iter().any(|turn| {
            (matches!(turn.answer.source.as_str(), "user" | "user_excluded")
                || effective.brief_edits.contains_key(&turn.question.id))
                && !brainstorm_effective_summary(&effective, turn).trim().is_empty()
                && brainstorm_question_fingerprint(flag) == brainstorm_question_fingerprint(&turn.question.prompt)
        }));
    }
    result.readiness_reason = stored.readiness_reason.clone();
    entry.touched = Instant::now();
    log_brainstorm_stage(req, "prefetch_hit", Instant::now());
    Some(result)
}

pub(super) fn schedule(state: &Arc<AppState>, stored: &BrainstormStoredState, req: &BrainstormTurnRequest, phase: &str, revision: i64) {
    // Lock ordering is storage -> cache everywhere we need both. No stale read
    // may re-enable speculative retrieval after another request changed inputs.
    let _ = with_brainstorm_session_guard(state, req.session_id.trim(), phase == "abandoned", |conn| {
        let current = crate::storage::repo::creation_brainstorm::get(conn, req.session_id.trim())?;
        if current.is_some_and(|session| session.revision == revision && session.phase == phase) {
            schedule_current(state, stored, req, phase, revision);
        }
        Ok(())
    });
}

fn schedule_current(state: &Arc<AppState>, stored: &BrainstormStoredState, req: &BrainstormTurnRequest, phase: &str, revision: i64) {
    let limit = std::env::var("MEMORYBREAD_BRAINSTORM_PREFETCH_LIMIT").ok()
        .and_then(|value| value.parse::<usize>().ok()).unwrap_or(4).min(16);
    let current_safe = stored.current_question.as_ref().and_then(|q| stored.prefetch_safe_options.get(&q.id))
        .is_some_and(|ids| !ids.is_empty());
    if phase != "exploring" || !current_safe || limit == 0 {
        invalidate(state, req.session_id.trim());
        return;
    }
    let mut snapshot = stored.clone();
    archive_superseded_brainstorm_turns(&mut snapshot);
    let all_keys: Vec<_> = pending_brainstorm_branches(&snapshot).iter()
        .map(|branch| branch_key(&snapshot, req, branch)).collect();
    let scheduled_keys: Vec<_> = all_keys.iter().take(limit).cloned().collect();
    let mut cache = state.brainstorm_prefetch.lock().unwrap();
    trim(&mut cache, req.session_id.trim());
    let entry = cache.sessions.entry(req.session_id.trim().to_string()).or_insert_with(empty_session);
    if revision < entry.minimum_revision { return; }
    entry.touched = Instant::now();
    // The limit bounds new speculative work, not already-completed valid
    // candidates. Dropping a ready candidate merely because it moved outside
    // the current scheduling window turns a later breadth-first step back into
    // foreground inference.
    entry.ready.retain(|key, _| all_keys.contains(key));
    entry.running.retain(|key, task| {
        if !all_keys.contains(key) { task.abort(); return false; }
        !task.is_finished()
    });
    entry.queued = scheduled_keys.into_iter().filter(|key| !entry.ready.contains_key(key) && !entry.running.contains_key(key))
        .map(|key| Job { key, snapshot: snapshot.clone(), request: req.clone() }).collect();
    launch_jobs(state, req.session_id.trim(), entry);
}

fn launch_jobs(state: &Arc<AppState>, session_id: &str, entry: &mut Session) {
    let concurrency = std::env::var("MEMORYBREAD_BRAINSTORM_PREFETCH_CONCURRENCY").ok()
        .and_then(|value| value.parse::<usize>().ok()).unwrap_or(3).clamp(1, 8);
    while entry.running.len() < concurrency {
        let Some(job) = entry.queued.pop_front() else { break; };
        let token = entry.token.clone();
        let weak_state = Arc::downgrade(state);
        let session_id = session_id.to_string();
        let key = job.key.clone();
        let task = tokio::spawn(async move {
            let Some(state) = weak_state.upgrade() else { return; };
            let generation = generate_uncached_brainstorm_step(&state, &job.snapshot, &job.request, true, false, "", Some(&job.key), true);
            tokio::pin!(generation);
            let result = loop {
                tokio::select! {
                    result = &mut generation => break result,
                    _ = tokio::time::sleep(Duration::from_millis(250)) => {
                        if with_brainstorm_session_guard(&state, &session_id, false, |_| Ok(())).is_err() { return; }
                    }
                }
            };
            if with_brainstorm_session_guard(&state, &session_id, false, |_| Ok(())).is_err() { return; }
            let mut cache = state.brainstorm_prefetch.lock().unwrap();
            let Some(entry) = cache.sessions.get_mut(&session_id)
                .filter(|entry| entry.token == token && entry.touched.elapsed() < TTL) else { return; };
            // A replaced/aborted job cannot repopulate the cache. Per-key jobs
            // preserve still-valid siblings while another branch advances.
            if entry.running.remove(&job.key).is_none() { return; }
            match result {
                Ok(result) => {
                    entry.ready.insert(job.key.clone(), result);
                    log_brainstorm_stage(&job.request, "prefetch_ready", Instant::now());
                }
                Err(_) => log_brainstorm_stage(&job.request, "prefetch_discarded", Instant::now()),
            }
            launch_jobs(&state, &session_id, entry);
        });
        entry.running.insert(key, task);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::{repo::creation_brainstorm, StorageManager};
    use serde_json::json;

    const SESSION: &str = "prefetch-unit-session";

    fn question(id: &str, prompt: &str) -> BrainstormQuestion {
        serde_json::from_value(json!({
            "id": id, "dimension": id, "exploration_stage": "solutions",
            "type": "multi_choice", "prompt": prompt, "why_now": "确认当前方向的具体安排。",
            "required": true, "allow_custom": true,
            "options": [
                {"id":"next", "label":"常规推进", "description":"先验证当前方向", "recommended":true},
                {"id":"other", "label":"替代推进", "description":"比较另一种安排", "recommended":false}
            ]
        })).unwrap()
    }

    fn snapshot() -> BrainstormStoredState {
        let mut root = question("root", "哪些方向需要一起讨论？");
        root.exploration_stage = Some("explore".into());
        root.options[0].id = "a".into();
        root.options[0].label = "方向甲".into();
        root.options[1].id = "b".into();
        root.options[1].label = "方向乙".into();
        let mut current = question("branch-a", "甲方向优先采用什么做法？");
        current.parent_question_id = Some("root".into());
        current.parent_option_id = Some("a".into());
        serde_json::from_value(json!({
            "root_request":"制定团队方案", "current_question": current,
            "turns":[{"question":root, "answer":{"selected_option_ids":["a","b"], "source":"user"}}],
            "prefetch_safe_options":{"branch-a":["next","other"]},
            "user_input_revisions":{"root_request":0,"root":1}
        })).unwrap()
    }

    fn request() -> BrainstormTurnRequest {
        serde_json::from_value(json!({
            "session_id":SESSION, "root_request":"制定团队方案", "action":"start"
        })).unwrap()
    }

    fn fixture(stored: &BrainstormStoredState) -> (tempfile::TempDir, Arc<AppState>) {
        let tmp = tempfile::tempdir().unwrap();
        let storage = StorageManager::open(&tmp.path().join("prefetch.db")).unwrap();
        let state = AppState::with_service_urls(storage,
            "http://127.0.0.1:9".into(), "http://127.0.0.1:9".into(), vec![]);
        state.storage.with_conn(|conn| {
            creation_brainstorm::create(conn, SESSION, &stored.root_request,
                "exploring", &serde_json::to_string(stored).unwrap())?;
            Ok(())
        }).unwrap();
        (tmp, state)
    }

    fn persist_next(state: &AppState, stored: &BrainstormStoredState, revision: i64) {
        state.storage.with_conn(|conn| {
            assert!(creation_brainstorm::update(conn, SESSION, revision, "exploring",
                &serde_json::to_string(stored).unwrap())?);
            Ok(())
        }).unwrap();
    }

    fn key_for(stored: &BrainstormStoredState, req: &BrainstormTurnRequest, parent_id: &str, option_id: &str) -> String {
        let branches = pending_brainstorm_branches(stored);
        let branch = branches.iter().find(|branch| branch.question.id == parent_id
            && branch.option.is_some_and(|option| option.id == option_id)).unwrap();
        branch_key(stored, req, branch)
    }

    fn answer_current(stored: &mut BrainstormStoredState, source: &str) {
        let current = stored.current_question.take().unwrap();
        stored.user_input_revisions.insert(current.id.clone(), 2);
        stored.turns.push(BrainstormStoredTurn {
            question: current,
            answer: BrainstormAnswer { selected_option_ids: if source == "user" { vec!["next".into()] } else { vec![] },
                custom_text: if source == "user" { String::new() } else { "暂未确定，保留为待核验假设".into() },
                source: source.into() },
        });
    }

    #[tokio::test]
    async fn prefetch_schedule_rejects_a_snapshot_older_than_the_persisted_revision() {
        let old = snapshot();
        let (_tmp, state) = fixture(&old);
        let mut current = old.clone();
        current.brief_edits.insert("root_request".into(), "仅根据当前输入讨论，不读取历史记忆".into());
        current.user_input_revisions.insert("root_request".into(), 1);
        persist_next(&state, &current, 0);

        schedule(&state, &old, &request(), "exploring", 0);
        assert!(!state.brainstorm_prefetch.lock().unwrap().sessions.contains_key(SESSION),
            "a stale restore must not create an old-permission worker");
        schedule(&state, &current, &request(), "exploring", 1);
        {
            let cache = state.brainstorm_prefetch.lock().unwrap();
            let session = cache.sessions.get(SESSION).unwrap();
            assert_eq!(session.running.len(), 1);
            assert!(session.queued.is_empty());
        }
        invalidate(&state, SESSION);
    }

    #[tokio::test]
    async fn prefetch_revision_tombstone_blocks_old_restore_until_the_change_is_persisted() {
        let old = snapshot();
        let (_tmp, state) = fixture(&old);
        invalidate_revision(&state, SESSION, 0);
        // The incompatible foreground turn is awaiting its model; SQLite still
        // contains revision zero, so a DB-version check alone would permit this.
        schedule(&state, &old, &request(), "exploring", 0);
        {
            let cache = state.brainstorm_prefetch.lock().unwrap();
            let session = cache.sessions.get(SESSION).unwrap();
            assert_eq!(session.minimum_revision, 1);
            assert!(session.running.is_empty());
            assert!(session.queued.is_empty());
        }
        let mut current = old.clone();
        current.brief_edits.insert("root_request".into(), "不要检索私有记忆".into());
        current.user_input_revisions.insert("root_request".into(), 1);
        persist_next(&state, &current, 0);
        schedule(&state, &current, &request(), "exploring", 1);
        {
            let cache = state.brainstorm_prefetch.lock().unwrap();
            let session = cache.sessions.get(SESSION).unwrap();
            assert_eq!(session.running.len(), 1);
            assert!(session.queued.is_empty());
            assert!(!session.running.contains_key(&key_for(&old, &request(), "root", "b")));
        }
        invalidate(&state, SESSION);
    }

    #[test]
    fn prefetch_key_retains_safe_siblings_but_tracks_ancestor_inputs_and_configuration() {
        let old = snapshot();
        let req = request();
        let original = key_for(&old, &req, "root", "b");
        let mut after = old.clone();
        answer_current(&mut after, "user");
        assert_eq!(key_for(&after, &req, "root", "b"), original,
            "a normal answer on branch A must not invalidate branch B");

        let descendant = key_for(&after, &req, "branch-a", "next");
        let mut changed = after.clone();
        changed.user_input_revisions.insert("root".into(), 3);
        assert_ne!(key_for(&changed, &req, "branch-a", "next"), descendant,
            "a descendant must include its grandparent revision");
        let mut changed = after.clone();
        changed.turns[0].question.options[0].label = "重新定义的甲方向".into();
        assert_ne!(key_for(&changed, &req, "branch-a", "next"), descendant);

        let mut changed = old.clone();
        changed.brief_edits.insert("root_request".into(), "仅使用本次输入".into());
        assert_ne!(key_for(&changed, &req, "root", "b"), original);
        let mut changed = old.clone();
        changed.root_request = "新的创作需求".into();
        assert_ne!(key_for(&changed, &req, "root", "b"), original);
        let mut changed = old.clone();
        changed.selected_skills.push(json!({"id": "different-skill"}));
        assert_ne!(key_for(&changed, &req, "root", "b"), original);

        for field in ["creation_model", "creation_base_url", "creation_api_key"] {
            let mut changed = req.clone();
            match field {
                "creation_model" => changed.creation_model = Some("another-model".into()),
                "creation_base_url" => changed.creation_base_url = Some("http://127.0.0.1:19".into()),
                _ => changed.creation_api_key = Some("fixture-only-account-key".into()),
            }
            assert_ne!(key_for(&old, &changed, "root", "b"), original, "{field} must isolate cached candidates");
        }
    }

    #[tokio::test]
    async fn prefetch_take_clears_only_confirmed_question_flags_and_preserves_assumptions() {
        for (source, manually_edited, removes_answered_flag) in [
            ("user", false, true),
            ("agent_assumption", false, false),
            ("agent_assumption", true, true),
        ] {
            let mut stored = snapshot();
            answer_current(&mut stored, source);
            if manually_edited {
                stored.brief_edits.insert("branch-a".into(), "用户人工明确选定的做法".into());
            }
            let answered_flag = "甲方向优先采用什么做法".to_string();
            let unresolved = "仍需核验现场可用资源".to_string();
            stored.open_flags = vec![answered_flag.clone(), unresolved.clone()];
            let (_tmp, state) = fixture(&stored);
            let req = request();
            let key = key_for(&stored, &req, "root", "b");
            let candidate: DynamicBrainstormResult = serde_json::from_value(json!({
                "status":"question", "question":question("candidate-b", "乙方向怎样安排交接？"),
                "open_flags":["候选生成时的旧全局摘要"],
                "sibling_question_goals":[answered_flag, "乙方向如何降低交接负担？"]
            })).unwrap();
            let mut session = empty_session();
            session.ready.insert(key, candidate);
            state.brainstorm_prefetch.lock().unwrap().sessions.insert(SESSION.into(), session);
            let result = take(&state, &stored, &req).expect("the sibling should be usable");
            assert_eq!(!result.open_flags.contains(&answered_flag), removes_answered_flag,
                "source={source}, manually_edited={manually_edited}");
            assert!(result.open_flags.contains(&unresolved));
            assert!(!result.open_flags.iter().any(|flag| flag.contains("旧全局摘要")));
            assert_eq!(result.sibling_question_goals, vec!["乙方向如何降低交接负担？"]);
            assert_eq!(result.question.unwrap().parent_option_id.as_deref(), Some("b"));
        }
    }
}
